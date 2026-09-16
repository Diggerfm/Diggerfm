"""Find the recording of a set from its title, and refuse a wrong match.

1001Tracklists serves a Cloudflare Turnstile challenge after about three
set pages, so the media link cannot be read from there in bulk and this
does not try to work around the challenge. The index page is still one
honest request and it carries the full title, which is enough to find the
recording on the platform that hosts it.

Searching on the title alone is not enough, and that is the whole point of
this module. Measured on five sets:

    Vision Radio S06E37   ->  found S06E31
    FSOE 980              ->  found FSOE 728
    Novasynth #279        ->  found #278
    Hysteria Radio 546    ->  correct
    Hot House Hours 335   ->  correct

Two of five. A radio show's episodes all share a title, so search happily
returns a neighbour. Fingerprinting the wrong episode would write tracks a
DJ never played into the evidence table, and the only reason the setlist
source outranks a chart is that it is evidence. So a candidate has to prove
it is the right episode before it is accepted.
"""

import re
import subprocess

# "S06E37", "#279", "546", "Vol. 12", "Episode 4"
EDITION = re.compile(
    r"(?:\bS\d{1,2}E\d{1,3}\b)"
    r"|(?:#\s*\d{1,5})"
    r"|(?:\b(?:ep(?:isode)?|vol(?:ume)?|part|pt)\.?\s*\d{1,5}\b)"
    r"|(?:\b\d{2,5}\b)",
    re.I)

TRAILING_DATE = re.compile(r"\s+\d{4}-\d{2}-\d{2}\s*$")


def edition_tokens(title):
    """The numbers that identify one episode among its siblings."""
    base = TRAILING_DATE.sub("", title or "")
    out = []
    for m in EDITION.findall(base):
        token = re.sub(r"[^0-9a-z]", "", m.lower())
        if token and token not in out:
            out.append(token)
    return out


def matches_edition(wanted_title, candidate_title):
    """True only when the candidate carries the same edition markers.

    A set with no number in its title (a club date, a festival) has nothing
    to check, so it falls back to requiring the words to overlap heavily
    rather than accepting anything.
    """
    wanted = edition_tokens(wanted_title)
    if not wanted:
        a = set(re.findall(r"[a-z]{4,}", (wanted_title or "").lower()))
        b = set(re.findall(r"[a-z]{4,}", (candidate_title or "").lower()))
        return bool(a) and len(a & b) >= max(2, len(a) // 2)
    found = edition_tokens(candidate_title)
    return any(w in found for w in wanted)


def _search(query, ytdlp, timeout=90):
    try:
        r = subprocess.run(
            [ytdlp, "--skip-download", "--no-warnings", "--flat-playlist",
             "--print", "%(title)s\t%(duration)s\t%(webpage_url)s", query],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace")
    except Exception:
        return []
    out = []
    for line in (r.stdout or "").strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            try:
                dur = float(parts[1])
            except (TypeError, ValueError):
                dur = None
            out.append({"title": parts[0], "duration": dur, "url": parts[2]})
    return out


def find_recording(title, ytdlp, min_seconds=1200, per_engine=5,
                   expected_seconds=None, tolerance=0.25):
    """Locate the recording of this exact set, or return None.

    min_seconds rejects a trailer or a single track pulled from the set.
    expected_seconds, when the index gave a duration, rejects a candidate
    whose length is wrong even if the title looks right.
    """
    clean = TRAILING_DATE.sub("", title or "").strip()
    if not clean:
        return None

    for engine in ("scsearch", "ytsearch"):
        for cand in _search("%s%d:%s" % (engine, per_engine, clean), ytdlp):
            if cand["duration"] is None or cand["duration"] < min_seconds:
                continue
            if not matches_edition(clean, cand["title"]):
                continue
            if expected_seconds:
                gap = abs(cand["duration"] - expected_seconds) / expected_seconds
                if gap > tolerance:
                    continue
            cand["engine"] = engine
            cand["query"] = clean
            return cand
    return None
