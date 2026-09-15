"""Discogs: the catalogue Beatport does not have.

Beatport is a shop, so it is thin before roughly 2005 and it keeps no
credits. Discogs is an archive of essentially everything pressed, and it
records who produced, remixed, engineered and mastered each record. That is
the material for filiation, which the brief asked for and nothing here
could answer: "the person who mixed your favourite record also did these
three".

Terms, read on 2026-09-15 and reflected in this module:

  - The fields used here are CC0: release titles, dates, track listings,
    identifiers, CREDITS, versions; artist names and associated releases;
    label, producer, distributor names and associated releases. Restricted
    Data (user profiles, marketplace prices and sales history, images) is
    never requested and never stored.
  - A unique, identifying User-Agent is mandatory. Generic ones are
    silently blocked, which looks like an outage rather than a refusal.
  - 60 requests a minute with a token, 25 without, over a moving 60 second
    window. The response carries the counters, so the throttle reads them
    rather than guessing.
  - Two notices must appear in any public surface, one about affiliation
    and one reading "Data provided by Discogs" next to the data with a
    dofollow link back. See NOTICE_APP and release_url().
"""

import difflib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.discogs.com"

NOTICE_APP = ("This application uses Discogs' API but is not affiliated "
              "with, sponsored or endorsed by Discogs. 'Discogs' is a "
              "trademark of Zink Media, LLC.")
NOTICE_DATA = "Data provided by Discogs"

# Credit roles worth keeping. A release lists dozens, most of them
# irrelevant to why a record sounds the way it does.
ROLES_OF_INTEREST = re.compile(
    r"produc|remix|written|composed|mix|master|engineer|arrange|vocal",
    re.I)


class DiscogsError(RuntimeError):
    pass


class DiscogsClient:
    def __init__(self, token=None, user_agent=None, min_interval=None):
        if not user_agent:
            raise DiscogsError("Discogs requires an identifying User-Agent")
        self.token = token
        self.user_agent = user_agent
        # 60/min with a token, 25 without, plus a margin.
        self.min_interval = min_interval or (1.1 if token else 2.6)
        self._last = 0.0
        self.remaining = None

    def _headers(self):
        h = {"User-Agent": self.user_agent,
             "Accept": "application/vnd.discogs.v2.discogs+json"}
        if self.token:
            h["Authorization"] = "Discogs token=%s" % self.token
        return h

    def get(self, path, **params):
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        # If the server says we are nearly out, wait for the window to roll.
        if self.remaining is not None and self.remaining <= 1:
            time.sleep(5)
        url = API + path
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self._last = time.time()
                rem = resp.headers.get("X-Discogs-Ratelimit-Remaining")
                self.remaining = int(rem) if rem and rem.isdigit() else None
                return json.loads(resp.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as exc:
            self._last = time.time()
            if exc.code == 429:
                time.sleep(10)
                raise DiscogsError("rate limited")
            if exc.code == 404:
                return None
            raise DiscogsError("HTTP %s on %s" % (exc.code, path))


def release_url(release_id):
    """The link the attribution notice has to point at."""
    return "https://www.discogs.com/release/%s" % release_id


# --- search -------------------------------------------------------------

def _ratio(a, b):
    return difflib.SequenceMatcher(None, (a or "").lower(),
                                   (b or "").lower()).ratio()


PAREN = re.compile(r"[\(\[].*?[\)\]]")
FEAT = re.compile(r"\s+(feat\.?|ft\.?|featuring|with)\s+.*$", re.I)
VA_PREFIX = re.compile(r"^\s*(VA|Various)\b.*?(mixed by|by)\s+", re.I)


def clean_title(title):
    """Strip what Discogs' track filter will not match.

    Shazam returns titles like "Silence (feat. Sarah McLachlan) [DJ Tiesto
    Remix]". Passed whole to track=, the filter finds nothing at all, even
    for a record as common as that one.
    """
    t = PAREN.sub(" ", title or "")
    t = FEAT.sub("", t)
    return re.sub(r"\s+", " ", t).strip(" -")


def clean_artist(artist):
    """Shazam sometimes returns a compilation credit as the artist."""
    a = VA_PREFIX.sub("", artist or "")
    a = FEAT.sub("", a)
    a = re.sub(r"\*+$", "", a)
    return re.sub(r"\s+", " ", a).strip()


def search_release(client, artist, title, year=None, per_page=10):
    """Find a release, and say how confident the match is.

    Scored rather than taken on faith: Discogs search is generous, and
    crediting a play to the wrong record is worse than leaving it unmatched.
    The reasons are returned alongside so a low score can be inspected
    instead of silently trusted.
    """
    a_clean = clean_artist(artist)
    t_clean = clean_title(title)
    data = client.get("/database/search", artist=a_clean, track=t_clean,
                      type="release", per_page=per_page)
    results = (data or {}).get("results") or []
    if not results:
        # The structured filters are strict; the free-text query is not.
        data = client.get("/database/search",
                          q="%s %s" % (a_clean, t_clean),
                          type="release", per_page=per_page)
        results = (data or {}).get("results") or []

    best = None
    for r in results:
        whole = r.get("title") or ""          # Discogs writes "Artist - Title"
        left, _, right = whole.partition(" - ")
        a_score = max(_ratio(a_clean, left), _ratio(a_clean, whole))
        t_score = max(_ratio(t_clean, right), _ratio(t_clean, whole),
                      _ratio(t_clean, clean_title(right)))
        score = 0.55 * a_score + 0.45 * t_score
        reasons = []
        if a_score > 0.85:
            reasons.append("artiste")
        if t_score > 0.85:
            reasons.append("titre")
        if year and str(r.get("year") or "") == str(year):
            score += 0.05
            reasons.append("annee")
        if best is None or score > best["confidence"]:
            best = {"id": r.get("id"), "title": whole,
                    "year": r.get("year"), "label": (r.get("label") or [None])[0],
                    "genre": r.get("genre"), "style": r.get("style"),
                    "confidence": round(min(1.0, score), 3),
                    "reasons": reasons,
                    "url": release_url(r.get("id"))}
    return best


# --- credits, which is the point ---------------------------------------

def release_credits(client, release_id):
    """Who actually made the record, plus its label and year."""
    data = client.get("/releases/%s" % release_id)
    if not data:
        return None
    people = {}
    for who in (data.get("extraartists") or []):
        role = who.get("role") or ""
        if not ROLES_OF_INTEREST.search(role):
            continue
        name = (who.get("name") or "").strip()
        if not name:
            continue
        people.setdefault(name, set()).add(role.split(",")[0].strip())
    for track in (data.get("tracklist") or []):
        for who in (track.get("extraartists") or []):
            role = who.get("role") or ""
            if not ROLES_OF_INTEREST.search(role):
                continue
            name = (who.get("name") or "").strip()
            if name:
                people.setdefault(name, set()).add(role.split(",")[0].strip())
    return {
        "id": data.get("id"),
        "title": data.get("title"),
        "year": data.get("year"),
        "labels": [l.get("name") for l in (data.get("labels") or [])],
        "label_ids": [l.get("id") for l in (data.get("labels") or [])],
        "styles": data.get("styles") or [],
        "genres": data.get("genres") or [],
        "credits": [{"name": n, "roles": sorted(r)} for n, r in people.items()],
        "url": release_url(data.get("id")),
    }


def artist_releases(client, artist_id, page=1, per_page=50):
    return client.get("/artists/%s/releases" % artist_id,
                      page=page, per_page=per_page, sort="year",
                      sort_order="desc")


def label_releases(client, label_id, page=1, per_page=50):
    return client.get("/labels/%s/releases" % label_id,
                      page=page, per_page=per_page)


def search_artist(client, name):
    data = client.get("/database/search", q=name, type="artist", per_page=5)
    for r in (data or {}).get("results", []) or []:
        if _ratio(name, r.get("title")) > 0.85:
            return {"id": r.get("id"), "name": r.get("title")}
    return None
