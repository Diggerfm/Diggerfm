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
