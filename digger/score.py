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


RECENCY_HALF_LIFE_DAYS = 540   # about eighteen months


def profile_vectors(conn):
    """Artists and labels he plays, weighted by play count and by recency.

    Play count alone mistakes history for taste. Forty plays last touched in
    2019 was a weapon once; ten plays last month is what he plays now. The
    XML carries LastPlayed for exactly this and the first parser ignored it,
    so each track's weight decays on a half-life: full weight if played
    recently, half after eighteen months, never zero because an old favourite
    is still evidence.
    """
    artists, labels = {}, {}
    decay = """
        CASE WHEN last_played IS NULL OR last_played = '' THEN 0.5
             ELSE MAX(0.25, POWER(0.5,
                  (JULIANDAY('now') - JULIANDAY(SUBSTR(last_played, 1, 10)))
                  / %d.0))
        END
    """ % RECENCY_HALF_LIFE_DAYS
    for r in conn.execute("""
        SELECT LOWER(artist) a,
               SUM((COALESCE(play_count, 0) + 1) * (%s)) w
        FROM profile_tracks GROUP BY LOWER(artist)
    """ % decay):
        artists[r["a"]] = r["w"] or 0
    for r in conn.execute("""
        SELECT LOWER(label) l,
               SUM((COALESCE(play_count, 0) + 1) * (%s)) w
        FROM profile_tracks WHERE label IS NOT NULL GROUP BY LOWER(label)
    """ % decay):
        labels[r["l"]] = r["w"] or 0
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


def affinity(cand, artists, labels, timbre=None, feats=None, rarity=None,
             rejected=None):
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

    blended = 0.65 * sound_score + 0.35 * name_score

    # Distance from what he has turned down, subtracted rather than ignored.
    # Capped at half: a record can resemble his rejects on these five
    # measures and still be one he wants, so this discourages and never
    # vetoes.
    if rejected and feats:
        away = timbre_similarity(feats, rejected)
        if away is not None:
            blended -= 0.5 * away
    return round(max(0.0, min(1.0, blended)), 4)


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
    rejected_sound = rejected_profile(conn)
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
                                 rarity=rarity, rejected=rejected_sound)
        c["corroboration"] = corroboration(c)
        c["score"] = 0.6 * c["affinity"] + 0.4 * c["corroboration"]
        scored.append(c)
    scored.sort(key=lambda x: (-x["score"], -x["n_sources"]))
    return scored


# A candidate has to clear this to be worth his time. Below it the slot is
# left short rather than filled.
#
# MusicMate answers a prompt very close to his brief with twenty tracks
# whose own relevance column reads 75, 75, 75, 75, 75, 75, then 18, 16, 15,
# 14, 12, 11, 9, 9, 9, 8, 8, 4, 4, 2. Six matches, fourteen of padding, and
# their interface colours the padding red. The promise of "20 tracks" beat
# the truth of six.
#
# Six right records beat twenty where fourteen are filler, because filler
# costs him the one thing the tool is meant to save: the time to listen.
SLOT_FLOOR = {
    "bullseye": 0.35,      # squarely his sound, or not his sound
    "modernisation": 0.20,
    "bridge": 0.12,        # deliberately loose: a bridge is not meant to be him
    "wildcard": 0.0,       # a gamble has no floor by definition
}


def assign_slots(scored, quota=None, floors=None):
    """Fill the imposed quota rather than taking the top N.

    bullseye takes the highest affinity, modernisation the corroborated ones
    that are less close, bridge the corroborated ones furthest from him, and
    wildcard is drawn from the tail so the list cannot become a monoculture.
    """
    quota = quota or SLOT_QUOTA
    floors = SLOT_FLOOR if floors is None else floors
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
        floor = floors.get(slot, 0.0)
        # The floor is what keeps a quota from becoming a promise the data
        # cannot keep. A slot that cannot be filled above it stays short.
        clears = (lambda c, f=floor: (c.get("score") or 0) >= f
                  or (c.get("affinity") or 0) >= f)

        if slot == "bullseye":
            got = take(n, lambda c: -c["affinity"],
                       lambda c: (c.get("affinity") or 0) >= floor)
        elif slot == "modernisation":
            got = take(n, lambda c: -c["score"],
                       lambda c: c["n_sources"] >= 1 and clears(c))
        elif slot == "bridge":
            got = take(n, lambda c: (-c["corroboration"], c["affinity"]), clears)
        else:
            mid = pool[len(pool) // 3:] if len(pool) > 6 else pool
            got = [c for c in mid if clears(c)][:n]
            for c in got:
                pool.remove(c)
        for rank, c in enumerate(got, 1):
            c["slot"] = slot
            c["rank"] = rank
            c["slot_target"] = n
            out.append(c)
    return out


def slot_shortfall(slots, quota=None):
    """Which slots came up short, so the page can say so instead of padding."""
    quota = quota or SLOT_QUOTA
    got = {}
    for s in slots:
        got[s["slot"]] = got.get(s["slot"], 0) + 1
    return [{"slot": name, "got": got.get(name, 0), "target": n}
            for name, n in quota if got.get(name, 0) < n]


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
    "onset_rate": 0.74,
    "dynamics": 0.69,
    "percussive_ratio": 0.27,
    "brightness": 0.20,
}

# Re-derived on 2026-09-15 once the analysis backlog cleared, over 702
# tracks in his territory against 189 controls instead of 124 against 96.
# The ordering held, which is the part that mattered:
#
#     field              d at n=220   d at n=891
#     energy                   0.99         0.89
#     onset_rate               0.57         0.44
#     dynamics                 0.42         0.41
#     percussive_ratio         0.26         0.16
#     brightness               0.02         0.12
#
# percussive_ratio is the one to distrust. It read 0.55 on a narrow genre
# set, 0.26 on a broader one and 0.16 on the full population: an effect
# that shrinks every time the sample grows is sample noise, not a property
# of the music. It is kept at a low weight rather than removed because his
# own verdicts, not genre labels, are what should settle it.

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


def rejected_profile(conn, min_rejected=8):
    """The sound he has turned down, as its own centre of gravity.

    Until now a "jamais" only removed that one record. Five rejections that
    share a signature said nothing about the sixth. Borrowed from
    kristopolous/Mutiny, whose pipeline.py carries positive and negative
    preferences side by side rather than using the negative ones only as a
    filter.

    Returns None below min_rejected: a centroid fitted on three opinions
    would push good records away for no reason.
    """
    rows = conn.execute("""
        SELECT f.energy, f.dynamics, f.brightness, f.percussive_ratio,
               f.onset_rate, 1 AS w
        FROM feedback fb
        JOIN audio_features f ON f.work_key = fb.work_key
        WHERE fb.verdict = 'never' AND f.error IS NULL
        GROUP BY fb.work_key
    """).fetchall()
    rows = [r for r in rows if all(r[f] is not None for f in TIMBRE_FIELDS)]
    if len(rows) < min_rejected:
        return None

    profile = {"n": len(rows)}
    for field in TIMBRE_FIELDS:
        values = [r[field] for r in rows]
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / len(values)
        std = var ** 0.5
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
