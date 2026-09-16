import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from digger import db, score

FAIL = []


def check(label, got, want):
    if got != want:
        FAIL.append("%-54s got=%r want=%r" % (label, got, want))


def approx(label, got, want, tol=0.05):
    if got is None or abs(got - want) > tol:
        FAIL.append("%-54s got=%r want~%r" % (label, got, want))


DB = os.path.join(os.path.dirname(__file__), "..", "data", "test_timbre.db")
if os.path.exists(DB):
    os.remove(DB)
conn = db.connect(DB)


def add(work, energy, dyn, bright, perc, onset, plays=1, profile=True):
    conn.execute("""INSERT OR REPLACE INTO audio_features
        (work_key, track_key, origin, energy, dynamics, brightness,
         percussive_ratio, onset_rate)
        VALUES (?,?,?,?,?,?,?,?)""",
        (work, work, "profile" if profile else "cand",
         energy, dyn, bright, perc, onset))
    if profile:
        conn.execute("""INSERT OR REPLACE INTO profile_tracks
            (track_key, work_key, artist, title, play_count, rating)
            VALUES (?,?,?,?,?,0)""", (work, work, "A", work, plays))


# A coherent library: dark, percussive, mid energy. Slight spread so the
# standard deviations are not degenerate.
for i in range(20):
    add("his%02d" % i, 0.20 + i * 0.001, 0.05, 2500 + i * 10,
        0.60 + i * 0.002, 6.0, plays=5)
conn.commit()

prof = score.timbre_profile(conn)
check("profile built", prof is not None, True)
check("profile counts tracks", prof["n"], 20)
approx("centroid energy", prof["energy"][0], 0.21, 0.02)
approx("centroid percussive", prof["percussive_ratio"][0], 0.619, 0.02)

# A track sitting on the centroid should score near 1.
on_centre = {"energy": 0.21, "dynamics": 0.05, "brightness": 2595,
             "percussive_ratio": 0.619, "onset_rate": 6.0}
sim_centre = score.timbre_similarity(on_centre, prof)
approx("centroid track scores high", sim_centre, 1.0, 0.15)

# A track far away on every axis should score low.
far = {"energy": 0.9, "dynamics": 0.4, "brightness": 9000,
       "percussive_ratio": 0.02, "onset_rate": 25.0}
sim_far = score.timbre_similarity(far, prof)
check("distant track scores lower", sim_far < sim_centre, True)
check("distant track is low", sim_far < 0.2, True)

# Matching percussion but wrong brightness must be penalised, not rejected.
partial = {"energy": 0.21, "dynamics": 0.05, "brightness": 9000,
           "percussive_ratio": 0.619, "onset_rate": 6.0}
sim_part = score.timbre_similarity(partial, prof)
check("partial match sits between", sim_far < sim_part < sim_centre, True)

# Guards.
check("no profile -> None", score.timbre_similarity(on_centre, None), None)
check("no features -> None", score.timbre_similarity(None, prof), None)
check("missing fields tolerated",
      score.timbre_similarity({"energy": 0.21}, prof) is not None, True)

# Too few analysed tracks must refuse to build a centroid.
conn.execute("DELETE FROM audio_features WHERE work_key NOT IN "
             "(SELECT work_key FROM audio_features LIMIT 5)")
conn.commit()
check("refuses a thin profile", score.timbre_profile(conn), None)

# affinity falls back to names when timbre is unavailable
a = score.affinity({"artist": "x", "label": None}, {"x": 40}, {},
                   timbre=None, feats=None)
approx("name-only affinity still works", a, 0.75, 0.01)

if FAIL:
    print("FAILED %d" % len(FAIL))
    for f in FAIL:
        print("  " + f)
    sys.exit(1)
print("timbre: all checks passed (centre=%.2f partiel=%.2f loin=%.2f)"
      % (sim_centre, sim_part, sim_far))


# --- rejections are evidence, not just a filter
conn.execute("DELETE FROM audio_features")
conn.execute("DELETE FROM profile_tracks")
conn.execute("DELETE FROM feedback")
for i in range(20):
    add("his%02d" % i, 0.20 + i * 0.001, 0.05, 2500 + i * 10,
        0.60 + i * 0.002, 6.0, plays=5)
# Ten rejected records that share a signature of their own: loud and bright.
for i in range(10):
    add("no%02d" % i, 0.80 + i * 0.002, 0.30, 8000 + i * 20,
        0.05, 20.0, profile=False)
    conn.execute("INSERT INTO feedback (work_key, verdict, why) VALUES (?,?,?)",
                 ("no%02d" % i, "never", "trop EDM"))
conn.commit()

prof2 = score.timbre_profile(conn)
rej = score.rejected_profile(conn)
check("rejected centroid built", rej is not None, True)
check("rejected counts", rej["n"], 10)

near_his = {"energy": 0.21, "dynamics": 0.05, "brightness": 2595,
            "percussive_ratio": 0.619, "onset_rate": 6.0}
like_rejects = {"energy": 0.81, "dynamics": 0.30, "brightness": 8090,
                "percussive_ratio": 0.05, "onset_rate": 20.0}

a_good = score.affinity({"artist": "x", "label": None}, {}, {},
                        timbre=prof2, feats=near_his, rejected=rej)
a_bad = score.affinity({"artist": "x", "label": None}, {}, {},
                       timbre=prof2, feats=like_rejects, rejected=rej)
check("record like his sound still scores", a_good > 0.5, True)
check("record like his rejects is pushed down", a_bad < a_good, True)

# The penalty must discourage, never veto: the same record without the
# rejection history should score higher, but not by collapsing to zero.
a_bad_nopenalty = score.affinity({"artist": "x", "label": None}, {}, {},
                                 timbre=prof2, feats=like_rejects)
check("penalty applied", a_bad <= a_bad_nopenalty, True)

# Too few rejections must not build a centroid.
conn.execute("DELETE FROM feedback WHERE work_key NOT IN ('no00','no01','no02')")
conn.commit()
check("refuses a thin reject profile", score.rejected_profile(conn), None)

if FAIL:
    print("FAILED %d" % len(FAIL))
    for f in FAIL:
        print("  " + f)
    sys.exit(1)
print("rejets: all checks passed (aime=%.2f, ressemble aux rejets=%.2f)"
      % (a_good, a_bad))


# --- a quota must never become a promise the data cannot keep
def cand(key, aff, corr, sources=1):
    return {"work_key": key, "artist": key, "title": key,
            "affinity": aff, "corroboration": corr,
            "score": 0.6 * aff + 0.4 * corr,
            "n_sources": sources, "n_charts": 0, "n_sets": 0}

# Six good ones and a long tail of near-zero, the shape MusicMate showed:
# six matches at the top, then a collapse to relevance 2.
pool = [cand("good%d" % i, 0.8, 0.9) for i in range(6)]
pool += [cand("weak%d" % i, 0.01, 0.02) for i in range(40)]
filled = score.assign_slots(pool)
check("padding refused", len(filled) <= 8, True)
check("the good ones are kept", len([x for x in filled if x["artist"].startswith("good")]), 6)
check("the tail is not shipped",
      [x for x in filled if x["artist"].startswith("weak") and x["slot"] != "wildcard"], [])

short = score.slot_shortfall(filled)
check("shortfall is reported", len(short) > 0, True)
check("shortfall names the slot and the gap",
      all(s["got"] < s["target"] for s in short), True)

# A full pool still fills the whole quota.
rich = [cand("ok%d" % i, 0.7, 0.8) for i in range(40)]
full = score.assign_slots(rich)
check("a real pool fills the quota", len(full), 20)
check("nothing short when the pool is rich", score.slot_shortfall(full), [])

if FAIL:
    print("FAILED %d" % len(FAIL))
    for f in FAIL:
        print("  " + f)
    sys.exit(1)
print("quota: all checks passed (pool maigre -> %d proposes, pool riche -> %d)"
      % (len(filled), len(full)))
