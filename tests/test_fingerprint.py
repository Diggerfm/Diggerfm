import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from digger.fingerprint.sets import _collapse

FAIL = []


def check(label, got, want):
    if got != want:
        FAIL.append("%-52s got=%r want=%r" % (label, got, want))


def hit(artist, title):
    return {"artist": artist, "title": title, "isrc": None,
            "label": None, "released": None, "shazam_key": None}


def probes(*spec):
    """spec: (offset, artist|None, title|None)"""
    return [{"offset": o, "hit": (hit(a, t) if a else None)} for o, a, t in spec]


# One record detected across three consecutive probes is ONE play.
p = _collapse(probes((90, "A", "X"), (180, "A", "X"), (270, "A", "X")))
check("three probes, one play", len(p), 1)
check("play span start", p[0]["start"], 90)
check("play span end", p[0]["end"], 270)
check("probe count kept", p[0]["probes"], 3)

# Two different records are two plays.
p = _collapse(probes((90, "A", "X"), (180, "B", "Y")))
check("two records, two plays", len(p), 2)

# A single miss inside a play does not split it (blend or breakdown).
p = _collapse(probes((90, "A", "X"), (180, None, None), (270, "A", "X")))
check("one gap tolerated", len(p), 1)
check("gap play spans through", p[0]["end"], 270)

# Three consecutive misses end the play.
p = _collapse(probes((90, "A", "X"), (180, None, None), (270, None, None),
                     (360, None, None), (450, "A", "X")))
check("long gap splits", len(p), 2)

# Same record played twice with another track between it counts twice.
p = _collapse(probes((90, "A", "X"), (180, "B", "Y"), (270, "A", "X")))
check("replay counted twice", len(p), 3)

# Version differences collapse at work level: the extended edit is the record.
p = _collapse(probes((90, "A", "X"), (180, "A", "X (Extended Mix)")))
check("extended is same work", len(p), 1)

# A remix is a different work and must split.
p = _collapse(probes((90, "A", "X"), (180, "A", "X (Dax J Remix)")))
check("remix splits", len(p), 2)

# Nothing recognised at all.
check("all misses", _collapse(probes((90, None, None), (180, None, None))), [])
check("empty input", _collapse([]), [])

if FAIL:
    print("FAILED %d" % len(FAIL))
    for f in FAIL:
        print("  " + f)
    sys.exit(1)
print("fingerprint collapse: all checks passed")


# --- gap detection, which drives the refine pass
from digger.fingerprint.sets import find_gaps

g = find_gaps(probes((90, "A", "X"), (180, None, None), (270, None, None),
                     (360, "B", "Y")))
check("one gap found", g, [(180, 270)])

# A single miss is not a gap: tracks blend, and chasing every one would
# double the probe count for nothing.
g = find_gaps(probes((90, "A", "X"), (180, None, None), (270, "B", "Y")))
check("single miss ignored", g, [])

g = find_gaps(probes((90, None, None), (180, None, None), (270, "A", "X"),
                     (360, None, None), (450, None, None), (540, None, None)))
check("gaps at both ends", g, [(90, 180), (360, 540)])

check("no results, no gaps", find_gaps([]), [])
check("all matched, no gaps",
      find_gaps(probes((90, "A", "X"), (180, "B", "Y"))), [])
g = find_gaps(probes((90, None, None), (180, None, None)))
check("everything missed is one gap", g, [(90, 180)])

if FAIL:
    print("FAILED %d" % len(FAIL))
    for f in FAIL:
        print("  " + f)
    sys.exit(1)
print("fingerprint gaps: all checks passed")
