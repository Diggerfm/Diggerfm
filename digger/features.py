"""Audio features computed locally.

This is the layer the premise needed. Until now affinity was measured on
artist and label names, which is genre matching wearing a disguise, and the
brief refused exactly that: he described groove, bassline, organic
percussion and tension, not a genre.

Nothing here calls a paid service. Bandcamp serves an mp3-128 preview for
every release and librosa does the rest on this machine.

Scope note: these are Bandcamp's own public preview streams. The same is
deliberately NOT done to SoundCloud, whose API terms forbid processing their
audio outside the API.
"""

import gc
import os
import shutil
import tempfile
import urllib.request

import librosa
import numpy as np

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120"

# Krumhansl-Schmuckler key profiles.
MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
                          2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
                          2.54, 4.75, 3.98, 2.69, 3.17, 3.17])

NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _build_camelot():
    """The Camelot wheel, generated rather than typed.

    1A is Ab minor and 1B is B major; each step clockwise rises a fifth.
    Writing the 24 entries by hand invites a typo that silently ruins
    harmonic matching, so they are derived.
    """
    wheel = {}
    for i in range(12):
        wheel[((8 + 7 * i) % 12, True)] = "%dA" % (i + 1)
        wheel[((11 + 7 * i) % 12, False)] = "%dB" % (i + 1)
    return wheel


CAMELOT = _build_camelot()

# A DJ set lives here. Tempo detection routinely halves or doubles, so
# results are folded back into this range.
BPM_MIN, BPM_MAX = 70.0, 180.0

# Club music clusters around this. librosa's default beat_track prior is 120
# but it still halves often: measured on a real preview it returned 63.0
# where both a 128 prior and autocorrelation agreed on ~122. The prior below
# is used to pick between octave variants, not to force a result.
BPM_CENTRE = 128.0

# Below this the estimate was wrong on every validation track.
BPM_CONFIDENCE_FLOOR = 0.90

# Halved after the first full batch was killed for memory pressure on a
# machine with 4 GB free. Thirty seconds of the body of a track carries
# the same timbre and costs half the arrays.
ANALYSIS_SECONDS = 30
SKIP_FRACTION = 0.25       # start a quarter in, past the intro
SAMPLE_RATE = 22050


def fold_bpm(bpm):
    """Bring an octave-confused tempo back into the plausible range."""
    if not bpm or bpm <= 0 or not np.isfinite(bpm):
        return None
    while bpm < BPM_MIN:
        bpm *= 2
    while bpm > BPM_MAX:
        bpm /= 2
    return round(float(bpm), 1)


def _octave_variants(bpm):
    """The same pulse read at half, single and double time."""
    out = []
    for factor in (0.5, 1.0, 2.0):
        v = bpm * factor
        if BPM_MIN <= v <= BPM_MAX:
            out.append(v)
    return out or [fold_bpm(bpm)]


def estimate_bpm(y, sr):
    """Two independent estimates, reconciled.

    beat_track with a club prior and onset autocorrelation fail in different
    ways, so they are run separately and the octave variant that puts them
    closest together, and nearest the club centre, wins. Agreement between
    the two after folding is reported as confidence, so a track the analysis
    is unsure about can be ignored downstream instead of trusted blindly.
    """
    beat = float(np.atleast_1d(
        librosa.beat.beat_track(y=y, sr=sr, start_bpm=BPM_CENTRE)[0])[0])
    onset_env = librosa.onset.onset_strength(y=y, sr=sr)
    auto = float(np.atleast_1d(librosa.feature.rhythm.tempo(
        onset_envelope=onset_env, sr=sr, aggregate=None)).mean())

    best = None
    for b in _octave_variants(beat):
        for a in _octave_variants(auto):
            if b is None or a is None:
                continue
            # closest pair, tie-broken by proximity to the club centre
            cost = abs(b - a) + 0.15 * abs((b + a) / 2 - BPM_CENTRE)
            if best is None or cost < best[0]:
                best = (cost, b, a)
    if best is None:
        return fold_bpm(beat), 0.0, round(beat, 1), round(auto, 1)

    _cost, b, a = best
    value = (b + a) / 2
    spread = abs(b - a) / max(value, 1.0)
    confidence = round(max(0.0, 1.0 - spread * 10), 3)
    return round(value, 1), confidence, round(beat, 1), round(auto, 1)


def detect_key(y, sr):
    """Krumhansl-Schmuckler over the averaged chroma.

    Returns (key_name, is_minor, camelot, confidence). Confidence is the
    margin between the best and second-best correlation, so a track with no
    clear tonal centre reports low rather than asserting a wrong key.
    """
    # chroma_stft rather than chroma_cqt: the constant-Q transform is by far
    # the heaviest operation in this module and the key it yields is not
    # trusted anyway (5/12 against Beatport), so its cost buys nothing.
    chroma = librosa.feature.chroma_stft(y=y, sr=sr)
    profile = chroma.mean(axis=1)
    del chroma
    if profile.sum() <= 0:
        return None, None, None, 0.0
    profile = profile / profile.sum()

    scores = []
    for pc in range(12):
        for is_minor, template in ((False, MAJOR_PROFILE), (True, MINOR_PROFILE)):
            rotated = np.roll(template, pc)
            rotated = rotated / rotated.sum()
            corr = float(np.corrcoef(profile, rotated)[0, 1])
            if np.isfinite(corr):
                scores.append((corr, pc, is_minor))
    if not scores:
        return None, None, None, 0.0

    scores.sort(reverse=True)
    best, pc, is_minor = scores[0]
    second = scores[1][0] if len(scores) > 1 else 0.0
    name = "%s %s" % (NOTES[pc], "minor" if is_minor else "major")
    confidence = round(max(0.0, best - second), 3)
    return name, is_minor, CAMELOT.get((pc, is_minor)), confidence


def analyze_file(path):
    """Extract the features a DJ actually reasons with."""
    total = librosa.get_duration(path=path)
    offset = max(0.0, min(total * SKIP_FRACTION, max(0.0, total - ANALYSIS_SECONDS)))
    y, sr = librosa.load(path, sr=SAMPLE_RATE, mono=True,
                         offset=offset, duration=ANALYSIS_SECONDS)
    if y.size == 0:
        raise ValueError("empty audio")

    bpm, bpm_conf, bpm_beat, bpm_auto = estimate_bpm(y, sr)

    key_name, is_minor, camelot, key_conf = detect_key(y, sr)

    rms = librosa.feature.rms(y=y)[0]
    centroid = librosa.feature.spectral_centroid(y=y, sr=sr)[0]

    # Harmonic/percussive split: a proxy for "organic percussion" versus a
    # wall of synth, which is one of the things he described wanting.
    harmonic, percussive = librosa.effects.hpss(y)
    h_energy = float(np.mean(harmonic ** 2))
    p_energy = float(np.mean(percussive ** 2))
    percussive_ratio = p_energy / (h_energy + p_energy) if (h_energy + p_energy) > 0 else None

    onsets = librosa.onset.onset_detect(y=y, sr=sr, units="time")
    onset_rate = len(onsets) / (len(y) / sr) if len(y) else 0.0

    # Measured against Beatport ground truth on 12 tracks: BPM is right to
    # within 3 above 0.90 confidence and wrong every time below it, so the
    # value is only surfaced when it clears the bar. Key came out at 5/12
    # even counting enharmonic spellings as correct, which is useless for
    # harmonic mixing, so it is stored but never presented as fact. Beatport
    # supplies both fields authoritatively anyway; the timbre block below is
    # what no source sells.
    return {
        "bpm": bpm if bpm_conf >= BPM_CONFIDENCE_FLOOR else None,
        "bpm_confidence": bpm_conf,
        "bpm_unfiltered": bpm,
        "bpm_beat": bpm_beat,
        "bpm_auto": bpm_auto,
        "music_key": key_name,
        "camelot": camelot,
        "key_confidence": key_conf,
        "key_reliable": False,
        "energy": round(float(np.mean(rms)), 5),
        "dynamics": round(float(np.std(rms)), 5),
        "brightness": round(float(np.mean(centroid)), 1),
        "percussive_ratio": round(percussive_ratio, 4) if percussive_ratio else None,
        "onset_rate": round(float(onset_rate), 3),
        "duration": round(float(total), 1),
    }


def analyze_url(url, timeout=60):
    """Download a preview to a temp file, analyse it, always clean up."""
    fd, tmp = tempfile.mkstemp(suffix=".mp3", prefix="digger_feat_")
    os.close(fd)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        # Streamed in chunks: resp.read() held whole files in memory.
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as fh:
            shutil.copyfileobj(resp, fh, 64 * 1024)
        if os.path.getsize(tmp) < 10000:
            raise ValueError("preview too small (%d bytes)" % os.path.getsize(tmp))
        return analyze_file(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# --- Batch runners ------------------------------------------------------

FEATURE_COLUMNS = ("bpm", "bpm_confidence", "music_key", "camelot",
                   "key_confidence", "energy", "dynamics", "brightness",
                   "percussive_ratio", "onset_rate", "duration")


def _store(conn, work_key, track_key, origin, feats=None, error=None):
    row = {"work_key": work_key, "track_key": track_key, "origin": origin,
           "error": error}
    for col in FEATURE_COLUMNS:
        row[col] = (feats or {}).get(col)
    with conn:
        conn.execute("""
            INSERT OR REPLACE INTO audio_features
              (work_key, track_key, origin, bpm, bpm_confidence, music_key,
               camelot, key_confidence, energy, dynamics, brightness,
               percussive_ratio, onset_rate, duration, error)
            VALUES (:work_key, :track_key, :origin, :bpm, :bpm_confidence,
                    :music_key, :camelot, :key_confidence, :energy, :dynamics,
                    :brightness, :percussive_ratio, :onset_rate, :duration,
                    :error)
        """, row)


def analyze_pending(conn, limit=100, source=None, on_done=None):
    """Analyse candidate previews that have no features yet.

    Failures are recorded rather than raised, so a dead preview URL costs one
    row and never blocks the batch or gets retried forever.
    """
    where = "AND s.source = ?" if source else ""
    params = [source] if source else []
    rows = conn.execute("""
        SELECT s.work_key, s.track_key, s.artist, s.title, s.source,
               MAX(s.stream_url) AS stream_url
        FROM sightings s
        LEFT JOIN audio_features f ON f.work_key = s.work_key
        WHERE s.stream_url IS NOT NULL AND f.work_key IS NULL %s
        GROUP BY s.work_key
        LIMIT ?
    """ % where, params + [limit]).fetchall()

    done = failed = 0
    for r in rows:
        try:
            feats = analyze_url(r["stream_url"])
            _store(conn, r["work_key"], r["track_key"], r["source"], feats=feats)
            done += 1
            status = "ok"
        except Exception as exc:
            _store(conn, r["work_key"], r["track_key"], r["source"],
                   error=str(exc)[:200])
            failed += 1
            feats, status = None, str(exc)[:60]
        if on_done:
            on_done(r, feats, status)
        gc.collect()
    return {"analyzed": done, "failed": failed, "total": len(rows)}


def analyze_profile(conn, limit=200, min_plays=0, on_done=None):
    """Analyse his own library from the real files, not from previews.

    The Rekordbox XML gives a path per track, so his ADN is measured on the
    full recordings he actually owns. Most played first: if the batch is cut
    short, the tracks that define him are already done.
    """
    rows = conn.execute("""
        SELECT p.track_key, p.work_key, p.artist, p.title, p.location,
               p.play_count
        FROM profile_tracks p
        LEFT JOIN audio_features f ON f.work_key = p.work_key
        WHERE p.location IS NOT NULL AND f.work_key IS NULL
          AND p.play_count >= ?
        ORDER BY p.play_count DESC, p.rating DESC
        LIMIT ?
    """, (min_plays, limit)).fetchall()

    done = failed = missing = 0
    for r in rows:
        path = r["location"]
        if not path or not os.path.exists(path):
            _store(conn, r["work_key"], r["track_key"], "profile",
                   error="file not found")
            missing += 1
            if on_done:
                on_done(r, None, "fichier absent")
            continue
        try:
            feats = analyze_file(path)
            _store(conn, r["work_key"], r["track_key"], "profile", feats=feats)
            done += 1
            status = "ok"
        except Exception as exc:
            _store(conn, r["work_key"], r["track_key"], "profile",
                   error=str(exc)[:200])
            failed += 1
            feats, status = None, str(exc)[:60]
        if on_done:
            on_done(r, feats, status)
    return {"analyzed": done, "failed": failed, "missing_files": missing,
            "total": len(rows)}


def coverage(conn):
    """What fraction of each source has usable features."""
    out = {}
    for r in conn.execute("""
        SELECT origin,
               COUNT(*) AS n,
               SUM(CASE WHEN error IS NULL THEN 1 ELSE 0 END) AS ok,
               SUM(CASE WHEN bpm IS NOT NULL THEN 1 ELSE 0 END) AS with_bpm,
               ROUND(AVG(energy), 4) AS avg_energy,
               ROUND(AVG(percussive_ratio), 3) AS avg_percussive,
               ROUND(AVG(brightness), 0) AS avg_brightness
        FROM audio_features GROUP BY origin
    """):
        out[r["origin"]] = dict(r)
    return out
