"""Chart de-duplication.

Corroboration rests on one assumption: that two DJ charts are two
independent opinions. Measured on 2026-09-15 over 120 charts, that
assumption is false often enough to matter.

Five charts, ids 902623 to 902627, all named "Future Glow 2 chart", all
published the same day, carried the same ten tracks. Their owners were
different people (Dream Donor, Audio Vision, Francesco Sisca, Under
Concept) so counting distinct owners does not help, and those same people
are the credited artists on the tracks. A label had its roster chart its
own catalogue.

Uncorrected, every one of those ten records scored as "supported by five
DJs", which is the top of the ranking. So charts are clustered by what they
contain, and a cluster counts once.
"""

import re

SIMILARITY_THRESHOLD = 0.7   # Jaccard over track sets
NAME_NOISE = re.compile(r"\b(chart|charts|top|playlist)\b|\d{4}|[^\w\s]", re.I)


def normalize_chart_name(name):
    if not name:
        return ""
    return re.sub(r"\s+", " ", NAME_NOISE.sub(" ", name)).strip().lower()


def chart_track_sets(conn):
    """chart_id -> set of work_keys it contains."""
    sets = {}
    for r in conn.execute("""
        SELECT json_extract(raw, '$.chart_id') AS cid, work_key
        FROM sightings WHERE source = 'beatport'
          AND json_extract(raw, '$.chart_id') IS NOT NULL
    """):
        sets.setdefault(str(r["cid"]), set()).add(r["work_key"])
    return sets


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


def cluster_charts(conn, threshold=SIMILARITY_THRESHOLD):
    """Group charts that carry substantially the same records.

    Union-find over pairwise Jaccard similarity. Content is used rather than
    the chart name because the names differed in other duplicate groups, and
    two honest DJs can both call theirs "September Chart" without agreeing
    on anything.
    """
    sets = chart_track_sets(conn)
    ids = sorted(sets)
    parent = {i: i for i in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if _jaccard(sets[a], sets[b]) >= threshold:
                union(a, b)

    groups = {}
    for cid in ids:
        groups[cid] = find(cid)
    return groups


def group_map(conn, threshold=SIMILARITY_THRESHOLD):
    """chart_id -> group id, as a plain dict for the scorer."""
    return cluster_charts(conn, threshold)


def report(conn, threshold=SIMILARITY_THRESHOLD):
    """What the clustering collapsed, for inspection."""
    groups = cluster_charts(conn, threshold)
    names = {}
    for r in conn.execute("""
        SELECT DISTINCT json_extract(raw, '$.chart_id') AS cid,
                        json_extract(raw, '$.chart') AS nom
        FROM sightings WHERE source = 'beatport'
    """):
        names[str(r["cid"])] = r["nom"]

    buckets = {}
    for cid, root in groups.items():
        buckets.setdefault(root, []).append(cid)
    collapsed = {k: v for k, v in buckets.items() if len(v) > 1}
    return {
        "charts": len(groups),
        "groups": len(buckets),
        "collapsed": [
            {"kept": names.get(k), "charts": len(v),
             "ids": sorted(v), "names": sorted({names.get(c) or "?" for c in v})}
            for k, v in sorted(collapsed.items(), key=lambda x: -len(x[1]))
        ],
    }
