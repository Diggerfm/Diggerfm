"""1001Tracklists, used as an index of sets rather than as a source of data.

Their community has spent years identifying tracks by hand. This module
never takes that work: it reads which sets exist, when, in which genre, and
where the recording can be heard, then the recording is fingerprinted here.
No tracklist content is parsed, stored or republished.

What the site gives, measured on 2026-09-16:

  - The home page carries 30 sets in the served HTML, with title, date and
    an icon per available media platform. 25 of the 30 had one. One request.
  - A set page carries the media URL and the genres. Media links are
    community-submitted, so a set can have none at all ("No media links
    found"), which is why the icon on the index is checked first.
  - The SoundCloud form is https://api.soundcloud.com/tracks/<id>, and
    yt-dlp resolves it straight to the real recording.

Their terms discourage automated access. This stays at one index read plus
one page per candidate actually wanted, spaced, with an identifying
User-Agent, and it stops at the first sign of a block rather than working
around one. Defeating a challenge is not something this will do.
"""

import html
import re
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://www.1001tracklists.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
DELAY = 3.0

BLOCK_SIGNS = ("captcha", "just a moment", "access denied", "cf-browser-verification")

MEDIA_ICONS = ("soundcloud", "youtube", "mixcloud", "video-camera",
               "play-circle", "podcast", "cloud")

MEDIA_PATTERNS = (
    ("soundcloud", re.compile(r"https?://api\.soundcloud\.com/tracks/\d+")),
    ("youtube", re.compile(r"https?://(?:www\.)?youtube\.com/watch\?v=[\w-]{11}")),
    ("youtube", re.compile(r"https?://youtu\.be/[\w-]{11}")),
    ("hearthis", re.compile(r"https?://(?:www\.)?hearthis\.at/[\w\-]+/[\w\-]+")),
    ("mixcloud", re.compile(r"https?://(?:www\.)?mixcloud\.com/[\w\-]+/[\w\-]+")),
)


class Blocked(RuntimeError):
    """The site asked us to stop. We stop."""


_last = [0.0]


def _get(path):
    wait = DELAY - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    url = path if path.startswith("http") else BASE + path
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429, 503):
            raise Blocked("HTTP %s on %s" % (exc.code, url))
        raise
    finally:
        _last[0] = time.time()
    low = body.lower()
    if any(sign in low for sign in BLOCK_SIGNS):
        raise Blocked("challenge page served for %s" % url)
    return body


def _clean(text):
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def fetch_index():
    """One request. Returns the sets the site is currently showing.

    Each entry: url, title, date, and the media platforms it advertises.
    A set with no platform has no recording to fingerprint, so it is
    marked rather than fetched.
    """
    home = _get("/")
    out = []
    seen = set()
    for chunk in re.split(r'(?=href="/tracklist/)', home)[1:]:
        m = re.search(r'href="(/tracklist/[^"]+)"', chunk)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        head = chunk[:2600]
        title = re.search(r">([^<>]{12,140})</a>", head)
        date = re.search(r"(\d{4}-\d{2}-\d{2})", head)
        icons = {i for i in MEDIA_ICONS if ("fa-" + i) in head}
        out.append({
            "url": BASE + m.group(1),
            "path": m.group(1),
            "title": _clean(title.group(1)) if title else "",
            "date": date.group(1) if date else None,
            "platforms": sorted(icons),
            "has_media": bool(icons),
        })
    return out


def fetch_media(set_path):
    """One request for one set. Returns its recording URL and genres.

    Only the media link and the genre line are read. The tracklist itself
    is deliberately left alone.
    """
    page = _get(set_path)
    media, kind = None, None
    for name, pattern in MEDIA_PATTERNS:
        found = [u for u in pattern.findall(page) if "1001tracklists" not in u]
        if found:
            media, kind = found[0], name
            break

    genres = None
    g = re.search(r"Genre\(s\).*?</div>\s*<div[^>]*>(.*?)</div>", page, re.S)
    if not g:
        g = re.search(r"genre[^>]*>\s*([A-Z][^<]{3,90})<", page)
    if g:
        genres = _clean(g.group(1))[:140]

    title = re.search(r'<meta itemprop="name" content="([^"]+)"', page)
    count = re.search(r'data-count="(\d+)"', page)
    return {
        "media": media,
        "kind": kind,
        "genres": genres,
        "title": _clean(title.group(1)) if title else None,
        "n_tracks": int(count.group(1)) if count else None,
    }


def discover(want_genres=(), limit=10, skip_urls=(), on_step=None):
    """Index, filter, then resolve only what is actually wanted.

    want_genres matches on the genre line of the set page, so it costs one
    request per candidate. Keeping limit small is the point: a DJ plays
    fifteen sets a week, not four hundred.
    """
    skip = set(skip_urls)
    index = fetch_index()
    candidates = [s for s in index if s["has_media"]]
    if on_step:
        on_step("index", {"total": len(index), "with_media": len(candidates)})

    wanted = [w.lower() for w in want_genres]
    out = []
    for s in candidates:
        if len(out) >= limit:
            break
        try:
            detail = fetch_media(s["path"])
        except Blocked:
            raise
        except Exception:
            continue
        if not detail["media"] or detail["media"] in skip:
            continue
        if wanted:
            line = (detail["genres"] or "").lower()
            if not any(w in line for w in wanted):
                if on_step:
                    on_step("skip", {"title": s["title"], "genres": detail["genres"]})
                continue
        s.update(detail)
        out.append(s)
        if on_step:
            on_step("keep", s)
    return out
