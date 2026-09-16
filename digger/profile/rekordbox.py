"""Rekordbox XML import.

His library is the ADN, and it is already written down. Play count matters
more than any declared favourite list: what he actually played beats what he
says he likes.

Rekordbox stores ratings as 0/51/102/153/204/255 for zero to five stars, and
splits the version between the Mix and Remixer attributes rather than putting
it in the title, so both are folded back into the title before normalizing.
"""

import os
import urllib.parse
import xml.etree.ElementTree as ET

from ..normalize import normalize_version, track_key, work_key

RATING_TO_STARS = {0: 0, 51: 1, 102: 2, 153: 3, 204: 4, 255: 5}


def _stars(value):
    try:
        raw = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if raw in RATING_TO_STARS:
        return RATING_TO_STARS[raw]
    return min(5, max(0, round(raw / 51.0)))


def _num(value, cast=float):
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def _full_title(attrs):
    """Rebuild 'Title (Remixer Remix)' from Rekordbox's split fields."""
    title = (attrs.get("Name") or "").strip()
    mix = (attrs.get("Mix") or "").strip()
    remixer = (attrs.get("Remixer") or "").strip()
    if mix and "(" not in title:
        if remixer and remixer.lower() not in mix.lower():
            title = "%s (%s %s)" % (title, remixer, mix)
        else:
            title = "%s (%s)" % (title, mix)
    elif remixer and "(" not in title:
        title = "%s (%s Remix)" % (title, remixer)
    return title


def _local_path(location):
    """Rekordbox stores a percent-encoded file:// URL. Turn it into a path."""
    if not location:
        return None
    raw = urllib.parse.unquote(location)
    for prefix in ("file://localhost/", "file:///", "file://"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    return raw.replace("/", os.sep) or None


def _cue_names(track_node):
    """Named hot cues and memory cues, in order of position.

    POSITION_MARK carries Name, Type (0 cue, 1 fade-in, 2 fade-out, 3 load,
    4 loop), Start, End and Num (-1 is a memory cue, 0+ are hot cues A, B,
    C...). Only the named ones are kept: an unnamed cue says where, a named
    one says what.
    """
    out = []
    for mark in track_node.findall("POSITION_MARK"):
        name = (mark.get("Name") or "").strip()
        if not name:
            continue
        out.append({"name": name,
                    "start": _num(mark.get("Start")),
                    "num": _num(mark.get("Num"), int)})
    out.sort(key=lambda c: c["start"] if c["start"] is not None else 0)
    return out


def parse(xml_path):
    """Yield one dict per track in the COLLECTION node."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    collection = root.find("COLLECTION")
    if collection is None:
        raise ValueError("no COLLECTION node: is this a Rekordbox XML export?")

    for node in collection.findall("TRACK"):
        a = node.attrib
        artist = (a.get("Artist") or "").strip()
        title = _full_title(a)
        if not artist or not title:
            continue
        yield {
            "track_key": track_key(artist, title),
            "work_key": work_key(artist, title),
            "artist": artist,
            "title": title,
            "version": normalize_version(title),
            "bpm": _num(a.get("AverageBpm")),
            "music_key": (a.get("Tonality") or "").strip() or None,
            "genre": (a.get("Genre") or "").strip() or None,
            "label": (a.get("Label") or "").strip() or None,
            "year": _num(a.get("Year"), int),
            "rating": _stars(a.get("Rating")),
            "play_count": _num(a.get("PlayCount"), int) or 0,
            "date_added": a.get("DateAdded"),
            "comments": (a.get("Comments") or "").strip() or None,
            "location": _local_path(a.get("Location")),
            # Five fields the official XML spec documents and the first
            # version of this parser ignored.
            #
            # colour        DJs colour-code crates by energy or by moment in
            #               the night. What each colour MEANS is personal, so
            #               it is extracted and never interpreted here.
            # last_played   play_count alone says a record was a weapon once.
            #               Forty plays last touched in 2019 is a different
            #               record from forty plays last month.
            # composer      the spec reads "Name of composer (or producer)",
            #               which is the credit Discogs could not give us on
            #               digital-only releases.
            # grouping      a free text field DJs use for their own tagging.
            # duration      long records behave differently in a set.
            "colour": (a.get("Colour") or "").strip() or None,
            "last_played": (a.get("LastPlayed") or "").strip() or None,
            "composer": (a.get("Composer") or "").strip() or None,
            "grouping": (a.get("Grouping") or "").strip() or None,
            "duration": _num(a.get("TotalTime")),
            # Cue names are his own annotations: a hot cue called "drop" or
            # "break" marks structure he found worth marking.
            "cues": _cue_names(node),
        }


def merge_versions(rows):
    """Collapse rows sharing a track_key.

    Rekordbox stores the original and the extended edit as two files, and
    they normalize to the same track_key. Overwriting one with the other
    would throw away its play count, so plays are summed, the best rating
    wins and the earliest date_added is kept. Aggregating here rather than
    in SQL also keeps a re-import idempotent.
    """
    merged = {}
    for r in rows:
        cur = merged.get(r["track_key"])
        if cur is None:
            merged[r["track_key"]] = dict(r)
            continue
        cur["play_count"] = (cur["play_count"] or 0) + (r["play_count"] or 0)
        cur["rating"] = max(cur["rating"] or 0, r["rating"] or 0)
        for field in ("bpm", "music_key", "genre", "label", "year",
                      "comments", "location", "colour", "composer",
                      "grouping", "duration"):
            if not cur.get(field) and r.get(field):
                cur[field] = r[field]
        if r.get("date_added") and (not cur.get("date_added")
                                    or r["date_added"] < cur["date_added"]):
            cur["date_added"] = r["date_added"]
        # The other way round for last_played: the most recent one wins.
        if r.get("last_played") and (not cur.get("last_played")
                                     or r["last_played"] > cur["last_played"]):
            cur["last_played"] = r["last_played"]
        if r.get("cues") and len(r["cues"]) > len(cur.get("cues") or []):
            cur["cues"] = r["cues"]
    return list(merged.values())


def load_into(conn, xml_path):
    """Import the collection. Returns a summary dict."""
    import json as _json

    parsed = list(parse(xml_path))
    rows = merge_versions(parsed)
    for r in rows:
        r["cues"] = _json.dumps(r.get("cues") or [], ensure_ascii=False) or None
    sql = """
        INSERT OR REPLACE INTO profile_tracks
          (track_key, work_key, artist, title, version, bpm, music_key,
           genre, label, year, rating, play_count, date_added, comments,
           location, colour, last_played, composer, grouping, duration, cues)
        VALUES (:track_key, :work_key, :artist, :title, :version, :bpm,
                :music_key, :genre, :label, :year, :rating, :play_count,
                :date_added, :comments, :location, :colour, :last_played,
                :composer, :grouping, :duration, :cues)
    """
    with conn:
        conn.executemany(sql, rows)
    played = [r for r in rows if (r["play_count"] or 0) > 0]
    return {
        "tracks": len(rows),
        "files": len(parsed),
        "merged_versions": len(parsed) - len(rows),
        "played_at_least_once": len(played),
        "total_plays": sum(r["play_count"] or 0 for r in rows),
        "with_bpm": sum(1 for r in rows if r["bpm"]),
        "with_key": sum(1 for r in rows if r["music_key"]),
        "rated": sum(1 for r in rows if r["rating"]),
    }


def colour_report(conn):
    """What his colour coding covers, without saying what it means.

    Rekordbox lets a DJ tag tracks with eight colours and the software
    attaches no meaning to them: red is peak time for one DJ and do-not-play
    for another. Guessing would poison the profile the same way guessing a
    playlist role would, so this counts and orders them and leaves the
    reading to him. Once he says what a colour means, put it in config.toml
    under [colours] and it becomes a set function like any other.
    """
    rows = conn.execute("""
        SELECT colour, COUNT(*) n, SUM(play_count) plays,
               ROUND(AVG(bpm), 1) bpm, ROUND(AVG(rating), 1) rating
        FROM profile_tracks WHERE colour IS NOT NULL AND colour != ''
        GROUP BY colour ORDER BY n DESC
    """).fetchall()
    total = conn.execute("SELECT COUNT(*) n FROM profile_tracks").fetchone()["n"]
    return {"total": total, "coloured": sum(r["n"] for r in rows),
            "colours": [dict(r) for r in rows]}


def cue_vocabulary(conn, limit=25):
    """The words he uses to name his cue points.

    A DJ who names a hot cue "break" or "drop" has annotated the structure
    of that record by hand. The vocabulary is his, so it is reported rather
    than matched against a fixed list.
    """
    import collections
    import json as _json

    counter = collections.Counter()
    for r in conn.execute("SELECT cues FROM profile_tracks WHERE cues IS NOT NULL"):
        try:
            for c in _json.loads(r["cues"]) or []:
                name = (c.get("name") or "").strip().lower()
                if name:
                    counter[name] += 1
        except Exception:
            continue
    return counter.most_common(limit)


def profile_summary(conn, min_plays=1):
    """The constants of his taste, measured on what he actually played."""
    out = {}
    q = """
        SELECT %s AS bucket, COUNT(*) n, SUM(play_count) plays
        FROM profile_tracks WHERE play_count >= ?
        GROUP BY bucket ORDER BY plays DESC LIMIT 20
    """
    for field in ("genre", "label", "music_key"):
        out[field] = [dict(r) for r in conn.execute(q % field, (min_plays,))]
    out["bpm_histogram"] = [dict(r) for r in conn.execute("""
        SELECT CAST(bpm / 2 AS INT) * 2 AS bucket, COUNT(*) n, SUM(play_count) plays
        FROM profile_tracks
        WHERE bpm IS NOT NULL AND bpm BETWEEN 100 AND 150 AND play_count >= ?
        GROUP BY bucket ORDER BY bucket
    """, (min_plays,))]
    out["most_played"] = [dict(r) for r in conn.execute("""
        SELECT artist, title, play_count, rating, bpm, music_key, label
        FROM profile_tracks ORDER BY play_count DESC, rating DESC LIMIT 30
    """)]
    out["top_artists"] = [dict(r) for r in conn.execute("""
        SELECT artist, COUNT(*) n, SUM(play_count) plays
        FROM profile_tracks GROUP BY LOWER(artist)
        ORDER BY plays DESC, n DESC LIMIT 25
    """)]
    return out


# --- Playlists, and the set function they encode ------------------------
#
# His brief asks for tracks classified as warm-up, peak, after, transition
# tool, "100% me", "I like it but it's commercial". Those are not genres,
# they are roles inside a night, and the digest slots are roles too. Without
# them the system proposes records that resemble him without knowing which
# hour of his set they belong to.
#
# Rekordbox already holds this: DJs name their playlists after exactly these
# roles. Reading them costs nothing and spares him tagging 150 tracks.

FUNCTION_PATTERNS = [
    ("warmup", ("warm up", "warmup", "warm-up", "opening", "opener",
                "early", "sunset", "chill", "ouverture", "debut")),
    ("peak", ("peak", "prime", "banger", "weapon", "bomb", "main",
              "heavy", "big room", "arme")),
    ("after", ("after", "closing", "close", "late", "sunrise", "deep night",
               "4am", "5am", "fin")),
    ("tool", ("tool", "transition", "loop", "acapella", "acca", "fx",
              "intro", "outro", "edit", "utility")),
    ("core", ("100%", "100 %", "signature", "classics", "classiques",
              "all time", "essentials", "moi", "me")),
    ("commercial", ("commercial", "mainstream", "radio", "pop", "crowd",
                    "grand public", "hits")),
    ("odd", ("weird", "bizarre", "strange", "experimental", "oddball",
             "curiosit")),
]


def classify_playlist(name):
    """Map a playlist name onto a set function, or None when it says nothing.

    Deliberately conservative: a playlist called "2024" or "Ibiza" carries no
    role, and guessing one would poison the profile.
    """
    if not name:
        return None
    low = name.lower()
    for function, needles in FUNCTION_PATTERNS:
        if any(n in low for n in needles):
            return function
    return None


def _walk_nodes(node, trail=()):
    """Yield (playlist_name, path, [track_ids]) for every leaf playlist."""
    for child in node.findall("NODE"):
        name = child.get("Name") or ""
        if child.get("Type") == "1":
            keys = [t.get("Key") for t in child.findall("TRACK") if t.get("Key")]
            yield name, trail + (name,), keys
        else:
            for item in _walk_nodes(child, trail + (name,)):
                yield item


def parse_playlists(xml_path):
    """Return {track_id: {"playlists": [...], "functions": set()}}."""
    root = ET.parse(xml_path).getroot()
    node = root.find("PLAYLISTS")
    if node is None:
        return {}, []

    by_track = {}
    summary = []
    for name, path, keys in _walk_nodes(node):
        # A folder name can carry the role even when the leaf does not,
        # e.g. "Peak Time / 2026".
        function = classify_playlist(name)
        if function is None:
            for part in reversed(path[:-1]):
                function = classify_playlist(part)
                if function:
                    break
        summary.append({"playlist": name, "path": " / ".join(path),
                        "function": function, "tracks": len(keys)})
        for key in keys:
            entry = by_track.setdefault(key, {"playlists": [], "functions": set()})
            entry["playlists"].append(name)
            if function:
                entry["functions"].add(function)
    return by_track, summary


def load_functions(conn, xml_path):
    """Attach set functions to already-imported profile tracks."""
    by_track, summary = parse_playlists(xml_path)
    if not by_track:
        return {"playlists": 0, "tagged": 0, "by_function": {}}

    # TrackID is only meaningful inside the XML, so re-read the collection to
    # map it onto the track_key the rest of the system uses.
    root = ET.parse(xml_path).getroot()
    id_to_key = {}
    for t in root.find("COLLECTION").findall("TRACK"):
        artist = (t.get("Artist") or "").strip()
        title = _full_title(t.attrib)
        if artist and title and t.get("TrackID"):
            id_to_key[t.get("TrackID")] = track_key(artist, title)

    counts = {}
    rows = []
    for tid, info in by_track.items():
        key = id_to_key.get(tid)
        if not key or not info["functions"]:
            continue
        for fn in info["functions"]:
            counts[fn] = counts.get(fn, 0) + 1
        rows.append((",".join(sorted(info["functions"])),
                     ",".join(info["playlists"][:8]), key))

    with conn:
        conn.executemany(
            "UPDATE profile_tracks SET functions = ?, playlists = ? "
            "WHERE track_key = ?", rows)
    return {"playlists": len(summary), "tagged": len(rows),
            "by_function": counts, "summary": summary}
