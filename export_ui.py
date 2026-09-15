"""Build the payload the web view renders.

Four views, because the weekly twenty is the output and not the product:

  digest   the curated short list, with audio and rating
  sets     every fingerprinted set, its tracklist, and a YouTube link
           timestamped to the second the track plays
  trends   what the most DJ charts agree on right now, duplicates removed
  fresh    recent releases from the labels he follows

Audio is transcoded to audio-only WebM/Opus and served from the site
alongside the page. Opus at 112 kbit keeps the bass a DJ judges a groove
on, and the container plays in an <audio> element in every current browser
even though the server labels it video/webm.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from digger import db, score

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "site")
AUDIO_DIR = os.path.join(OUT_DIR, "audio")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120"

# Served as ordinary files from the site, so the page is not paying for
# them: a DJ judging a groove needs the bass intact, and 45 s at 112 kbit
# Opus stereo is about 630 KB, which is nothing over HTTP and everything
# compared with the 72 kbit mono a page-embedded copy would have forced.
PREVIEW_SECONDS = 45
OPUS_BITRATE = "112k"
WAVE_BUCKETS = 96

NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
ENHARMONIC = {"DB": "C#", "EB": "D#", "GB": "F#", "AB": "G#", "BB": "A#",
              "CB": "B", "FB": "E", "E#": "F", "B#": "C"}


def camelot(key_name):
    """Beatport writes 'Gb Major'; a DJ reads '2B'."""
    if not key_name:
        return None
    m = re.match(r"\s*([A-Ga-g][#b]?)\s*(maj|min)", key_name.strip(), re.I)
    if not m:
        return None
    note = m.group(1).upper().replace("B", "b") if len(m.group(1)) > 1 else m.group(1).upper()
    note = m.group(1).strip().upper()
    note = ENHARMONIC.get(note, note)
    if note not in NOTES:
        return None
    pc = NOTES.index(note)
    minor = m.group(2).lower().startswith("min")
    for i in range(12):
        if minor and (8 + 7 * i) % 12 == pc:
            return "%dA" % (i + 1)
        if not minor and (11 + 7 * i) % 12 == pc:
            return "%dB" % (i + 1)
    return None


def _ffmpeg(args):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y"] + args,
                   check=True, timeout=180)


def make_preview(stream_url, out_path):
    """Download, trim, and write an audio-only WebM."""
    fd, raw = tempfile.mkstemp(suffix=".audio")
    os.close(fd)
    try:
        req = urllib.request.Request(stream_url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as resp, open(raw, "wb") as fh:
            fh.write(resp.read())
        if os.path.getsize(raw) < 10000:
            raise ValueError("preview too small")
        # Start a quarter in: intros do not tell a DJ anything.
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", raw],
            capture_output=True, text=True, check=True)
        total = float(probe.stdout.strip() or 0)
        start = max(0.0, min(total * 0.25, max(0.0, total - PREVIEW_SECONDS)))
        _ffmpeg(["-ss", str(start), "-t", str(PREVIEW_SECONDS), "-i", raw,
                 "-vn", "-ac", "2", "-ar", "48000",
                 "-c:a", "libopus", "-b:a", OPUS_BITRATE, out_path])
        return os.path.getsize(out_path)
    finally:
        try:
            os.remove(raw)
        except OSError:
            pass


def waveform(path, buckets=WAVE_BUCKETS):
    """RMS envelope, 0-100 per bucket, for the scrubber.

    Peak amplitude was the obvious choice and it renders as a flat slab:
    club music is compressed, so nearly every bucket peaks near the
    maximum and the shape disappears. RMS carries the dynamics instead,
    normalised against the 95th percentile rather than the single loudest
    bucket (one snare should not flatten the rest), then curved so the
    difference between a breakdown and a drop is visible at 30 pixels.
    """
    try:
        import numpy as np
        import librosa
        y, sr = librosa.load(path, sr=8000, mono=True)
        if y.size == 0:
            return []
        step = max(1, len(y) // buckets)
        rms = np.array([float(np.sqrt(np.mean(np.square(y[i:i + step]))))
                        for i in range(0, step * buckets, step)])
        if not rms.size or not np.isfinite(rms).any():
            return []
        top = float(np.percentile(rms, 95)) or float(rms.max()) or 1.0
        norm = np.clip(rms / top, 0.0, 1.0) ** 0.6
        floor = 0.12
        return [int(round(100 * (floor + (1 - floor) * v))) for v in norm]
    except Exception:
        return []


def _meta(conn, work_key):
    r = conn.execute("""
        SELECT MAX(stream_url) stream_url, MAX(bpm) bpm, MAX(music_key) music_key,
               MAX(genre) genre, MAX(label) label, MAX(url) url, MAX(isrc) isrc,
               GROUP_CONCAT(DISTINCT source) sources
        FROM sightings WHERE work_key = ?
    """, (work_key,)).fetchone()
    return dict(r) if r else {}


def _features(conn, work_key):
    r = conn.execute("""
        SELECT energy, dynamics, brightness, percussive_ratio, onset_rate
        FROM audio_features WHERE work_key = ? AND error IS NULL
    """, (work_key,)).fetchone()
    return dict(r) if r else None


# The meta line already carries BPM, key and the chart count, and the
# missing-profile note belongs in the page's empty state rather than
# repeated under all ten rows.
_REASON_DROP = (
    re.compile(r",?\s*\d+\s*BPM[^,]*", re.I),
    re.compile(r",?\s*charte par \d+ DJs[^,]*", re.I),
    re.compile(r",?\s*analyse audio faite, profil de reference absent", re.I),
    re.compile(r",?\s*pas d'analyse audio[^,]*", re.I),
)


def _ui_reason(text):
    for pattern in _REASON_DROP:
        text = pattern.sub("", text or "")
    text = re.sub(r"\s*,\s*", ", ", text).strip(" ,")
    return text


def build_digest(conn, limit_audio=10):
    artists, labels = score.profile_vectors(conn)
    timbre = score.timbre_profile(conn)
    cand_feats = score.load_candidate_features(conn)
    scored = score.score_all(conn, since_days=14)
    slots = score.assign_slots(scored)

    out = []
    for s in slots:
        m = _meta(conn, s["work_key"])
        out.append({
            "id": s["work_key"],
            "slot": s["slot"],
            "rank": s["rank"],
            "artist": s["artist"],
            "title": s["title"],
            "bpm": m.get("bpm"),
            "key": m.get("music_key"),
            "camelot": camelot(m.get("music_key")),
            "genre": m.get("genre"),
            "label": m.get("label"),
            "url": m.get("url"),
            "sources": (m.get("sources") or "").split(","),
            "charts": s.get("n_charts") or 0,
            "charts_raw": s.get("n_charts_raw") or 0,
            "reason": _ui_reason(score.explain(s, artists, labels, timbre,
                                               cand_feats.get(s["work_key"]))),
            "features": _features(conn, s["work_key"]),
            # The score is shown broken into its parts rather than as one
            # number: a single figure invites the "94 % compatible" theatre
            # the brief was right to distrust, while the parts can each be
            # argued with.
            "score": round(s.get("score") or 0, 3),
            "affinity": round(s.get("affinity") or 0, 3),
            "corroboration": round(s.get("corroboration") or 0, 3),
            "timbre_sim": (score.timbre_similarity(cand_feats.get(s["work_key"]), timbre)
                           if timbre else None),
            "stream": m.get("stream_url"),
        })
    return out


def build_stats(conn):
    """The shape of what was collected, and of his own library."""
    stats = {}
    stats["by_source"] = [dict(r) for r in conn.execute("""
        SELECT source, COUNT(*) n, COUNT(DISTINCT work_key) works
        FROM sightings GROUP BY source ORDER BY n DESC
    """)]
    stats["by_genre"] = [dict(r) for r in conn.execute("""
        SELECT genre, COUNT(DISTINCT work_key) n FROM sightings
        WHERE genre IS NOT NULL AND source = 'beatport'
        GROUP BY genre ORDER BY n DESC LIMIT 18
    """)]
    stats["bpm"] = [dict(r) for r in conn.execute("""
        SELECT CAST(bpm / 5 AS INT) * 5 AS bucket, COUNT(DISTINCT work_key) n
        FROM sightings WHERE bpm BETWEEN 100 AND 160
        GROUP BY bucket ORDER BY bucket
    """)]
    stats["timbre_by_genre"] = [dict(r) for r in conn.execute("""
        SELECT s.genre, COUNT(*) n,
               ROUND(AVG(f.energy), 4) energy,
               ROUND(AVG(f.percussive_ratio), 3) percussive,
               ROUND(AVG(f.onset_rate), 2) onsets,
               ROUND(AVG(f.brightness), 0) brightness
        FROM sightings s JOIN audio_features f ON f.work_key = s.work_key
        WHERE s.source = 'beatport' AND f.error IS NULL AND s.genre IS NOT NULL
        GROUP BY s.genre HAVING n >= 8 ORDER BY energy DESC LIMIT 14
    """)]

    # His own library, when it has been imported.
    prof = conn.execute("SELECT COUNT(*) n FROM profile_tracks").fetchone()["n"]
    if prof:
        stats["profile"] = {
            "tracks": prof,
            "functions": [dict(r) for r in conn.execute("""
                SELECT functions, COUNT(*) n, SUM(play_count) plays
                FROM profile_tracks WHERE functions IS NOT NULL
                GROUP BY functions ORDER BY plays DESC
            """)],
            "labels": [dict(r) for r in conn.execute("""
                SELECT label, COUNT(*) n, SUM(play_count) plays
                FROM profile_tracks WHERE label IS NOT NULL
                GROUP BY LOWER(label) ORDER BY plays DESC LIMIT 15
            """)],
            "bpm": [dict(r) for r in conn.execute("""
                SELECT CAST(bpm / 2 AS INT) * 2 AS bucket, COUNT(*) n,
                       SUM(play_count) plays
                FROM profile_tracks WHERE bpm BETWEEN 100 AND 150
                GROUP BY bucket ORDER BY bucket
            """)],
        }
    else:
        stats["profile"] = None
    return stats


def set_metadata(url):
    """Title, uploader, duration and date, straight from yt-dlp."""
    try:
        r = subprocess.run(
            ["yt-dlp", "--skip-download", "--no-warnings", "--print",
             "%(title)s\t%(uploader)s\t%(duration)s\t%(upload_date)s", url],
            capture_output=True, text=True, timeout=120, encoding="utf-8")
        parts = (r.stdout or "").strip().split("\t")
        if len(parts) >= 4:
            return {"title": parts[0], "uploader": parts[1],
                    "duration": int(parts[2]) if parts[2].isdigit() else None,
                    "date": parts[3]}
    except Exception:
        pass
    return {}


def fetch_thumb(video_id, out_dir):
    """Save the YouTube thumbnail next to the page.

    Downloaded rather than hotlinked: 30 KB each, and the card then holds
    together if the video is pulled or YouTube changes its CDN paths.
    """
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "%s.jpg" % video_id)
    if os.path.exists(path):
        return "thumbs/%s.jpg" % video_id
    for name in ("maxresdefault", "hqdefault", "mqdefault"):
        try:
            req = urllib.request.Request(
                "https://i.ytimg.com/vi/%s/%s.jpg" % (video_id, name),
                headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            if len(data) > 3000:
                with open(path, "wb") as fh:
                    fh.write(data)
                # maxresdefault arrives around 300 KB and the card shows it
                # at 152 px. Thumbnails load with the tab while audio waits
                # to be asked for, so they are the first-paint cost.
                try:
                    _ffmpeg(["-i", path, "-vf", "scale=640:-2",
                             "-q:v", "4", path + ".tmp.jpg"])
                    if os.path.getsize(path + ".tmp.jpg") > 2000:
                        os.replace(path + ".tmp.jpg", path)
                except Exception:
                    pass
                finally:
                    if os.path.exists(path + ".tmp.jpg"):
                        os.remove(path + ".tmp.jpg")
                return "thumbs/%s.jpg" % video_id
        except Exception:
            continue
    return None


def build_sets(conn):
    rows = conn.execute("""
        SELECT url, artist, title, raw FROM sightings WHERE source = 'setlist'
    """).fetchall()
    by_url = {}
    for r in rows:
        raw = json.loads(r["raw"])
        entry = by_url.setdefault(r["url"], {"url": r["url"],
                                             "name": raw.get("set"),
                                             "tracks": []})
        entry["tracks"].append({
            "artist": r["artist"], "title": r["title"],
            "start": raw.get("start"), "end": raw.get("end"),
            "probes": raw.get("probes", 1),
        })
    out = []
    for url, entry in by_url.items():
        vid = None
        m = re.search(r"(?:v=|youtu\.be/)([\w-]{11})", url)
        if m:
            vid = m.group(1)
        entry["video_id"] = vid
        entry["tracks"].sort(key=lambda t: t["start"] or 0)
        # A track a DJ can jump to: the link lands on the second it plays.
        for t in entry["tracks"]:
            t["at"] = ("https://youtu.be/%s?t=%d" % (vid, max(0, (t["start"] or 0) - 5))
                       if vid else None)
        entry["count"] = len(entry["tracks"])

        meta = set_metadata(url)
        if meta.get("title"):
            entry["name"] = meta["title"]
        entry["uploader"] = meta.get("uploader")
        entry["duration"] = meta.get("duration")
        entry["date"] = meta.get("date")
        if vid:
            entry["thumb"] = fetch_thumb(vid, os.path.join(OUT_DIR, "thumbs"))
        # How much of the set the fingerprinting actually accounted for.
        if entry["duration"]:
            covered = sum(max(0, (t["end"] or 0) - (t["start"] or 0)) + 90
                          for t in entry["tracks"])
            entry["coverage"] = min(1.0, covered / entry["duration"])
        out.append(entry)
    out.sort(key=lambda e: -e["count"])
    return out


def build_trends(conn, limit=50):
    from digger import charts as ch
    groups = ch.group_map(conn)
    rows = conn.execute("""
        SELECT work_key, MIN(artist) artist, MIN(title) title,
               GROUP_CONCAT(DISTINCT json_extract(raw, '$.chart_id')) cids
        FROM sightings WHERE source = 'beatport' GROUP BY work_key
    """).fetchall()
    scored = []
    for r in rows:
        ids = [i for i in (r["cids"] or "").split(",") if i]
        n = len({groups.get(i, i) for i in ids})
        if n < 2:
            continue
        m = _meta(conn, r["work_key"])
        scored.append({
            "id": r["work_key"], "artist": r["artist"], "title": r["title"],
            "charts": n, "charts_raw": len(set(ids)),
            "bpm": m.get("bpm"), "key": m.get("music_key"),
            "camelot": camelot(m.get("music_key")),
            "genre": m.get("genre"), "label": m.get("label"),
            "url": m.get("url"), "stream": m.get("stream_url"),
        })
    scored.sort(key=lambda x: (-x["charts"], x["artist"]))
    return scored[:limit]


FRESH_WINDOW_DAYS = 120
FRESH_PER_ARTIST = 2


def build_fresh(conn, limit=50):
    """Recent releases, newest first and actually sorted by date.

    Dates are ISO in the database now; they were not, and a text sort over
    Bandcamp's "31 Jul 2026 08:28:51 GMT" ordered by day-of-month, which
    put a 2011 record above a 2026 one. See normalize.iso_date.

    Capped per artist because a five-track EP published in one go would
    otherwise take five consecutive rows and push out four other releases.
    """
    import datetime

    cutoff = (datetime.date.today()
              - datetime.timedelta(days=FRESH_WINDOW_DAYS)).isoformat()
    rows = conn.execute("""
        SELECT work_key, MIN(artist) artist, MIN(title) title, MAX(label) label,
               MAX(published_at) published_at, MAX(bpm) bpm,
               MAX(music_key) music_key, MAX(genre) genre, MAX(url) url,
               MAX(stream_url) stream_url, GROUP_CONCAT(DISTINCT source) sources
        FROM sightings
        WHERE source IN ('label', 'bandcamp') AND published_at >= ?
        GROUP BY work_key ORDER BY published_at DESC
    """, (cutoff,)).fetchall()

    seen = {}
    out = []
    for r in rows:
        who = (r["artist"] or "").lower()
        if seen.get(who, 0) >= FRESH_PER_ARTIST:
            continue
        seen[who] = seen.get(who, 0) + 1
        out.append({
            "id": r["work_key"], "artist": r["artist"], "title": r["title"],
            "label": r["label"], "date": r["published_at"], "bpm": r["bpm"],
            "key": r["music_key"], "camelot": camelot(r["music_key"]),
            "genre": r["genre"], "url": r["url"], "stream": r["stream_url"],
            "sources": (r["sources"] or "").split(","),
        })
        if len(out) >= limit:
            break
    return out


def attach_audio(items, max_items):
    """Transcode previews for the first max_items that have a stream."""
    os.makedirs(AUDIO_DIR, exist_ok=True)
    made = 0
    for it in items:
        if made >= max_items:
            it.pop("stream", None)
            continue
        stream = it.get("stream")
        it.pop("stream", None)
        if not stream:
            continue
        safe = re.sub(r"[^a-z0-9]+", "-", it["id"].lower())[:60]
        path = os.path.join(AUDIO_DIR, "%s.webm" % safe)
        try:
            if not os.path.exists(path):
                make_preview(stream, path)
            it["wave"] = waveform(path)
            it["audio"] = "audio/" + os.path.basename(path)
            made += 1
            print("  audio %2d/%d  %s - %s" % (made, max_items,
                                               it["artist"][:22], it["title"][:26]),
                  flush=True)
        except Exception as exc:
            print("  ECHEC %s - %s : %s" % (it["artist"][:22], it["title"][:22],
                                            str(exc)[:60]), flush=True)
    return made


def main():
    import datetime

    conn = db.connect()
    os.makedirs(OUT_DIR, exist_ok=True)

    print("digest...")
    digest = build_digest(conn)
    print("sets...")
    sets = build_sets(conn)
    print("tendances...")
    trends = build_trends(conn)
    print("nouveautes...")
    fresh = build_fresh(conn)
    print("analyse...")
    stats = build_stats(conn)

    print("audio du digest...")
    n1 = attach_audio(digest, 20)
    print("audio des tendances...")
    n2 = attach_audio(trends, 30)
    print("audio des nouveautes...")
    n3 = attach_audio(fresh, 25)

    timbre = score.timbre_profile(conn)
    payload = {
        "week": datetime.date.today().isoformat(),
        "generated": datetime.datetime.now().isoformat(timespec="minutes"),
        "digest": digest,
        "sets": sets,
        "trends": trends,
        "fresh": fresh,
        "stats": stats,
        "profile": ({"tracks": timbre["n"]} if timbre else None),
        "weights": score.TIMBRE_WEIGHTS,
        "counts": {
            "sightings": conn.execute(
                "SELECT COUNT(*) n FROM sightings").fetchone()["n"],
            "works": conn.execute(
                "SELECT COUNT(DISTINCT work_key) n FROM sightings").fetchone()["n"],
            "analysed": conn.execute(
                "SELECT COUNT(*) n FROM audio_features WHERE error IS NULL"
            ).fetchone()["n"],
        },
    }
    path = os.path.join(OUT_DIR, "payload.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    size = os.path.getsize(path)
    print()
    print("ecrit %s (%.0f Ko)" % (path, size / 1024))
    print("  digest %d | sets %d | tendances %d | nouveautes %d"
          % (len(digest), len(sets), len(trends), len(fresh)))
    print("  audio: %d morceaux transcodes" % (n1 + n2 + n3))
    return 0


if __name__ == "__main__":
    sys.exit(main())
