import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from digger.sources.setfinder import edition_tokens, matches_edition

FAIL = []


def check(label, got, want):
    if got != want:
        FAIL.append("%-56s got=%r want=%r" % (label, got, want))


# --- the three real misses this module exists to stop
check("wrong season episode refused",
      matches_edition("Nik Roos & Dave Columbo Jenkins - Noisia's Vision Radio S06E37",
                      "VISION Radio S06E31 // Hosted by Dave Columbo Jenkins"), False)
check("wrong show number refused",
      matches_edition("Aly & Fila - Future Sound Of Egypt 980",
                      "Future Sound of Egypt 728 with Aly & Fila"), False)
check("off-by-one episode refused",
      matches_edition("Alex O'Neill - Novasynth RadioShow #279",
                      "Novasynth RadioShow #278"), False)

# --- and the two real hits it must still accept
check("correct episode accepted",
      matches_edition("Bingo Players & .EXA - Hysteria Radio 546",
                      "Hysteria Radio 546 (.EXA)"), True)
check("hash form accepted",
      matches_edition("Dave Baker - Hot House Hours 335",
                      "#335: feat. Major Lazer, Dimitri Vegas, Timmy Trumpet"), True)

# --- token extraction
check("season episode token", edition_tokens("Vision Radio S06E37"), ["s06e37"])
check("hash token", edition_tokens("Novasynth RadioShow #279"), ["279"])
check("bare number token", edition_tokens("Future Sound Of Egypt 980"), ["980"])
check("trailing date ignored",
      edition_tokens("Hot House Hours 335 2026-09-16"), ["335"])
check("a year in the title is not an edition, but is still a token",
      "2026" in edition_tokens("Anyma @ UNVRS Ibiza 2026-09-15"), False)

# --- a set with no number falls back to word overlap, not to anything goes
check("club date matches itself",
      matches_edition("Mannero @ Boiler Room Vibes, Elephant Beach Club",
                      "Mannero at Elephant Beach Club Boiler Room Vibes"), True)
check("club date refuses a stranger",
      matches_edition("Mannero @ Boiler Room Vibes, Elephant Beach Club",
                      "Adriatique live from Tulum"), False)
check("empty candidate refused",
      matches_edition("Hot House Hours 335", ""), False)

if FAIL:
    print("FAILED %d" % len(FAIL))
    for f in FAIL:
        print("  " + f)
    sys.exit(1)
print("setfinder: all checks passed")
