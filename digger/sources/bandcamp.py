"""Bandcamp discovery.

Bandcamp's official API is limited to label sales reporting and exposes no
search or discovery, so this uses the JSON endpoint that Bandcamp's own
discovery page calls. It is undocumented and can change without notice; the
payload shape below was verified live on 2026-09-15.

Every item carries a featured_track stream URL, which is what makes local
audio analysis possible later without buying anything.
"""

from ..normalize import track_key, work_key, normalize_version
from .base import request

DISCOVER_URL = "https://bandcamp.com/api/discover/3/get_web"
TAG_PAGE_URL = "https://bandcamp.com/tag/{tag}?tab=all_releases"

# Verified slices. "new" is arrivals, "top" is best-selling, "rec" is curated.
SLICES = ("new", "top", "rec")


def _payload(page=0, slice_="new", genre=None, tag=None, time_window=None,
             location=None, category=None):
    """Only 'p' and 's' are mandatory; the rest narrow the result set."""
    body = {"p": int(page), "s": slice_}
    if genre:
        body["g"] = genre
    if tag:
        body["t"] = tag
    if time_window is not None:
        body["w"] = time_window
    if location is not None:
        body["l"] = location
    if category:
        body["f"] = category
    return body


def fetch_page(page=0, slice_="new", genre=None, tag=None, **kw):
    data = request(DISCOVER_URL, payload=_payload(page, slice_, genre, tag, **kw))
    if data.get("error"):
        raise RuntimeError("bandcamp discover: %s" % data.get("error_message"))
    return data.get("items", []), data.get("total_count", 0)


def _stream_url(item):
    ft = item.get("featured_track") or {}
    files = ft.get("file") or {}
    return files.get("mp3-128")


def _item_url(item):
    hints = item.get("url_hints") or {}
    sub = hints.get("subdomain")
    slug = hints.get("slug")
    kind = "album" if hints.get("item_type") == "a" else "track"
    if sub and slug:
        return "https://%s.bandcamp.com/%s/%s" % (sub, kind, slug)
    return None


def to_sighting(item):
    """Bandcamp names the release in primary_text and the artist in secondary_text."""
    title = (item.get("primary_text") or "").strip()
    artist = (item.get("secondary_text") or "").strip()
    if not title or not artist:
        return None
    return {
        "source": "bandcamp",
        "source_id": str(item.get("item_type_id") or item.get("id")),
        "track_key": track_key(artist, title),
        "work_key": work_key(artist, title),
        "artist": artist,
        "title": title,
        "version": normalize_version(title),
        "url": _item_url(item),
        "stream_url": _stream_url(item),
        "genre": item.get("genre_text"),
        "label": None,
        "location": item.get("location_text"),
        "published_at": item.get("publish_date"),
        "raw": item,
    }


def collect(pages=2, slices=("new",), genre=None, tag=None, **kw):
    """Yield sightings across the requested pages and slices."""
    out = []
    for slice_ in slices:
        for page in range(pages):
            items, _total = fetch_page(page, slice_, genre, tag, **kw)
            if not items:
                break
            for item in items:
                s = to_sighting(item)
                if s:
                    out.append(s)
    return out


# --- Release drill-down -------------------------------------------------
#
# The discover endpoint returns releases, not tracks. A DJ buys tracks, so
# every release is opened once and its trackinfo read from the data-tralbum
# blob embedded in the page. Verified live 2026-09-15.

import html as _html
import json as _json
import re as _re
import urllib.request as _urlreq

from .base import UA as _UA
from .base import throttle as _throttle

TRALBUM_RE = _re.compile(r'data-tralbum="(.*?)"', _re.S)
TAG_RE = _re.compile(r'<a class="tag" href="[^"]*">([^<]+)</a>')

# Releases a DJ will never play. Cheap title filter, applied before any fetch.
NOISE_WORDS = (
    "sample pack", "sample-pack", "samplepack", "drum kit", "loop kit",
    "preset pack", "presets", "sound kit", "one shots", "one-shots",
    "construction kit", "midi pack",
)


def is_noise(item):
    title = (item.get("primary_text") or "").lower()
    return any(word in title for word in NOISE_WORDS)


def fetch_release(url):
    """Return (tralbum_dict, tags) for a Bandcamp release page."""
    _throttle("bandcamp.com", 1.5)
    req = _urlreq.Request(url, headers={"User-Agent": _UA})
    with _urlreq.urlopen(req, timeout=25) as resp:
        page = resp.read().decode("utf-8", "ignore")
    m = TRALBUM_RE.search(page)
    if not m:
        return None, []
    tralbum = _json.loads(_html.unescape(m.group(1)))
    tags = [t.strip().lower() for t in TAG_RE.findall(page)]
    return tralbum, tags


def tracks_from_release(item, tralbum, tags):
    """Expand one release into per-track sightings."""
    artist = (tralbum.get("artist") or item.get("secondary_text") or "").strip()
    url = _item_url(item)
    out = []
    for t in tralbum.get("trackinfo") or []:
        title = (t.get("title") or "").strip()
        if not title:
            continue
        files = t.get("file") or {}
        fallback_id = "%s-%s" % (item.get("id"), t.get("track_num"))
        out.append({
            "source": "bandcamp",
            "source_id": "t%s" % (t.get("id") or fallback_id),
            "track_key": track_key(artist, title),
            "work_key": work_key(artist, title),
            "artist": artist,
            "title": title,
            "version": normalize_version(title),
            "url": (url + "#t%s" % t.get("track_num")) if url else None,
            "stream_url": files.get("mp3-128"),
            "genre": item.get("genre_text"),
            "label": None,
            "location": item.get("location_text"),
            "published_at": item.get("publish_date"),
            "raw": {"tags": tags, "duration": t.get("duration"),
                    "release": item.get("primary_text")},
        })
    return out


def collect_tracks(pages=1, slices=("new",), genre="electronic", tag=None,
                   want_tags=(), max_releases=40, skip_various=True):
    """Discover releases, then expand each into tracks.

    Sub-genre filtering through the discover payload does not take effect
    (verified: 'techno' returns the same 752 total as plain 'electronic'),
    so want_tags filters client-side on the tags carried by the release page.
    """
    out = []
    seen_releases = 0
    for slice_ in slices:
        for page in range(pages):
            items, _ = fetch_page(page, slice_, genre, tag)
            for item in items:
                if seen_releases >= max_releases:
                    return out
                if is_noise(item):
                    continue
                secondary = (item.get("secondary_text") or "").lower()
                if skip_various and secondary.startswith("various"):
                    continue
                url = _item_url(item)
                if not url:
                    continue
                try:
                    tralbum, tags_found = fetch_release(url)
                except Exception:
                    continue
                seen_releases += 1
                if not tralbum:
                    continue
                if want_tags and not (set(want_tags) & set(tags_found)):
                    continue
                out.extend(tracks_from_release(item, tralbum, tags_found))
    return out
