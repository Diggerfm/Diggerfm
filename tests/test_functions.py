import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from digger import db
from digger.profile import rekordbox

FIXTURE = os.path.join(os.path.dirname(__file__), "fixture_rekordbox.xml")
FAIL = []


def check(label, got, want):
    if got != want:
        FAIL.append("%-52s got=%r want=%r" % (label, got, want))


# --- name classification
check("warm up", rekordbox.classify_playlist("Warm Up 2026"), "warmup")
check("peak", rekordbox.classify_playlist("Peak Time"), "peak")
check("closing", rekordbox.classify_playlist("Closing Set"), "after")
check("tools", rekordbox.classify_playlist("Transition Tools"), "tool")
check("signature", rekordbox.classify_playlist("100% Me"), "core")
check("commercial", rekordbox.classify_playlist("Crowd Pleasers"), "commercial")
check("weird", rekordbox.classify_playlist("Bizarre mais genial"), "odd")
check("case insensitive", rekordbox.classify_playlist("PEAK TIME"), "peak")
# A neutral name must stay neutral: guessing would poison the profile.
check("year says nothing", rekordbox.classify_playlist("2024"), None)
check("place says nothing", rekordbox.classify_playlist("Ibiza"), None)
check("empty", rekordbox.classify_playlist(""), None)

# --- walking the tree
by_track, summary = rekordbox.parse_playlists(FIXTURE)
check("playlists found", len(summary), 4)
names = {s["playlist"]: s["function"] for s in summary}
check("warm up tagged", names["Warm Up 2026"], "warmup")
check("peak tagged", names["Peak Time"], "peak")
# The leaf is called "2026" and says nothing; the parent folder is "Closing".
check("function inherited from folder", names["2026"], "after")
check("neutral playlist untagged", names["Ibiza 2019"], None)

check("track 3 in two roles", by_track["3"]["functions"], {"peak", "after"})
check("track 5 only warmup", by_track["5"]["functions"], {"warmup"})
check("track 5 lists both playlists", len(by_track["5"]["playlists"]), 2)

# --- writing onto the profile
DB = os.path.join(os.path.dirname(__file__), "..", "data", "test_functions.db")
if os.path.exists(DB):
    os.remove(DB)
conn = db.connect(DB)
rekordbox.load_into(conn, FIXTURE)
res = rekordbox.load_functions(conn, FIXTURE)
check("playlists counted", res["playlists"], 4)
# "Peak Time" holds track 1 and track 3, which are two distinct
# track_keys (the original and the Dax J remix), so peak counts 2.
check("peak counted", res["by_function"].get("peak"), 2)
check("warmup counted", res["by_function"].get("warmup"), 2)

row = conn.execute(
    "SELECT functions FROM profile_tracks WHERE title LIKE 'Your Mind (Dax%'").fetchone()
check("remix carries both roles", row["functions"], "after,peak")

# "Orphan" sits in no playlist at all and must stay unclassified rather
# than being assigned a role by default.
orphan = conn.execute(
    "SELECT functions FROM profile_tracks WHERE title = 'Orphan'").fetchone()
check("track in no playlist stays untagged", orphan["functions"], None)

# "Rolling" is in a warm-up list and in "Ibiza 2019", which carries no role.
rolling = conn.execute(
    "SELECT functions, playlists FROM profile_tracks WHERE title = 'Rolling'").fetchone()
check("neutral playlist adds no role", rolling["functions"], "warmup")
check("but is still recorded", "Ibiza 2019" in rolling["playlists"], True)

if FAIL:
    print("FAILED %d" % len(FAIL))
    for f in FAIL:
        print("  " + f)
    sys.exit(1)
print("functions: all checks passed (%d playlists, %s)" % (
    res["playlists"], res["by_function"]))
