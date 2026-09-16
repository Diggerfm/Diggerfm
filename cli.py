"""digger command line.

    python cli.py status
    python cli.py profile "C:/path/to/rekordbox.xml"
    python cli.py collect beatport --charts 200
    python cli.py collect bandcamp --releases 40
    python cli.py digest --days 14
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from digger import db, score
from digger.profile import rekordbox
from digger.sources import bandcamp


def cmd_status(args):
    conn = db.connect()
    print("sightings par source")
    for r in conn.execute("""
        SELECT source, COUNT(*) n, COUNT(DISTINCT work_key) w,
               MAX(seen_at) last FROM sightings GROUP BY source
    """):
        print("  %-10s %6d sightings  %6d oeuvres  dernier %s"
              % (r["source"], r["n"], r["w"], r["last"]))
    p = conn.execute("SELECT COUNT(*) n, SUM(play_count) p FROM profile_tracks").fetchone()
    print("profil: %d morceaux, %s lectures" % (p["n"] or 0, p["p"] or 0))
    f = conn.execute("SELECT COUNT(*) n FROM feedback").fetchone()
    print("retours: %d" % (f["n"] or 0))


def cmd_profile(args):
    conn = db.connect()
    summary = rekordbox.load_into(conn, args.xml)
    print(json.dumps(summary, indent=2))
    prof = rekordbox.profile_summary(conn)
    print("\ntop artistes par ecoutes")
    for a in prof["top_artists"][:15]:
        print("  %-34s %4d morceaux %6s lectures" % (a["artist"][:34], a["n"], a["plays"]))
    print("\ntop labels")
    for l in prof["label"][:12]:
        if l["bucket"]:
            print("  %-34s %4d morceaux %6s lectures" % (l["bucket"][:34], l["n"], l["plays"]))
    rep = rekordbox.colour_report(conn)
    if rep["coloured"]:
        print()
        print("couleurs, %d morceaux sur %d en portent une"
              % (rep["coloured"], rep["total"]))
        print("  Rekordbox n'attache aucun sens aux couleurs, c'est toi qui")
        print("  le donnes. Dis-moi ce que chacune veut dire et elle devient")
        print("  un role de set, comme une playlist nommee 'peak time'.")
        for c in rep["colours"]:
            print("  %-12s %5d morceaux  %6s lectures  BPM moyen %-7s note %s"
                  % (c["colour"], c["n"], c["plays"], c["bpm"], c["rating"]))

    vocab = rekordbox.cue_vocabulary(conn)
    if vocab:
        print()
        print("mots qu'il emploie pour nommer ses points de repere")
        print("  " + ", ".join("%s (%d)" % (w, n) for w, n in vocab[:14]))

    print()
    print("BPM (sur ce qu'il a reellement joue)")
    for b in prof["bpm_histogram"]:
        bar = "#" * min(40, int((b["plays"] or 0) / 2) + 1)
        print("  %3d  %s %s" % (b["bucket"], bar, b["plays"]))


def cmd_collect(args):
    conn = db.connect()
    total = 0
    if args.source in ("beatport", "all"):
        from digger import config
        from digger.sources import beatport
        u, p = config.beatport_credentials()
        client = beatport.BeatportClient(u, p)
        rows = beatport.collect(client, n_charts=args.charts, per_chart=40)
        n = db.upsert_sightings(conn, rows)
        print("beatport: %d pistes, %d nouvelles" % (len(rows), n))
        total += n
    if args.source in ("labels", "all"):
        from digger import config
        from digger.sources import beatport
        cfg = config.load()
        names = list(cfg.get("labels", {}).get("follow") or [])
        if not names:
            names = beatport.labels_from_profile(conn)
            if names:
                print("labels deduits de sa bibliotheque: %d" % len(names))
        if not names:
            print("labels: aucune liste configuree et aucun profil importe, "
                  "rien a suivre")
        else:
            u, p = config.beatport_credentials()
            client = beatport.BeatportClient(u, p)

            def on_label(name, lab, n, err):
                if err:
                    print("  %-24s ECHEC %s" % (name[:24], err))
                else:
                    print("  %-24s %3d pistes" % (name[:24], n))

            rows = beatport.collect_labels(
                client, names,
                since_days=cfg.get("labels", {}).get("since_days", 30),
                on_label=on_label)
            n = db.upsert_sightings(conn, rows)
            print("labels: %d pistes, %d nouvelles" % (len(rows), n))
            total += n

    if args.source in ("enrich", "all"):
        from digger import config
        from digger.sources import beatport
        u, p = config.beatport_credentials()
        client = beatport.BeatportClient(u, p)

        def on_each(row, track, how):
            if track:
                print("  %-4s %-24s %s" % (how, row["artist"][:24], row["title"][:30]))

        r = beatport.enrich_setlist(conn, client, limit=300,
                                    on_each=on_each if args.verbose else None)
        print("enrichissement: %d cherches, %d trouves, %d absents de Beatport"
              % (r["looked_up"], r["found"], r["missed"]))

    if args.source in ("bandcamp", "all"):
        rows = bandcamp.collect_tracks(pages=args.pages, slices=("new", "top"),
                                       genre=args.genre, max_releases=args.releases)
        n = db.upsert_sightings(conn, rows)
        print("bandcamp: %d pistes, %d nouvelles" % (len(rows), n))
        total += n
    print("total nouveau: %d" % total)


def cmd_discover(args):
    """Read this week's sets from the 1001Tracklists index and locate them.

    Only the index is read from their site: one request. Their set pages
    serve a Cloudflare challenge after about three, which this does not try
    to get around. The recording is then found on the platform hosting it,
    and a candidate that cannot prove it is the right episode is refused.
    """
    import os
    from digger.sources import tracklists
    from digger.sources.setfinder import find_recording

    ytdlp = os.path.join(ROOT, ".venv", "Scripts", "yt-dlp.exe")
    if not os.path.exists(ytdlp):
        ytdlp = "yt-dlp"

    sets_path = os.path.join(ROOT, "sets.txt")
    known = set()
    if os.path.exists(sets_path):
        with open(sets_path, encoding="utf-8") as fh:
            known = {l.strip() for l in fh if l.strip() and not l.startswith("#")}

    try:
        index = tracklists.fetch_index()
    except tracklists.Blocked as exc:
        print("1001Tracklists nous demande de nous arreter: %s" % exc)
        print("Rien n'a ete force. Reessaie plus tard.")
        return

    wanted = [s for s in index if s["has_media"]][:args.limit]
    print("index: %d sets, %d avec un enregistrement, %d examines"
          % (len(index), sum(1 for s in index if s["has_media"]), len(wanted)))
    print()

    found, refused = [], 0
    for s in wanted:
        hit = find_recording(s["title"], ytdlp)
        short = s["title"][:46]
        if not hit:
            refused += 1
            print("  refuse  %-46s aucun candidat ne prouve l'edition" % short)
            continue
        if hit["url"] in known:
            print("  connu   %-46s" % short)
            continue
        found.append((s, hit))
        print("  trouve  %-46s %d min" % (short, int(hit["duration"] // 60)))
        print("          %s" % hit["url"])

    print()
    print("%d nouveaux, %d refuses faute de preuve d'edition" % (len(found), refused))
    if found and not args.dry:
        with open(sets_path, "a", encoding="utf-8") as fh:
            for s, hit in found:
                fh.write("\n# %s (%s)\n%s\n"
                         % (s["title"], s.get("date") or "", hit["url"]))
        print("ajoutes a sets.txt. Lance ensuite: python cli.py fingerprint --file sets.txt")
    elif found:
        print("--dry: rien n'a ete ecrit.")


def cmd_fingerprint(args):
    """Identify what a DJ actually played, as opposed to what he charted."""
    from digger.fingerprint import sets as fpsets
    conn = db.connect()

    urls = list(args.url)
    if args.file:
        with open(args.file) as fh:
            urls += [l.strip() for l in fh if l.strip() and not l.startswith("#")]
    if not urls:
        print("aucune URL. Passe une URL ou --file liste.txt")
        return

    def progress(rec):
        h = rec["hit"]
        label = "%s - %s" % (h["artist"][:26], h["title"][:34]) if h else "(rien)"
        print("   t=%4ds  %s" % (rec["offset"], label), flush=True)

    total_new = 0
    for url in urls:
        print()
        print(">>> %s" % url)
        try:
            sightings, stats = fpsets.fingerprint_set(
                url, probe_every=args.every, max_slices=args.slices,
                max_seconds=args.seconds, delay=args.delay,
                on_result=progress if args.verbose else None)
        except Exception as exc:
            print("    ECHEC: %s" % str(exc)[:200])
            continue
        n = db.upsert_sightings(conn, sightings)
        total_new += n
        print("    %d sondes, %d reconnues (%.0f%%), %d morceaux, %d nouveaux"
              % (stats["probes"], stats["matched"], 100 * stats["match_rate"],
                 stats["tracks"], n))
        for s in sightings:
            print("      %-26s - %s" % (s["artist"][:26], s["title"][:38]))
    print()
    print("total nouveau: %d" % total_new)


def cmd_analyze(args):
    """Compute timbre locally: previews for candidates, real files for him."""
    from digger import features
    conn = db.connect()

    def on_done(row, feats, status):
        if feats:
            print("  %-24s %-28s bpm=%-6s perc=%-5s bright=%.0f" % (
                row["artist"][:24], row["title"][:28], feats["bpm"] or "-",
                feats["percussive_ratio"] or "-", feats["brightness"]), flush=True)
        else:
            print("  %-24s %-28s ECHEC %s" % (
                row["artist"][:24], row["title"][:28], status), flush=True)

    cb = on_done if args.verbose else None
    if args.profile:
        r = features.analyze_profile(conn, limit=args.limit,
                                     min_plays=args.min_plays, on_done=cb)
        print("profil: %s" % r)
    else:
        r = features.analyze_pending(conn, limit=args.limit,
                                     source=args.source, on_done=cb)
        print("candidats: %s" % r)

    print()
    print("couverture")
    for origin, c in features.coverage(conn).items():
        print("  %-9s n=%-4d ok=%-4d bpm=%-4d | energie %s | percussif %s | brillance %s" % (
            origin, c["n"], c["ok"], c["with_bpm"], c["avg_energy"],
            c["avg_percussive"], c["avg_brightness"]))


def cmd_digest(args):
    conn = db.connect()
    artists, labels = score.profile_vectors(conn)
    timbre = score.timbre_profile(conn)
    cand_feats = score.load_candidate_features(conn)
    scored = score.score_all(conn, since_days=args.days)
    if timbre:
        print("profil timbral construit sur %d morceaux analyses" % timbre["n"])
    else:
        print("pas encore de profil timbral (il en faut %d analyses), "
              "affinite sur les noms seuls" % score.MIN_PROFILE_TRACKS)
    if not scored:
        print("aucun candidat. Lance d'abord: python cli.py collect all")
        return
    slots = score.assign_slots(scored)
    print("%d candidats -> %d proposes\n" % (len(scored), len(slots)))
    current = None
    for s in slots:
        if s["slot"] != current:
            current = s["slot"]
            print("\n--- %s ---" % current.upper())
        print("%-28s - %-34s" % (s["artist"][:28], s["title"][:34]))
        print("    %s" % score.explain(s, artists, labels, timbre,
                                       cand_feats.get(s["work_key"])))
        if s.get("url"):
            print("    %s" % s["url"])
    week = args.week or __import__("datetime").date.today().isoformat()
    if args.export:
        from digger import feedback as fb
        fb.export_digest(
            conn, slots, args.export, week,
            explain_fn=lambda x: score.explain(x, artists, labels, timbre,
                                               cand_feats.get(x["work_key"])))
        print()
        print("exporte dans %s" % args.export)
        print("Envoie-lui le fichier, il le remplit, puis :")
        print("  python cli.py feedback --import %s" % args.export)
    if args.save:
        with conn:
            for s in slots:
                conn.execute("""INSERT OR IGNORE INTO digest_items
                    (week, work_key, slot, rank, reason) VALUES (?,?,?,?,?)""",
                    (week, s["work_key"], s["slot"], s["rank"],
                     score.explain(s, artists, labels, timbre,
                                   cand_feats.get(s["work_key"]))))
        print("\nenregistre pour la semaine %s" % week)


def cmd_feedback(args):
    """Record his verdicts, and re-derive the weights once there are enough."""
    from digger import feedback
    conn = db.connect()

    if args.import_path:
        res = feedback.import_file(conn, args.import_path)
        print("enregistres: %d | sans reponse: %d | non reconnus: %d"
              % (res["recorded"], res["unanswered"], res["unrecognised"]))
    elif args.work_key:
        v = feedback.record(conn, args.work_key, args.verdict, args.why)
        print("enregistre: %s -> %s" % (args.work_key, v))

    if args.learn:
        res = feedback.learn_weights(conn)
        if res is None:
            print()
            print("pas encore assez d'avis notes pour reapprendre les poids.")
            print("Il en faut 25 dont 8 aimes et 8 rejetes, sur des morceaux analyses.")
        else:
            print()
            print("poids reappris sur ses verdicts (%d aimes / %d rejetes)"
                  % (res["n_liked"], res["n_disliked"]))
            for field, d in sorted(res["cohens_d"].items(),
                                   key=lambda x: -(x[1] or 0)):
                print("  %-18s d=%-6s poids=%s"
                      % (field, d, res["weights"][field]))
            print()
            print("Reporte-les dans score.TIMBRE_WEIGHTS pour les activer.")

    s = feedback.summary(conn)
    print()
    print("total retours: %d, dont %d avec une raison" % (s["total"], s["with_reason"]))
    for verdict, n in sorted(s["by_verdict"].items(), key=lambda x: -x[1]):
        print("  %-8s %d" % (verdict, n))

    if args.list:
        print()
        for r in conn.execute("""SELECT fb.work_key, fb.verdict, fb.why,
                                        MIN(s.artist) a, MIN(s.title) t
                                 FROM feedback fb
                                 LEFT JOIN sightings s ON s.work_key = fb.work_key
                                 GROUP BY fb.id ORDER BY fb.rated_at DESC LIMIT 40"""):
            print("  %-6s %-24s - %-28s %s"
                  % (r["verdict"], (r["a"] or "?")[:24], (r["t"] or "?")[:28],
                     r["why"] or ""))


def main():
    p = argparse.ArgumentParser(prog="digger")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(func=cmd_status)

    sp = sub.add_parser("profile", help="importer l'export XML rekordbox")
    sp.add_argument("xml")
    sp.set_defaults(func=cmd_profile)

    sc = sub.add_parser("collect")
    sc.add_argument("source",
                choices=["beatport", "bandcamp", "labels", "enrich", "all"])
    sc.add_argument("--charts", type=int, default=50)
    sc.add_argument("--releases", type=int, default=30)
    sc.add_argument("--pages", type=int, default=1)
    sc.add_argument("--genre", default="electronic")
    sc.add_argument("-v", "--verbose", action="store_true")
    sc.set_defaults(func=cmd_collect)

    sd2 = sub.add_parser("discover",
                         help="relever les sets de la semaine et les localiser")
    sd2.add_argument("--limit", type=int, default=12)
    sd2.add_argument("--dry", action="store_true")
    sd2.set_defaults(func=cmd_discover)

    sf = sub.add_parser("fingerprint", help="identifier les morceaux d'un set")
    sf.add_argument("url", nargs="*")
    sf.add_argument("--file", help="fichier d'URLs, une par ligne")
    sf.add_argument("--every", type=int, default=90, help="secondes entre sondes")
    sf.add_argument("--slices", type=int, default=None, help="nombre max de sondes")
    sf.add_argument("--seconds", type=int, default=None, help="limiter le telechargement")
    sf.add_argument("--delay", type=float, default=3.0, help="pause entre appels Shazam")
    sf.add_argument("-v", "--verbose", action="store_true")
    sf.set_defaults(func=cmd_fingerprint)

    sa = sub.add_parser("analyze", help="analyse audio locale")
    sa.add_argument("--source", choices=["beatport", "bandcamp", "setlist"])
    sa.add_argument("--profile", action="store_true",
                    help="analyser sa bibliotheque rekordbox au lieu des candidats")
    sa.add_argument("--limit", type=int, default=200)
    sa.add_argument("--min-plays", type=int, default=0, dest="min_plays")
    sa.add_argument("-v", "--verbose", action="store_true")
    sa.set_defaults(func=cmd_analyze)

    sd = sub.add_parser("digest")
    sd.add_argument("--days", type=int, default=14)
    sd.add_argument("--save", action="store_true")
    sd.add_argument("--week")
    sd.add_argument("--export", help="ecrire un fichier a faire remplir")
    sd.set_defaults(func=cmd_digest)

    sb = sub.add_parser("feedback", help="enregistrer ses verdicts")
    sb.add_argument("work_key", nargs="?")
    sb.add_argument("verdict", nargs="?",
                    help="fire | keep | meh | never")
    sb.add_argument("why", nargs="?")
    sb.add_argument("--import", dest="import_path",
                    help="importer un digest rempli")
    sb.add_argument("--list", action="store_true")
    sb.add_argument("--learn", action="store_true",
                    help="reapprendre les poids timbraux sur ses verdicts")
    sb.set_defaults(func=cmd_feedback)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
