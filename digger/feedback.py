"""His verdicts, and what the system learns from them.

His brief is explicit that the value is not the rating but the reason: "tu
me dis simplement bien / moyen / jamais, et surtout deux mots sur pourquoi".
A rating ranks; a reason teaches. So `why` is carried everywhere and the
re-weighting below reads the ratings while the reasons stay readable by a
human, who is still better at spotting "the break is too long" than any
regression on five numbers.

Exchange happens through a file rather than a prompt, because he is not at
this machine: export a digest, send it, get it back filled in, import it.
"""

import os
import re

VERDICTS = {
    "fire": ("fire", "enorme", "3", "!!!", "feu"),
    "keep": ("keep", "garde", "2", "ok", "oui"),
    "meh": ("meh", "moyen", "1", "bof"),
    "never": ("never", "jamais", "0", "non", "no"),
}

# Ratings, for learning. Never is deliberately negative rather than zero:
# "not for me" is information, not an absence of it.
VERDICT_SCORE = {"fire": 1.0, "keep": 0.6, "meh": 0.1, "never": -0.6}

# The placeholder is "???", so the verdict slot has to accept non-word
# characters: matching only \w+ made an unanswered line fail the regex
# outright and vanish, which then reported "0 unanswered" when there were
# some. An untouched line must be counted, not silently dropped.
LINE_RE = re.compile(
    r"^\s*-\s*\[(?P<key>[^\]]+)\]\s*(?P<verdict>[^|]*?)\s*(?:\|\s*(?P<why>.*))?$")


def normalize_verdict(raw):
    if not raw:
        return None
    low = raw.strip().lower()
    for canonical, aliases in VERDICTS.items():
        if low == canonical or low in aliases:
            return canonical
    return None


def export_digest(conn, slots, path, week, explain_fn=None):
    """Write a digest he can fill in and send back."""
    lines = [
        "# Digest %s" % week,
        "",
        "Pour chaque morceau, remplace le mot apres le crochet par :",
        "  fire   = enorme, je le joue",
        "  keep   = je garde",
        "  meh    = moyen",
        "  never  = jamais",
        "",
        "Et apres le | , deux mots sur POURQUOI. C'est la partie qui compte.",
        "Exemple :  - [abc::def] keep | groove ok mais le break est trop long",
        "",
    ]
    current = None
    for s in slots:
        if s["slot"] != current:
            current = s["slot"]
            lines += ["", "## %s" % current.upper(), ""]
        lines.append("%s - %s" % (s["artist"], s["title"]))
        if explain_fn:
            lines.append("    %s" % explain_fn(s))
        if s.get("url"):
            lines.append("    %s" % s["url"])
        lines.append("  - [%s] ??? | " % s["work_key"])
        lines.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


def import_file(conn, path):
    """Read back a filled digest. Unanswered lines are skipped, not guessed."""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    recorded = skipped = unknown = 0
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = LINE_RE.match(line.rstrip("\n"))
            if not m:
                continue
            verdict = normalize_verdict(m.group("verdict"))
            if verdict is None:
                if (m.group("verdict") or "").strip("? "):
                    unknown += 1
                else:
                    skipped += 1
                continue
            why = (m.group("why") or "").strip() or None
            rows.append((m.group("key").strip(), verdict, why))
            recorded += 1

    with conn:
        for key, verdict, why in rows:
            conn.execute(
                "INSERT INTO feedback (work_key, verdict, why) VALUES (?,?,?)",
                (key, verdict, why))
    return {"recorded": recorded, "unanswered": skipped, "unrecognised": unknown}


def record(conn, work_key, verdict, why=None, slot=None):
    v = normalize_verdict(verdict)
    if v is None:
        raise ValueError("verdict inconnu: %r" % verdict)
    with conn:
        conn.execute(
            "INSERT INTO feedback (work_key, verdict, why, slot) VALUES (?,?,?,?)",
            (work_key, v, why, slot))
    return v


def summary(conn):
    out = {"by_verdict": {}, "with_reason": 0, "total": 0}
    for r in conn.execute(
            "SELECT verdict, COUNT(*) n, SUM(why IS NOT NULL) w "
            "FROM feedback GROUP BY verdict"):
        out["by_verdict"][r["verdict"]] = r["n"]
        out["with_reason"] += r["w"] or 0
        out["total"] += r["n"]
    return out


def learn_weights(conn, min_rated=25):
    """Re-derive the timbre weights from his verdicts instead of from genre.

    The weights currently shipped were measured against genre labels, which
    the brief explicitly rejects as a stand-in for taste. Once enough tracks
    carry a verdict, each feature is scored by how far apart the liked and
    the rejected sit on it, and the weights follow that instead.

    Returns None below min_rated: a weight table fitted on a dozen opinions
    would be worse than the honest genre-derived one it replaces.
    """
    rows = conn.execute("""
        SELECT fb.verdict, f.energy, f.dynamics, f.brightness,
               f.percussive_ratio, f.onset_rate
        FROM feedback fb
        JOIN audio_features f ON f.work_key = fb.work_key
        WHERE f.error IS NULL
    """).fetchall()
    if len(rows) < min_rated:
        return None

    fields = ("energy", "dynamics", "brightness", "percussive_ratio", "onset_rate")
    liked = [r for r in rows if VERDICT_SCORE.get(r["verdict"], 0) > 0.3]
    disliked = [r for r in rows if VERDICT_SCORE.get(r["verdict"], 0) <= 0.1]
    if len(liked) < 8 or len(disliked) < 8:
        return None

    weights, detail = {}, {}
    for field in fields:
        a = [r[field] for r in liked if r[field] is not None]
        b = [r[field] for r in disliked if r[field] is not None]
        if len(a) < 5 or len(b) < 5:
            detail[field] = None
            continue
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        va = sum((x - ma) ** 2 for x in a) / len(a)
        vb = sum((x - mb) ** 2 for x in b) / len(b)
        pooled = (((va + vb) / 2) ** 0.5) or 1e-9
        detail[field] = round(abs(ma - mb) / pooled, 3)

    valid = {k: v for k, v in detail.items() if v}
    if not valid:
        return None
    top = max(valid.values())
    for field in fields:
        d = detail.get(field)
        weights[field] = round(max(0.15, 1.5 * d / top), 2) if d else 0.15
    return {"weights": weights, "cohens_d": detail,
            "n_liked": len(liked), "n_disliked": len(disliked)}
