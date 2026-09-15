"""Candidate scoring.

The v1 signal is deliberately shallow and says so: cross-source agreement,
plus affinity measured on artists and labels he already plays. It does not
listen to anything. Audio similarity is the layer that would make this real,
and it plugs in at affinity() without touching the rest.

The slot quota is imposed rather than derived. A pure ranker collapses onto
the same handful of artists every week, so the wildcard and bridge slots are
reserved by construction.
"""

import math

from digger import charts

SLOT_QUOTA = [
    ("bullseye", 10),      # squarely his sound
    ("modernisation", 5),  # current, still close enough to play
    ("bridge", 3),         # hooks a contemporary crowd before his own set
    ("wildcard", 2),       # he would not have found it himself
]


def label_rarity(conn):
    """How much a shared label is worth, by how common the label is.

    Borrowed from kristopolous/Mutiny, whose weight.py puts it plainly:
    "High cardinality (common) properties = weak signal. Low cardinality
    (rare) properties = strong signal. This is fundamentally Bayesian."

    The previous scoring had this backwards. It scored a label match as
    min(1, plays / 40), so a label he plays constantly scored highest, and
    Defected, which everybody plays, counted the same as a small label he
    has quietly bought from for years. Sharing a label with three thousand
    other records says almost nothing; sharing one with forty says a lot.
    """
    sizes = {}
    for r in conn.execute("""
        SELECT LOWER(label) AS label, COUNT(DISTINCT work_key) AS n
        FROM sightings WHERE label IS NOT NULL AND TRIM(label) != ''
        GROUP BY LOWER(label)
    """):
        sizes[r["label"]] = r["n"]
    if not sizes:
        return {}
    rarity = {}
    for name, n in sizes.items():
        # 1/log, floored so a one-release label is not infinitely strong.
        rarity[name] = 1.0 / math.log(max(n, 2) + 1.0)
    top = max(rarity.values()) or 1.0
    return {k: v / top for k, v in rarity.items()}


def profile_vectors(conn):
    """Artists and labels he plays, weighted by play count."""
    artists, labels = {}, {}
    for r in conn.execute("""
        SELECT LOWER(artist) a, SUM(play_count) p, COUNT(*) n
        FROM profile_tracks GROUP BY LOWER(artist)
    """):
        artists[r["a"]] = (r["p"] or 0) + r["n"]
    for r in conn.execute("""
        SELECT LOWER(label) l, SUM(play_count) p, COUNT(*) n
        FROM profile_tracks WHERE label IS NOT NULL GROUP BY LOWER(label)
    """):
        labels[r["l"]] = (r["p"] or 0) + r["n"]
    return artists, labels


def owned_works(conn):
    return {r["work_key"] for r in conn.execute("SELECT work_key FROM profile_tracks")}


def rejected_works(conn):
    return {r["work_key"] for r in conn.execute(
        "SELECT work_key FROM feedback WHERE verdict = 'never'")}


def candidates(conn, since_days=14):
    """One row per work seen recently, with its cross-source evidence."""
    rows = conn.execute("""
        SELECT work_key,
               COUNT(DISTINCT source) AS n_sources,
               GROUP_CONCAT(DISTINCT CASE WHEN source = 'beatport'
                    THEN json_extract(raw, '$.chart_id') END) AS chart_ids,
               COUNT(DISTINCT CASE WHEN source = 'setlist'
                    THEN url END) AS n_sets,
               COUNT(*)               AS n_sightings,
               MAX(bpm) AS bpm, MAX(music_key) AS music_key, MAX(isrc) AS isrc,
               GROUP_CONCAT(DISTINCT source) AS sources,
               MIN(artist) AS artist, MIN(title) AS title,
               MAX(url) AS url, MAX(stream_url) AS stream_url,
               MAX(genre) AS genre, MAX(label) AS label,
               MAX(published_at) AS published_at
        FROM sightings
        WHERE seen_at >= datetime('now', ?)
        GROUP BY work_key
    """, ("-%d days" % since_days,))
    out = []
    for r in rows:
        d = dict(r)
        ids = [i for i in (d.get("chart_ids") or "").split(",") if i]
        d.setdefault("n_charts", len(set(ids)))
        out.append(d)
    return out


def affinity(cand, artists, labels, timbre=None, feats=None, rarity=None):
    """How close this candidate sits to what he already plays, 0 to 1.

    Timbre leads when it is available on both sides, because it measures the
    record rather than its credits. Name proximity stays in the blend: it
    carries something sound does not, namely that he trusts this artist or
    this label, and it is the only signal left when a preview failed to
    download.
    """
    a = artists.get((cand.get("artist") or "").lower(), 0)
    name = (cand.get("label") or "").lower()
    l = labels.get(name, 0)
    artist_score = min(1.0, a / 20.0)
    # Scaled by how rare the label is, not by how much of it he owns.
    label_score = min(1.0, l / 10.0) * (rarity or {}).get(name, 0.5)
    name_score = 0.75 * artist_score + 0.25 * label_score

    sound_score = timbre_similarity(feats, timbre) if (timbre and feats) else None
    if sound_score is None:
        return name_score
    return round(0.65 * sound_score + 0.35 * name_score, 4)


def corroboration(cand):
    """How many independent parties vouched for this track.

    Measured on 2026-09-15 over 25 charts and 2 sources: Bandcamp and
    Beatport overlapped on 1 work out of 644 (0.16 %), so cross-source
    agreement is nearly always zero and cannot carry the score. The signal
    that does exist is within Beatport: 8 of 345 tracks were charted by more
    than one DJ off only 25 charts, and Beatport exposes 10 000, so this
    sharpens as n_charts grows. Chart count leads, cross-source is a bonus.
    """
    charts = cand.get("n_charts") or 0
    chart_score = min(1.0, max(0, charts - 1) / 3.0)

    # A set play outranks a chart entry and used to count for nothing, which
    # meant a record a DJ he follows actually dropped scored zero unless
    # somebody had also charted it. A chart is a claim of support and can be
    # traded for promo; a fingerprinted set is what came out of the speakers.
    # So one set already carries weight where one chart does not.
    sets = cand.get("n_sets") or 0
    set_score = min(1.0, sets * 0.4)

    cross = 0.25 if (cand.get("n_sources") or 1) > 1 else 0.0
    return min(1.0, chart_score + set_score + cross)


def score_all(conn, since_days=14):
    artists, labels = profile_vectors(conn)
    owned = owned_works(conn)
    rejected = rejected_works(conn)

    timbre = timbre_profile(conn)
    cand_feats = load_candidate_features(conn)
    rarity = label_rarity(conn)
    # Charts that carry the same records are one opinion, not several.
    # Without this a label charting its own catalogue five times put ten of
    # its releases at the top of the ranking. See digger/charts.py.
    groups = charts.group_map(conn)

    scored = []
    for c in candidates(conn, since_days):
        if c["work_key"] in owned or c["work_key"] in rejected:
            continue
        ids = [i for i in (c.get("chart_ids") or "").split(",") if i]
        c["n_charts_raw"] = len(set(ids))
        c["n_charts"] = len({groups.get(i, i) for i in ids})
        feats = cand_feats.get(c["work_key"])
        c["has_timbre"] = bool(timbre and feats)
        c["affinity"] = affinity(c, artists, labels, timbre=timbre, feats=feats,
                                 rarity=rarity)
        c["corroboration"] = corroboration(c)
        c["score"] = 0.6 * c["affinity"] + 0.4 * c["corroboration"]
        scored.append(c)
    scored.sort(key=lambda x: (-x["score"], -x["n_sources"]))
    return scored


def assign_slots(scored, quota=None):
    """Fill the imposed quota rather than taking the top N.

    bullseye takes the highest affinity, modernisation the corroborated ones
    that are less close, bridge the corroborated ones furthest from him, and
    wildcard is drawn from the tail so the list cannot become a monoculture.
    """
    quota = quota or SLOT_QUOTA
    pool = list(scored)
    out = []

    def take(n, key, pred=None):
        picked = []
        for c in sorted([c for c in pool if pred is None or pred(c)], key=key):
            if len(picked) >= n:
                break
            picked.append(c)
        for c in picked:
            pool.remove(c)
        return picked

    for slot, n in quota:
        if slot == "bullseye":
            got = take(n, lambda c: -c["affinity"], lambda c: c["affinity"] >= 0.35)
        elif slot == "modernisation":
            got = take(n, lambda c: -c["score"], lambda c: c["n_sources"] >= 1)
        elif slot == "bridge":
            got = take(n, lambda c: (-c["corroboration"], c["affinity"]))
        else:
            mid = pool[len(pool) // 3:] if len(pool) > 6 else pool
            got = mid[:n]
            for c in got:
                pool.remove(c)
        for rank, c in enumerate(got, 1):
            c["slot"] = slot
            c["rank"] = rank
            out.append(c)
    return out


def explain(cand, artists, labels, timbre=None, feats=None):
    """Why this one is on the list. Plain reasons, no invented percentage."""
    bits = []
    a = artists.get((cand.get("artist") or "").lower(), 0)
    if a:
        bits.append("tu joues deja cet artiste (%d ecoutes)" % a)
    l = labels.get((cand.get("label") or "").lower(), 0)
    if l:
        bits.append("label que tu joues (%s)" % cand.get("label"))
    sets = cand.get("n_sets") or 0
    if sets:
        bits.insert(0, "joue dans %d set%s" % (sets, "s" if sets > 1 else ""))
    if (cand.get("n_charts") or 0) >= 2:
        raw = cand.get("n_charts_raw") or cand["n_charts"]
        if raw > cand["n_charts"]:
            bits.append("charte par %d DJs (%d charts, doublons retires)"
                        % (cand["n_charts"], raw))
        else:
            bits.append("charte par %d DJs" % cand["n_charts"])
    if cand["n_sources"] >= 2:
        bits.append("vu sur %d sources (%s)" % (cand["n_sources"], cand["sources"]))
    if cand.get("bpm"):
        bits.append("%s BPM %s" % (int(cand["bpm"]), cand.get("music_key") or ""))
    if timbre and feats:
        sim = timbre_similarity(feats, timbre)
        if sim is not None:
            if sim >= 0.6:
                bits.insert(0, "sonorite tres proche de ton profil")
            elif sim >= 0.35:
                bits.insert(0, "sonorite compatible")
            else:
                bits.append("sonorite eloignee de ton profil")
    elif feats and not timbre:
        # The candidate is analysed; what is missing is something to compare
        # it against. Saying "no audio analysis" here would send the next
        # reader debugging the wrong half of the pipeline.
        bits.append("analyse audio faite, profil de reference absent")
    elif timbre and not feats:
        bits.append("pas d'analyse audio sur ce morceau")
    if not bits:
        bits.append("hors de ton perimetre habituel")
    return ", ".join(bits)


# --- Timbre affinity ----------------------------------------------------
#
# Artist and label proximity was always a stand-in. His brief asked for
# groove, bassline weight, organic percussion and tension, none of which a
# name carries. These five measures do, and no source sells them.
#
# BPM and key are deliberately absent here. Validated against Beatport on
# 12 tracks, local key detection scored 5/12, which is worse than useless
# for a DJ, and Beatport already supplies both fields authoritatively.

TIMBRE_FIELDS = ("energy", "dynamics", "brightness", "percussive_ratio",
                 "onset_rate")

# Weights are measured, not guessed. Cohen's d between 124 tracks in his
# genre territory (tech house, minimal, afro, hypnotic techno, deep and
# organic house, melodic) and 96 in deliberate negative controls (drum and
# bass, psy-trance, hard dance, dance pop, latin electronic):
#
#     energy            0.99   the only strong separator
#     onset_rate        0.57
#     dynamics          0.42
#     percussive_ratio  0.26   and unstable: it read 0.55 on a narrower set
#     brightness        0.02   indistinguishable from noise
#
# An earlier version of this table had brightness at 1.1 and energy at 0.8,
# which was backwards. Two caveats stand: these were measured against genre,
# a proxy for his taste and not his taste itself, and n is 220. They get
# re-derived from his own verdicts once feedback exists.
TIMBRE_WEIGHTS = {
    "energy": 1.50,
    "onset_rate": 0.86,
    "dynamics": 0.63,
    "percussive_ratio": 0.39,
    "brightness": 0.15,
}

MIN_PROFILE_TRACKS = 15   # below this the centroid is noise, so fall back


def timbre_profile(conn, min_plays=1):
    """His sonic centre of gravity, weighted by how often he played a track.

    Returns None when too few of his tracks have been analysed, so callers
    fall back to name matching rather than trusting a centroid built from
    three records.
    """
    rows = conn.execute("""
        SELECT f.energy, f.dynamics, f.brightness, f.percussive_ratio,
               f.onset_rate, MAX(p.play_count, 1) AS w
        FROM audio_features f
        JOIN profile_tracks p ON p.work_key = f.work_key
        WHERE f.error IS NULL AND p.play_count >= ?
    """, (min_plays,)).fetchall()
    rows = [r for r in rows if all(r[f] is not None for f in TIMBRE_FIELDS)]
    if len(rows) < MIN_PROFILE_TRACKS:
        return None

    profile = {"n": len(rows)}
    for field in TIMBRE_FIELDS:
        values = [r[field] for r in rows]
        weights = [r["w"] for r in rows]
        total = float(sum(weights)) or 1.0
        mean = sum(v * w for v, w in zip(values, weights)) / total
        var = sum(w * (v - mean) ** 2 for v, w in zip(values, weights)) / total
        std = var ** 0.5
        # A degenerate spread would make every candidate infinitely far away.
        profile[field] = (mean, std if std > 1e-9 else abs(mean) * 0.25 + 1e-6)
    return profile


def timbre_similarity(feats, profile):
    """0 to 1. A diagonal Gaussian kernel around his centroid.

    Each feature is scored on its own z-distance, so a track that matches
    his percussion but is far brighter is penalised on brightness alone
    rather than being rejected outright.
    """
    if not profile or not feats:
        return None
    num = den = 0.0
    for field in TIMBRE_FIELDS:
        value = feats.get(field)
        if value is None:
            continue
        mean, std = profile[field]
        z = (value - mean) / std
        weight = TIMBRE_WEIGHTS.get(field, 1.0)
        num += weight * math.exp(-0.5 * z * z)
        den += weight
    if den == 0:
        return None
    return round(num / den, 4)


def load_candidate_features(conn):
    """work_key -> timbre row, for every candidate already analysed."""
    out = {}
    for r in conn.execute("""
        SELECT work_key, energy, dynamics, brightness, percussive_ratio,
               onset_rate FROM audio_features WHERE error IS NULL
    """):
        out[r["work_key"]] = dict(r)
    return out
