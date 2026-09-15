"""Beatport API v4.

Beatport killed API v3 and no longer grants developer credentials the normal
way. The working path, as used by the beets-beatport4 plugin, is to scrape
the public client ID out of the scripts on api.beatport.com/v4/docs/ and then
run the standard OAuth authorization_code flow with a real Beatport account.

This is a workaround against an endpoint Beatport does not publish. It works
today; it can stop working whenever they change the docs bundle.
"""

import json
import os
import re
import time
import urllib.parse

import requests

API_BASE = "https://api.beatport.com/v4"
REDIRECT_URI = API_BASE + "/auth/o/post-message/"
_HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOKEN_CACHE = os.path.join(_HERE, "data", "beatport_token.json")

SCRIPT_SRC_RE = re.compile(r'<script[^>]+src="([^"]+)"')
CLIENT_ID_RE = re.compile(r"API_CLIENT_ID\s*[:=]\s*[\"']([A-Za-z0-9]+)[\"']")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


class BeatportError(RuntimeError):
    pass


class BeatportClient:
    def __init__(self, username, password, client_id=None, token_path=None):
        self.username = username
        self.password = password
        self.client_id = client_id
        self.token_path = token_path or TOKEN_CACHE
        self.token = self._load_token()
        if not self._token_valid():
            self.token = self._authorize()
            self._save_token()

    # --- token plumbing
    def _load_token(self):
        try:
            with open(self.token_path) as fh:
                return json.load(fh)
        except Exception:
            return None

    def _save_token(self):
        os.makedirs(os.path.dirname(self.token_path), exist_ok=True)
        with open(self.token_path, "w") as fh:
            json.dump(self.token, fh)

    def _token_valid(self):
        if not self.token or not self.token.get("access_token"):
            return False
        if self.token.get("expires_at", 0) < time.time() + 60:
            return False
        try:
            self.get("/my/account/")
            return True
        except Exception:
            return False

    # --- auth
    def _fetch_client_id(self):
        html = requests.get(API_BASE + "/docs/", timeout=20,
                            headers={"User-Agent": UA}).text
        for src in SCRIPT_SRC_RE.findall(html):
            url = src if src.startswith("http") else "https://api.beatport.com" + src
            try:
                js = requests.get(url, timeout=20, headers={"User-Agent": UA}).text
            except Exception:
                continue
            found = CLIENT_ID_RE.findall(js)
            if found:
                return found[0]
        raise BeatportError("could not scrape API_CLIENT_ID from the docs bundle")

    def _authorize(self):
        if not self.client_id:
            self.client_id = self._fetch_client_id()
        with requests.Session() as s:
            s.headers.update({"User-Agent": UA})
            r = s.post(API_BASE + "/auth/login/",
                       json={"username": self.username, "password": self.password},
                       timeout=20)
            r.raise_for_status()
            data = r.json()
            if "username" not in data:
                raise BeatportError("login rejected: %s" % data)

            q = urllib.parse.urlencode({
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": REDIRECT_URI})
            r = s.get(API_BASE + "/auth/o/authorize/?" + q,
                      allow_redirects=False, timeout=20)
            loc = r.headers.get("Location")
            if not loc:
                raise BeatportError(
                    "no Location header on authorize (status %s)" % r.status_code)
            parsed = urllib.parse.urlparse(loc)
            codes = urllib.parse.parse_qs(parsed.query).get("code")
            if not codes:
                raise BeatportError("no authorization code in redirect: %s" % loc)

            q = urllib.parse.urlencode({
                "code": codes[0],
                "grant_type": "authorization_code",
                "redirect_uri": REDIRECT_URI,
                "client_id": self.client_id})
            r = s.post(API_BASE + "/auth/o/token/?" + q, timeout=20)
            r.raise_for_status()
            tok = r.json()
            tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600))
            return tok

    # --- calls
    def get(self, path, **params):
        r = requests.get(
            API_BASE + path,
            headers={"Authorization": "Bearer %s" % self.token["access_token"],
                     "User-Agent": UA},
            params=params, timeout=25)
        if r.status_code == 401:
            raise BeatportError("unauthorized")
        r.raise_for_status()
        return r.json()

    # --- what we actually need
    def charts(self, page=1, per_page=100, **filters):
        """DJ charts. Filter by genre_id and by publish date range."""
        return self.get("/catalog/charts/", page=page, per_page=per_page, **filters)

    def chart_tracks(self, chart_id, page=1, per_page=100):
        return self.get("/catalog/charts/%s/tracks/" % chart_id,
                        page=page, per_page=per_page)

    def top100(self, genre_id=None, page=1, per_page=100):
        params = {"page": page, "per_page": per_page}
        if genre_id:
            params["genre_id"] = genre_id
        return self.get("/catalog/top/100/", **params)

    def genres(self):
        return self.get("/catalog/genres/", per_page=200)

    def search_tracks(self, query, page=1, per_page=50, **filters):
        return self.get("/catalog/search/", q=query, type="tracks",
                        page=page, per_page=per_page, **filters)


# --- Collector ----------------------------------------------------------
#
# DJ charts are the point of this source. Beatport exposes 10k of them and
# the freshest are hours old, so what DJs are actually pushing this week is
# readable directly. Chart tracks also carry ISRC, BPM and key, which no
# other source gives us for free.

from ..normalize import iso_date, normalize_version, track_key, work_key


def _chart_track_to_sighting(t, chart):
    artists = [a.get("name", "") for a in (t.get("artists") or []) if a.get("name")]
    remixers = [a.get("name", "") for a in (t.get("remixers") or []) if a.get("name")]
    artist = ", ".join(artists)
    title = (t.get("name") or "").strip()
    if not artist or not title:
        return None

    # Beatport keeps the version in mix_name, not in the title.
    mix = (t.get("mix_name") or "").strip()
    if mix:
        if remixers and remixers[0].lower() not in mix.lower():
            title = "%s (%s %s)" % (title, remixers[0], mix)
        else:
            title = "%s (%s)" % (title, mix)

    key = t.get("key")
    if isinstance(key, dict):
        key = key.get("name")
    genre = t.get("genre")
    if isinstance(genre, dict):
        genre = genre.get("name")
    release = t.get("release") or {}
    label = None
    if isinstance(release, dict):
        label = (release.get("label") or {}).get("name")

    return {
        "source": "beatport",
        "source_id": "c%s-t%s" % (chart.get("id"), t.get("id")),
        "track_key": track_key(artist, title),
        "work_key": work_key(artist, title),
        "artist": artist,
        "title": title,
        "version": normalize_version(title),
        "url": "https://www.beatport.com/track/x/%s" % t.get("id"),
        "stream_url": t.get("sample_url"),
        "genre": genre,
        "label": label,
        "location": None,
        "published_at": iso_date(t.get("new_release_date") or t.get("publish_date")),
        "isrc": t.get("isrc"),
        "bpm": t.get("bpm"),
        "music_key": key,
        "raw": {"chart": chart.get("name"), "chart_id": chart.get("id"),
                "length": t.get("length")},
    }


def collect(client, n_charts=25, per_chart=40, want_genres=()):
    """Walk the freshest DJ charts and flatten them into track sightings.

    want_genres filters on each track's own genre rather than on the chart,
    because the chart-level genre filter does not actually narrow the list
    (verified: count stays at 10000 with or without genre_id).
    """
    out = []
    charts = client.charts(page=1, per_page=n_charts).get("results", [])
    for chart in charts:
        try:
            tracks = client.chart_tracks(chart["id"], per_page=per_chart)
        except Exception:
            continue
        for t in tracks.get("results", []):
            s = _chart_track_to_sighting(t, chart)
            if not s:
                continue
            if want_genres and (s["genre"] or "") not in want_genres:
                continue
            out.append(s)
    return out


# --- Labels -------------------------------------------------------------
#
# His brief lists labels alongside charts as a source, and they are a
# different signal: a chart says a DJ liked a record, a label roster says an
# A&R he trusts keeps signing a sound. Following twenty labels catches
# records before any DJ charts them.
#
# Endpoint notes, probed live 2026-09-15:
#   /catalog/labels/?name=X    filters properly
#   /catalog/labels/?q=X       does NOT filter, it returns arbitrary labels
#   /catalog/labels/{id}/tracks/     404
#   /catalog/labels/{id}/releases/   works
# So the path to a label's new tracks runs through its releases.

def find_label(client, name):
    """Resolve a label name to its Beatport entry. Exact match wins."""
    data = client.get("/catalog/labels/", name=name, per_page=20)
    results = data.get("results", [])
    if not results:
        return None
    exact = [r for r in results if (r.get("name") or "").lower() == name.lower()]
    return (exact or results)[0]


def label_releases(client, label_id, page=1, per_page=50):
    return client.get("/catalog/labels/%s/releases/" % label_id,
                      page=page, per_page=per_page)


def release_tracks(client, release_id, per_page=50):
    return client.get("/catalog/releases/%s/tracks/" % release_id,
                      per_page=per_page)


def collect_labels(client, label_names, since_days=30, max_releases_per_label=8,
                   on_label=None):
    """Walk recent releases of the labels he follows and flatten to tracks."""
    from datetime import date, timedelta

    cutoff = (date.today() - timedelta(days=since_days)).isoformat()
    out = []
    for name in label_names:
        try:
            label = find_label(client, name)
        except Exception:
            label = None
        if not label:
            if on_label:
                on_label(name, None, 0, "label introuvable")
            continue

        n_before = len(out)
        try:
            releases = label_releases(client, label["id"]).get("results", [])
        except Exception as exc:
            if on_label:
                on_label(name, label, 0, str(exc)[:60])
            continue

        fresh = [r for r in releases
                 if (r.get("new_release_date") or r.get("publish_date") or "") >= cutoff]
        for rel in fresh[:max_releases_per_label]:
            try:
                tracks = release_tracks(client, rel["id"]).get("results", [])
            except Exception:
                continue
            pseudo_chart = {"id": "label%s" % label["id"],
                            "name": "label: %s" % label["name"]}
            for t in tracks:
                s = _chart_track_to_sighting(t, pseudo_chart)
                if not s:
                    continue
                # Overwrite the source so label finds never inflate the
                # "charted by N DJs" count, which must stay DJ-only.
                s["source"] = "label"
                s["source_id"] = "l%s-t%s" % (label["id"], t.get("id"))
                s["label"] = label["name"]
                out.append(s)
        if on_label:
            on_label(name, label, len(out) - n_before, None)
    return out


def labels_from_profile(conn, limit=25, min_tracks=2):
    """The labels he actually plays, most played first.

    Better than a hand-written list: it cannot contain a label he has never
    touched, and it updates itself every time his library is re-imported.
    """
    rows = conn.execute("""
        SELECT label, COUNT(*) n, SUM(play_count) plays
        FROM profile_tracks
        WHERE label IS NOT NULL AND TRIM(label) != ''
        GROUP BY LOWER(label)
        HAVING n >= ?
        ORDER BY plays DESC, n DESC
        LIMIT ?
    """, (min_tracks, limit)).fetchall()
    return [r["label"] for r in rows]


# --- Enrichment ---------------------------------------------------------
#
# A fingerprinted set gives an artist and a title and nothing else: no BPM,
# no key, no label, no preview. Those are the best records in the database,
# actually played by DJs he follows, and they were the least usable ones.
#
# Shazam returns an ISRC on most matches, and Beatport's ?isrc= filter is an
# exact lookup (unlike ?q=, which ignores the query and returns the same
# arbitrary rows for anything). So a played track resolves to its catalogue
# entry, which carries everything the scorer and the page need, plus the
# link to buy it.

def find_by_isrc(client, isrc):
    if not isrc:
        return None
    data = client.get("/catalog/tracks/", isrc=isrc, per_page=5)
    results = data.get("results") or []
    return results[0] if results else None


def find_by_name(client, artist, title):
    """Fallback for tracks Shazam gave no ISRC for.

    Filters on the track name, then checks the artist by hand: Beatport's
    name filter is loose enough to return other people's records with the
    same title, and crediting a play to the wrong artist is worse than
    leaving the row thin.
    """
    if not title:
        return None
    clean = title.split("(")[0].strip()
    try:
        data = client.get("/catalog/tracks/", name=clean, per_page=20)
    except Exception:
        return None
    wanted = {w for w in (artist or "").lower().replace("&", ",").split(",")}
    wanted = {w.strip() for w in wanted if w.strip()}
    if not wanted:
        return None
    for t in data.get("results") or []:
        names = {a.get("name", "").lower() for a in (t.get("artists") or [])}
        if names & wanted:
            return t
    return None


def enrich_setlist(conn, client, limit=200, on_each=None):
    """Fill in the blanks on tracks that only fingerprinting knows about."""
    rows = conn.execute("""
        SELECT id, artist, title, isrc FROM sightings
        WHERE source = 'setlist' AND bpm IS NULL
        ORDER BY isrc IS NULL, id LIMIT ?
    """, (limit,)).fetchall()

    found = missed = 0
    for r in rows:
        track = None
        how = None
        try:
            track = find_by_isrc(client, r["isrc"])
            how = "isrc" if track else None
            if not track:
                track = find_by_name(client, r["artist"], r["title"])
                how = "nom" if track else None
        except Exception:
            track = None

        if not track:
            missed += 1
            if on_each:
                on_each(r, None, None)
            continue

        key = track.get("key")
        if isinstance(key, dict):
            key = key.get("name")
        genre = track.get("genre")
        if isinstance(genre, dict):
            genre = genre.get("name")
        release = track.get("release") or {}
        label = None
        if isinstance(release, dict):
            label = (release.get("label") or {}).get("name")

        with conn:
            conn.execute("""
                UPDATE sightings
                SET bpm = ?, music_key = ?, genre = ?, label = ?,
                    url = COALESCE(url, ?), stream_url = COALESCE(stream_url, ?),
                    isrc = COALESCE(isrc, ?)
                WHERE id = ?
            """, (track.get("bpm"), key, genre, label,
                  "https://www.beatport.com/track/x/%s" % track.get("id"),
                  track.get("sample_url"), track.get("isrc"), r["id"]))
        found += 1
        if on_each:
            on_each(r, track, how)
    return {"looked_up": len(rows), "found": found, "missed": missed}
