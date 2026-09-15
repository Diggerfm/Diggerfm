"""Turn a DJ set into sightings.

This is the source the whole project was missing. A Beatport chart is what a
DJ says he supports; a fingerprinted set is what he actually played, which
is a stronger claim and cannot be gamed by promo.

Consecutive probes matching the same record are one play, not three, so they
collapse into a single sighting carrying the played span. Corroboration then
reads naturally: how many different sets played this track.
"""

import hashlib
import os
import shutil
import tempfile

from ..normalize import normalize_version, track_key, work_key
from . import audio, identify


# A play is considered over after this much unrecognised audio. Measured in
# seconds rather than in probes: the refine pass leaves probes at mixed
# densities, and a tolerance of "two probes" then means 180 seconds in the
# coarse stretches but only 60 in the refined ones. That split single plays
# into three, for instance CULT - Ortofon reported at 26:30, 37:30 and
# 69:30 when it was one long play plus one reprise.
# 200 s absorbs exactly one missed coarse probe (a 90 s step leaves a
# 180 s hole between two hits, which is the ordinary breakdown case) and
# refuses two (270 s), which is long enough to be another record.
GAP_TOLERANCE_SECONDS = 200


def _collapse(results, gap_tolerance=None, gap_seconds=GAP_TOLERANCE_SECONDS):
    """Merge runs of probes matching the same record into single plays.

    Runs are keyed on track_key, not work_key: a remix played straight after
    its original is two tracks, and merging them would credit the play to
    the wrong record. Version noise still collapses, because an extended
    edit and the original share a track_key by construction.

    gap_seconds allows unrecognised audio inside a play, which happens
    whenever a probe lands on a breakdown or on a blend. gap_tolerance is
    kept for callers that still count probes.
    """
    plays = []
    current = None
    last_hit_at = None
    for r in results:
        hit = r["hit"]
        if hit is None:
            continue
        wk = work_key(hit["artist"], hit["title"])
        tk = track_key(hit["artist"], hit["title"])
        gap = (r["offset"] - last_hit_at) if last_hit_at is not None else 0
        limit = gap_seconds
        if gap_tolerance is not None and gap_seconds is None:
            limit = None
        same_play = (current is not None
                     and current["track_key"] == tk
                     and (limit is None or gap <= limit))
        if same_play:
            current["end"] = r["offset"]
            current["probes"] += 1
        else:
            if current:
                plays.append(current)
            current = {
                "work_key": wk, "track_key": tk,
                "start": r["offset"], "end": r["offset"],
                "probes": 1, **hit,
            }
        last_hit_at = r["offset"]
    if current:
        plays.append(current)
    return plays


def _set_id(url):
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def find_gaps(results, min_run=2):
    """Stretches where nothing was recognised, as (start, end) seconds.

    A run of consecutive misses is either unreleased material or a stretch
    the coarse pass simply stepped over. Measured on a 74 minute Boiler
    Room: a 20 minute gap at 90 second spacing looked like unreleased dubs
    and was not. Re-probing it at 30 seconds found five records, four of
    them missed entirely by the coarse pass. So a gap is a question, not a
    conclusion.
    """
    gaps = []
    run = []
    for r in results:
        if r["hit"] is None:
            run.append(r["offset"])
        else:
            if len(run) >= min_run:
                gaps.append((run[0], run[-1]))
            run = []
    if len(run) >= min_run:
        gaps.append((run[0], run[-1]))
    return gaps


def _probe_range(path, workdir, start, end, every, slice_len):
    """Cut probes across one window only."""
    import subprocess

    made = []
    for off in range(int(start), int(end) + 1, int(every)):
        wav = os.path.join(workdir, "r%06d_%d.wav" % (off, slice_len))
        if not os.path.exists(wav):
            subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-y",
                 "-ss", str(off), "-t", str(slice_len), "-i", path,
                 "-ac", "1", "-ar", "16000", wav],
                check=True, timeout=120)
        made.append((off, wav))
    return made


def fingerprint_set(url, set_label=None, probe_every=audio.DEFAULT_PROBE_EVERY,
                    max_slices=None, max_seconds=None, delay=identify.DEFAULT_DELAY,
                    workdir=None, keep_audio=False, on_result=None,
                    refine_every=30, refine=True):
    """Identify a set and return (sightings, stats).

    Two passes rather than one uniform sweep. The coarse pass covers the set
    cheaply; the refine pass then spends probes only on the stretches where
    the coarse pass found nothing. Uniform 30 second spacing over this same
    set would cost 148 probes; coarse-then-refine costs about 110 and puts
    them where they are needed.

    refine=False restores the single-pass behaviour.
    """
    tmp = workdir or tempfile.mkdtemp(prefix="digger_fp_")
    sid = _set_id(url)
    try:
        path = audio.download(url, tmp, max_seconds=max_seconds)
        slices = audio.slice_windows(path, tmp, probe_every=probe_every,
                                     max_slices=max_slices)
        results = identify.identify(slices, delay=delay, on_result=on_result)
        n_coarse = len(results)

        n_refined = 0
        if refine and refine_every and refine_every < probe_every:
            extra = []
            for start, end in find_gaps(results):
                # Reach one coarse step past each edge: a track can start
                # just inside the gap.
                lo = max(0, start - probe_every + refine_every)
                hi = end + probe_every - refine_every
                extra.extend(_probe_range(path, tmp, lo, hi, refine_every,
                                          audio.DEFAULT_SLICE_LEN))
            seen = {o for o, _ in slices}
            extra = [(o, w) for o, w in extra if o not in seen]
            if extra:
                n_refined = len(extra)
                results.extend(identify.identify(extra, delay=delay,
                                                 on_result=on_result))
                results.sort(key=lambda r: r["offset"])

        plays = _collapse(results)

        sightings = []
        for p in plays:
            title = p["title"]
            sightings.append({
                "source": "setlist",
                "source_id": "%s#%s" % (sid, p["work_key"]),
                "track_key": p["track_key"],
                "work_key": p["work_key"],
                "artist": p["artist"],
                "title": title,
                "version": normalize_version(title),
                "url": url,
                "stream_url": None,
                "genre": None,
                "label": p.get("label"),
                "location": None,
                "published_at": p.get("released"),
                "isrc": p.get("isrc"),
                "bpm": None,
                "music_key": None,
                "raw": {"set": set_label or url, "start": p["start"],
                        "end": p["end"], "probes": p["probes"]},
            })

        matched = sum(1 for r in results if r["hit"])
        stats = {
            "url": url,
            "probes": len(results),
            "probes_coarse": n_coarse,
            "probes_refined": n_refined,
            "matched": matched,
            "match_rate": round(matched / max(len(results), 1), 3),
            "plays": len(plays),
            "tracks": len(sightings),
        }
        return sightings, stats
    finally:
        if not keep_audio and workdir is None:
            shutil.rmtree(tmp, ignore_errors=True)


def fingerprint_many(urls, **kw):
    all_sightings, all_stats = [], []
    for u in urls:
        try:
            s, st = fingerprint_set(u, **kw)
        except Exception as exc:
            all_stats.append({"url": u, "error": str(exc)[:200]})
            continue
        all_sightings.extend(s)
        all_stats.append(st)
    return all_sightings, all_stats
