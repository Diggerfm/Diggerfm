"""The weekly run.

His brief: "on peut aussi programmer ici un dig hebdomadaire automatique,
par exemple chaque jeudi ou vendredi avant que tu prepares ta musique du
week-end". This is that run, start to finish.

Each stage is wrapped: a source being down must not cost the whole week's
digest, so a failure is recorded and the run continues on what is left.
"""

import datetime
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from digger import db, score
from digger.sources import bandcamp

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "data", "digests")
SETS_FILE = os.path.join(ROOT, "sets.txt")


def log(msg):
    print("[%s] %s" % (datetime.datetime.now().strftime("%H:%M:%S"), msg),
          flush=True)


def stage(name, fn, results):
    """Run one stage; record the outcome instead of aborting the run."""
    log("--- %s" % name)
    try:
        value = fn()
        results[name] = {"ok": True, "value": value}
        log("    ok: %s" % value)
        return value
    except Exception as exc:
        results[name] = {"ok": False, "error": str(exc)[:200]}
        log("    ECHEC: %s" % str(exc)[:200])
        if os.environ.get("DIGGER_DEBUG"):
            traceback.print_exc()
        return None


def read_sets():
    """URLs of sets to fingerprint, one per line, # for comments."""
    if not os.path.exists(SETS_FILE):
        return []
    with open(SETS_FILE, encoding="utf-8") as fh:
        return [l.strip() for l in fh
                if l.strip() and not l.strip().startswith("#")]


def main():
    week = datetime.date.today().isoformat()
    os.makedirs(OUT_DIR, exist_ok=True)
    conn = db.connect()
    results = {}
    log("=== digger, semaine %s ===" % week)

    # 1. Charts DJ
    def charts():
        from digger import config
        from digger.sources import beatport
        u, p = config.beatport_credentials()
        client = beatport.BeatportClient(u, p)
        rows = beatport.collect(client, n_charts=120, per_chart=40)
        return "%d pistes, %d nouvelles" % (len(rows), db.upsert_sightings(conn, rows))
    stage("charts beatport", charts, results)

    # 2. Labels he follows
    def labels():
        from digger import config
        from digger.sources import beatport
        cfg = config.load()
        names = list(cfg.get("labels", {}).get("follow") or []) \
            or beatport.labels_from_profile(conn)
        if not names:
            return "aucun label a suivre"
        u, p = config.beatport_credentials()
        client = beatport.BeatportClient(u, p)
        rows = beatport.collect_labels(
            client, names, since_days=cfg.get("labels", {}).get("since_days", 30))
        return "%d labels, %d pistes, %d nouvelles" % (
            len(names), len(rows), db.upsert_sightings(conn, rows))
    stage("labels", labels, results)

    # 3. Bandcamp
    def bc():
        rows = bandcamp.collect_tracks(pages=2, slices=("new", "top"),
                                       genre="electronic", max_releases=60)
        return "%d pistes, %d nouvelles" % (len(rows), db.upsert_sightings(conn, rows))
    stage("bandcamp", bc, results)

    # 4. Sets to fingerprint
    def sets():
        urls = read_sets()
        if not urls:
            return "aucun set dans sets.txt"
        from digger.fingerprint import sets as fpsets
        done = 0
        for url in urls:
            already = conn.execute(
                "SELECT 1 FROM sightings WHERE source='setlist' AND url = ? LIMIT 1",
                (url,)).fetchone()
            if already:
                continue
            sightings, stats = fpsets.fingerprint_set(url, probe_every=90)
            db.upsert_sightings(conn, sightings)
            done += 1
            log("    %s -> %d morceaux (%.0f%% reconnu)"
                % (url[-40:], stats["tracks"], 100 * stats["match_rate"]))
        return "%d nouveaux sets sur %d" % (done, len(urls))
    stage("fingerprint sets", sets, results)

    # 5. Resolve what the sets turned up against the catalogue.
    #    Fingerprinting yields an artist and a title; Beatport's ?isrc=
    #    filter turns that into BPM, key, label, a preview and a buy link,
    #    so the records he was actually played become usable.
    def enrich():
        from digger import config
        from digger.sources import beatport
        u, p = config.beatport_credentials()
        client = beatport.BeatportClient(u, p)
        r = beatport.enrich_setlist(conn, client, limit=300)
        return "%d cherches, %d resolus, %d absents du catalogue" % (
            r["looked_up"], r["found"], r["missed"])
    stage("enrichissement des sets", enrich, results)

    # 6. Timbre on whatever is new
    def feats():
        # 300 was a permanent backlog, not a cap: a week brings roughly 1500
        # new tracks from 120 charts, so the queue only ever grew. At about
        # four seconds each this is the long pole of the run, which is the
        # right place for it in a Friday-morning background job.
        from digger import features
        r = features.analyze_pending(conn, limit=1500)
        return "%d analyses, %d echecs" % (r["analyzed"], r["failed"])
    stage("analyse audio", feats, results)

    # 7. The digest itself
    def digest():
        from digger import feedback as fb
        artists, labels_v = score.profile_vectors(conn)
        timbre = score.timbre_profile(conn)
        cand_feats = score.load_candidate_features(conn)
        scored = score.score_all(conn, since_days=10)
        slots = score.assign_slots(scored)
        path = os.path.join(OUT_DIR, "digest-%s.md" % week)
        fb.export_digest(
            conn, slots, path, week,
            explain_fn=lambda x: score.explain(x, artists, labels_v, timbre,
                                               cand_feats.get(x["work_key"])))
        with conn:
            for s in slots:
                conn.execute(
                    "INSERT OR IGNORE INTO digest_items "
                    "(week, work_key, slot, rank, reason) VALUES (?,?,?,?,?)",
                    (week, s["work_key"], s["slot"], s["rank"],
                     score.explain(s, artists, labels_v, timbre,
                                   cand_feats.get(s["work_key"]))))
        return "%d candidats -> %d proposes -> %s" % (len(scored), len(slots), path)
    stage("digest", digest, results)

    failed = [k for k, v in results.items() if not v["ok"]]
    log("=== termine, %d/%d etapes ok ===" % (len(results) - len(failed), len(results)))
    if failed:
        log("etapes en echec: %s" % ", ".join(failed))
    return 1 if len(failed) == len(results) else 0


if __name__ == "__main__":
    sys.exit(main())
