"""Mixcloud.

The public read API needs no authentication. Tracklists live in the
'sections' field of a cloudcast detail response, but uploaders fill it in
rarely, so the fill rate is measured rather than assumed. See
report_section_fill_rate().
"""

from ..normalize import normalize_version, track_key, work_key
from .base import request

API = "https://api.mixcloud.com"


def search_cloudcasts(query, limit=30):
    data = request("%s/search/?q=%s&type=cloudcast&limit=%d"
                   % (API, request_quote(query), limit))
    return data.get("data", [])


def request_quote(text):
    import urllib.parse
    return urllib.parse.quote(text)


def cloudcast_detail(key):
    return request(API + key)


def popular(limit=30, hot=False):
    path = "/popular/hot/" if hot else "/popular/"
    return request("%s%s?limit=%d" % (API, path, limit)).get("data", [])


def tracks_from_sections(detail, cloudcast):
    """Turn a filled 'sections' tracklist into sightings."""
    out = []
    for sec in detail.get("sections") or []:
        song = (sec.get("track") or {})
        title = (song.get("name") or sec.get("song") or "").strip()
        artist = ((song.get("artist") or {}).get("name")
                  or sec.get("artist") or "").strip()
        if not title or not artist:
            continue
        out.append({
            "source": "mixcloud",
            "source_id": "%s#%s" % (cloudcast.get("key"), sec.get("start_time")),
            "track_key": track_key(artist, title),
            "work_key": work_key(artist, title),
            "artist": artist,
            "title": title,
            "version": normalize_version(title),
            "url": cloudcast.get("url"),
            "stream_url": None,
            "genre": ",".join(t.get("name", "") for t in cloudcast.get("tags", [])) or None,
            "label": None,
            "location": None,
            "published_at": cloudcast.get("created_time"),
            "raw": {"cloudcast": cloudcast.get("key"), "section": sec},
        })
    return out


def report_section_fill_rate(query="techno", sample=15):
    """How many sampled cloudcasts actually carry a tracklist."""
    casts = search_cloudcasts(query, sample)
    filled = 0
    total_tracks = 0
    for c in casts:
        detail = cloudcast_detail(c["key"])
        n = len(detail.get("sections") or [])
        if n:
            filled += 1
            total_tracks += n
    return {"sampled": len(casts), "with_tracklist": filled, "tracks": total_tracks}


def collect(queries=("tech house", "melodic techno"), per_query=20):
    out = []
    for q in queries:
        for cast in search_cloudcasts(q, per_query):
            detail = cloudcast_detail(cast["key"])
            out.extend(tracks_from_sections(detail, cast))
    return out
