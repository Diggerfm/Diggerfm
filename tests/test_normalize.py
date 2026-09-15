import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from digger.normalize import (
    work_key, track_key, normalize_artist, normalize_title,
    normalize_version, split_version, parse_combined,
)

FAIL = []

def check(label, got, want):
    if got != want:
        FAIL.append("%-52s got=%r want=%r" % (label, got, want))

# --- same record, different source spellings must collapse
check("case/space",
      work_key("Adam Beyer", "Your Mind") == work_key("ADAM  BEYER ", "your mind"), True)
check("en dash version vs paren",
      work_key("Adam Beyer", "Your Mind (Original Mix)") == work_key("Adam Beyer", "Your Mind"), True)
check("extended collapses at work level",
      work_key("Adam Beyer", "Your Mind (Extended Mix)") == work_key("Adam Beyer", "Your Mind"), True)
check("label bracket ignored",
      work_key("Adam Beyer", "Your Mind [Drumcode]") == work_key("Adam Beyer", "Your Mind"), True)
check("accents folded",
      work_key("Amelie Lens", "Hypnotised") == work_key("Amélie Lens", "Hypnotised"), True)
check("collaborator order",
      work_key("A & B", "Track") == work_key("B & A", "Track"), True)
check("comma vs ampersand",
      work_key("A, B", "Track") == work_key("A & B", "Track"), True)
check("feat stripped from artist",
      work_key("Kerri Chandler feat. Dana", "Track") == work_key("Kerri Chandler", "Track"), True)
check("feat stripped from title",
      work_key("X", "Track feat. Dana") == work_key("X", "Track"), True)

# --- a remix is NOT the same product, but IS the same work
check("remix differs at track level",
      track_key("X", "Track (Someone Remix)") != track_key("X", "Track"), True)
check("remix same at work level",
      work_key("X", "Track (Someone Remix)") == work_key("X", "Track"), True)
check("two different remixes differ",
      track_key("X", "Track (A Remix)") != track_key("X", "Track (B Remix)"), True)
check("extended == original at track level",
      track_key("X", "Track (Extended Mix)") == track_key("X", "Track (Original Mix)"), True)

# --- parts
check("split_version plain", split_version("Track"), ("Track", ""))
check("split_version remix", split_version("Track (Dax J Remix)"), ("Track", "dax j remix"))
check("split_version feat not version", split_version("Track (feat. Dana)"), ("Track (feat. Dana)", ""))
check("normalize_version neutral", normalize_version("Track (Original Mix)"), "")
check("normalize_version remix", normalize_version("Track (Dax J Remix)"), "dax j remix")
check("normalize_artist sorted", normalize_artist("Zed & Alpha"), "alpha zed")
check("normalize_title clean", normalize_title("Your  Mind! (Extended Mix)"), "your mind")

# --- tracklist line parsing
check("parse hyphen", parse_combined("Adam Beyer - Your Mind"), ("Adam Beyer", "Your Mind"))
check("parse en dash", parse_combined("Adam Beyer – Your Mind"), ("Adam Beyer", "Your Mind"))
check("parse no artist", parse_combined("ID"), ("", "ID"))

# --- guards against over-collapsing
check("different titles differ", work_key("X", "Alpha") != work_key("X", "Beta"), True)
check("different artists differ", work_key("X", "Track") != work_key("Y", "Track"), True)

if FAIL:
    print("FAILED %d" % len(FAIL))
    for f in FAIL:
        print("  " + f)
    sys.exit(1)
print("normalize: all checks passed")
