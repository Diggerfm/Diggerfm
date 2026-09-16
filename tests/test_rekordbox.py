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


# --- the five fields the official XML spec documents and the first parser
#     ignored, plus the cues he named himself
rows2 = list(rekordbox.parse(FIXTURE))
plain2 = [r for r in rows2 if r["title"] == "Your Mind"][0]
ext2 = [r for r in rows2 if r["title"] == "Your Mind (Extended Mix)"][0]
hyp = [r for r in rows2 if r["title"] == "Hypnotised"][0]

check("colour read", plain2["colour"], "0xFF0000")
check("last played read", plain2["last_played"], "2026-08-30")
check("composer read", plain2["composer"], "Adam Beyer")
check("grouping read", plain2["grouping"], "weapons")
check("duration read", plain2["duration"], 412.0)
check("absent colour stays none", [r for r in rows2 if r["title"] == "Rolling"][0]["colour"], None)
check("composer on another track", hyp["composer"], "Amelie Lens")

# Only named cues are kept, and they come back in playing order.
check("named cues only", [c["name"] for c in plain2["cues"]], ["intro", "drop", "break"])
check("cue position kept", plain2["cues"][1]["start"], 96.2)
check("memory cue kept", plain2["cues"][2]["num"], -1)
check("track without cues", hyp["cues"], [])

# Merging the original with its extended edit must keep the LATER play date,
# where date_added keeps the earlier one.
merged2 = {r["track_key"]: r for r in rekordbox.merge_versions(rows2)}
same = merged2[plain2["track_key"]]
check("merge keeps latest play", same["last_played"], "2026-09-10")
check("merge keeps earliest add", same["date_added"], "2020-03-14")
check("merge keeps the richer cues", len(same["cues"]), 3)

# Round trip through the database.
conn.execute("DELETE FROM profile_tracks")
rekordbox.load_into(conn, FIXTURE)
row2 = conn.execute(
    "SELECT colour, last_played, composer, grouping, duration, cues "
    "FROM profile_tracks WHERE title = 'Your Mind'").fetchone()
check("colour stored", row2["colour"], "0xFF0000")
check("composer stored", row2["composer"], "Adam Beyer")
check("cues stored as json", "drop" in (row2["cues"] or ""), True)

if FAIL:
    print("FAILED %d" % len(FAIL))
    for f in FAIL:
        print("  " + f)
    sys.exit(1)
print("champs de la spec: all checks passed")
