import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from digger import db, feedback

FAIL = []


def check(label, got, want):
    if got != want:
        FAIL.append("%-52s got=%r want=%r" % (label, got, want))


# --- verdict aliases
check("canonical", feedback.normalize_verdict("fire"), "fire")
check("french alias", feedback.normalize_verdict("enorme"), "fire")
check("french never", feedback.normalize_verdict("jamais"), "never")
check("numeric", feedback.normalize_verdict("0"), "never")
check("case and space", feedback.normalize_verdict("  KEEP "), "keep")
check("unknown word", feedback.normalize_verdict("peutetre"), None)
check("empty", feedback.normalize_verdict(""), None)
check("placeholder", feedback.normalize_verdict("???"), None)

# --- parsing a filled file
SAMPLE = os.path.join(os.path.dirname(__file__), "..", "data", "test_fb.md")
with open(SAMPLE, "w", encoding="utf-8") as fh:
    fh.write("""# Digest test

## BULLSEYE

Artist One - Track One
  - [a::one] fire | groove roulant, exactement moi

Artist Two - Track Two
  - [b::two] never | trop EDM

Artist Three - Track Three
  - [c::three] ??? |

Artist Four - Track Four
  - [d::four] enorme | bassline parfaite

Artist Five - Track Five
  - [e::five] peutetre | je sais pas

Artist Six - Track Six
  - [f::six] keep |
""")

DB = os.path.join(os.path.dirname(__file__), "..", "data", "test_fb.db")
if os.path.exists(DB):
    os.remove(DB)
conn = db.connect(DB)

res = feedback.import_file(conn, SAMPLE)
check("recorded", res["recorded"], 4)
# The untouched "???" line must be counted, not silently dropped.
check("unanswered counted", res["unanswered"], 1)
check("unrecognised counted", res["unrecognised"], 1)

rows = {r["work_key"]: r for r in conn.execute("SELECT * FROM feedback")}
check("fire stored", rows["a::one"]["verdict"], "fire")
check("reason stored", rows["a::one"]["why"], "groove roulant, exactement moi")
check("alias resolved", rows["d::four"]["verdict"], "fire")
check("verdict without reason", rows["f::six"]["verdict"], "keep")
check("empty reason is null", rows["f::six"]["why"], None)
check("unknown word not stored", "e::five" in rows, False)
check("placeholder not stored", "c::three" in rows, False)

s = feedback.summary(conn)
check("summary total", s["total"], 4)
check("summary reasons", s["with_reason"], 3)

# --- learning refuses on thin evidence rather than fitting noise
check("refuses to learn from 4 opinions", feedback.learn_weights(conn), None)

# --- direct recording
feedback.record(conn, "z::nine", "meh", "correct sans plus")
check("direct record", feedback.summary(conn)["total"], 5)
try:
    feedback.record(conn, "z::ten", "n_importe_quoi")
    FAIL.append("bad verdict should raise")
except ValueError:
    pass

if FAIL:
    print("FAILED %d" % len(FAIL))
    for f in FAIL:
        print("  " + f)
    sys.exit(1)
print("feedback: all checks passed")
