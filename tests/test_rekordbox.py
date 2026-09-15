import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from digger import db
from digger.profile import rekordbox

FIXTURE = os.path.join(os.path.dirname(__file__), "fixture_rekordbox.xml")
FAIL = []


def check(label, got, want):
    if got != want:
        FAIL.append("%-48s got=%r want=%r" % (label, got, want))


rows = list(rekordbox.parse(FIXTURE))
check("empty track skipped", len(rows), 6)

by_id = {(r["artist"], r["title"]): r for r in rows}

# Mix and Remixer must be folded back into the title.
titles = sorted(r["title"] for r in rows if r["artist"] == "Adam Beyer")
check("mix folded into title", titles,
      ["Your Mind", "Your Mind (Dax J Remix)", "Your Mind (Extended Mix)"])

plain = [r for r in rows if r["title"] == "Your Mind"][0]
ext = [r for r in rows if r["title"] == "Your Mind (Extended Mix)"][0]
remix = [r for r in rows if r["title"] == "Your Mind (Dax J Remix)"][0]

check("extended == original (track)", ext["track_key"], plain["track_key"])
check("remix differs (track)", remix["track_key"] != plain["track_key"], True)
check("remix same work", remix["work_key"], plain["work_key"])

check("rating 204 -> 4 stars", plain["rating"], 4)
check("rating 255 -> 5 stars", remix["rating"], 5)
check("play_count parsed", plain["play_count"], 42)
check("zero plays kept", [r for r in rows if r["artist"].startswith("Am")][0]["play_count"], 0)
check("bpm float", plain["bpm"], 128.0)
check("key parsed", plain["music_key"], "Am")
check("label parsed", plain["label"], "Drumcode")
check("accented artist kept raw",
      [r for r in rows if r["title"] == "Hypnotised"][0]["artist"], "Amélie Lens")

# Round trip through the database, including the summary query.
conn = db.connect(os.path.join(os.path.dirname(__file__), "..", "data", "test_profile.db"))
conn.execute("DELETE FROM profile_tracks")
summary = rekordbox.load_into(conn, FIXTURE)
check("merged to distinct products", summary["tracks"], 5)
check("files read", summary["files"], 6)
check("one version merged", summary["merged_versions"], 1)
check("played at least once", summary["played_at_least_once"], 4)
check("total plays", summary["total_plays"], 73)

prof = rekordbox.profile_summary(conn, min_plays=1)
check("top artist by plays", prof["top_artists"][0]["artist"], "Adam Beyer")
check("plays summed, not overwritten", prof["most_played"][0]["play_count"], 45)
check("bpm histogram non empty", len(prof["bpm_histogram"]) > 0, True)
check("genre bucket present", any(g["bucket"] == "Techno" for g in prof["genre"]), True)

if FAIL:
    print("FAILED %d" % len(FAIL))
    for f in FAIL:
        print("  " + f)
    sys.exit(1)
print("rekordbox: all checks passed (%d tracks, %d plays)" % (
    summary["tracks"], summary["total_plays"]))
