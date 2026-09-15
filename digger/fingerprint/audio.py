"""Set audio: fetch it, then cut it into recognisable slices.

A track inside a DJ set runs three to six minutes, so probing every 90
seconds catches each one two to four times. Probing every 20 seconds would
cost four times as many recognition calls for the same tracklist.

The first minute is skipped because sets open on intros, crowd noise and
talking, none of which identify.
"""

import glob
import os
import subprocess

DEFAULT_PROBE_EVERY = 90      # seconds between probes
DEFAULT_SLICE_LEN = 12        # seconds handed to the recogniser
DEFAULT_HEAD_SKIP = 60        # ignore the opening


AUDIO_EXTS = ("*.m4a", "*.webm", "*.opus", "*.mp3", "*.mp4")


def download(url, out_dir, max_seconds=None, quiet=True):
    """Pull the smallest usable audio stream. Returns the file path.

    The stream is taken as-is rather than re-encoded: a 74 minute set is
    about 25 MB in the low m4a rendition, and asking yt-dlp to extract and
    convert costs far more time than downloading it whole.

    max_seconds is deliberately NOT implemented with --download-sections.
    That flag makes yt-dlp re-mux through ffmpeg and, on HLS sources, it
    stalls for minutes. Slicing after the fact is faster and more reliable.
    """
    os.makedirs(out_dir, exist_ok=True)
    template = os.path.join(out_dir, "%(id)s.%(ext)s")
    cmd = [
        "yt-dlp",
        "-f", "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio",
        "--no-playlist", "--no-warnings", "--no-part",
        "--retries", "3", "--socket-timeout", "30",
        "-o", template, url,
    ]
    if quiet:
        cmd.append("--quiet")
    subprocess.run(cmd, check=True, timeout=1800)

    files = []
    for pattern in AUDIO_EXTS:
        files.extend(glob.glob(os.path.join(out_dir, pattern)))
    if not files:
        raise RuntimeError("yt-dlp produced no audio for %s" % url)
    return max(files, key=os.path.getmtime)


def duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def slice_windows(path, out_dir, probe_every=DEFAULT_PROBE_EVERY,
                  slice_len=DEFAULT_SLICE_LEN, head_skip=DEFAULT_HEAD_SKIP,
                  max_slices=None):
    """Cut mono 16 kHz WAV probes. Returns [(offset_seconds, wav_path)]."""
    os.makedirs(out_dir, exist_ok=True)
    total = duration(path)
    offsets = list(range(head_skip, int(total) - slice_len, probe_every))
    if max_slices:
        offsets = offsets[:max_slices]

    made = []
    for off in offsets:
        wav = os.path.join(out_dir, "s%06d.wav" % off)
        if not os.path.exists(wav):
            subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-y",
                 "-ss", str(off), "-t", str(slice_len), "-i", path,
                 "-ac", "1", "-ar", "16000", wav],
                check=True, timeout=120)
        made.append((off, wav))
    return made


def cleanup(paths):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass
