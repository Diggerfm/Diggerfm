"""Recognition through Shazam.

shazamio speaks to Shazam's own mobile endpoint. It is free and needs no
key, but it is unofficial and it rate limits, hence the delay between calls
and the tolerance for empty answers: a slice landing on a transition or on
an unreleased edit simply will not match, and that is expected.
"""

import asyncio

from shazamio import Shazam

DEFAULT_DELAY = 3.0


def _extract(result):
    """Pull artist, title and ISRC out of a Shazam response."""
    track = (result or {}).get("track") or {}
    if not track:
        return None
    title = (track.get("title") or "").strip()
    artist = (track.get("subtitle") or "").strip()
    if not title or not artist:
        return None

    isrc = track.get("isrc")
    label, released = None, None
    for section in track.get("sections") or []:
        for item in section.get("metadata") or []:
            key = (item.get("title") or "").lower()
            if key == "label":
                label = item.get("text")
            elif key == "released":
                released = item.get("text")
            elif key == "isrc" and not isrc:
                isrc = item.get("text")
    return {
        "artist": artist,
        "title": title,
        "isrc": isrc,
        "label": label,
        "released": released,
        "shazam_key": track.get("key"),
    }


async def _identify_all(slices, delay, on_result=None):
    shazam = Shazam()
    out = []
    for offset, path in slices:
        try:
            res = await shazam.recognize(path)
            hit = _extract(res)
        except Exception as exc:
            hit = None
            res = {"error": str(exc)[:120]}
        record = {"offset": offset, "hit": hit}
        out.append(record)
        if on_result:
            on_result(record)
        await asyncio.sleep(delay)
    return out


def identify(slices, delay=DEFAULT_DELAY, on_result=None):
    """Recognise each slice. Returns [{offset, hit}] with hit possibly None."""
    return asyncio.run(_identify_all(slices, delay, on_result))
