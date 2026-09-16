"""SQLite storage.

Sightings are append-only evidence: one row per (source, source_id). The
cross-source score is derived from them at query time, never stored, so a
scoring change never requires a re-collect.
"""

import json
import os
import sqlite3

DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "digger.db"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sightings (
    id           INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    track_key    TEXT NOT NULL,
    work_key     TEXT NOT NULL,
    artist       TEXT NOT NULL,
    title        TEXT NOT NULL,
    version      TEXT NOT NULL DEFAULT '',
    url          TEXT,
    stream_url   TEXT,
    genre        TEXT,
    label        TEXT,
    location     TEXT,
    published_at TEXT,
    isrc         TEXT,
    bpm          REAL,
    music_key    TEXT,
    seen_at      TEXT NOT NULL DEFAULT (datetime('now')),
    raw          TEXT,
    UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_sightings_work ON sightings (work_key);
CREATE INDEX IF NOT EXISTS idx_sightings_track ON sightings (track_key);
CREATE INDEX IF NOT EXISTS idx_sightings_seen ON sightings (seen_at);

-- His own library, parsed from the Rekordbox XML export. This is the ADN.
CREATE TABLE IF NOT EXISTS profile_tracks (
    track_key   TEXT PRIMARY KEY,
    work_key    TEXT NOT NULL,
    artist      TEXT NOT NULL,
    title       TEXT NOT NULL,
    version     TEXT NOT NULL DEFAULT '',
    bpm         REAL,
    music_key   TEXT,
    genre       TEXT,
    label       TEXT,
    year        INTEGER,
    rating      INTEGER,
    play_count  INTEGER,
    date_added  TEXT,
    comments    TEXT
);
CREATE INDEX IF NOT EXISTS idx_profile_work ON profile_tracks (work_key);

-- His verdict on what we proposed. The 'why' is the only field that matters.
CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY,
    work_key   TEXT NOT NULL,
    verdict    TEXT NOT NULL,
    why        TEXT,
    slot       TEXT,
    rated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_feedback_work ON feedback (work_key);

-- What we actually shipped each week, so hit rate is measurable afterwards.
CREATE TABLE IF NOT EXISTS digest_items (
    id         INTEGER PRIMARY KEY,
    week       TEXT NOT NULL,
    work_key   TEXT NOT NULL,
    slot       TEXT NOT NULL,
    rank       INTEGER,
    reason     TEXT,
    UNIQUE (week, work_key)
);

-- Timbre, computed locally. These have no ground truth to validate
-- against, unlike bpm and key: they are internally consistent and therefore
-- comparable between tracks, which is what affinity needs.
CREATE TABLE IF NOT EXISTS audio_features (
    work_key         TEXT PRIMARY KEY,
    track_key        TEXT,
    origin           TEXT NOT NULL,
    bpm              REAL,
    bpm_confidence   REAL,
    music_key        TEXT,
    camelot          TEXT,
    key_confidence   REAL,
    energy           REAL,
    dynamics         REAL,
    brightness       REAL,
    percussive_ratio REAL,
    onset_rate       REAL,
    duration         REAL,
    analyzed_at      TEXT NOT NULL DEFAULT (datetime('now')),
    error            TEXT
);
CREATE INDEX IF NOT EXISTS idx_feat_origin ON audio_features (origin);

CREATE TABLE IF NOT EXISTS run_log (
    id        INTEGER PRIMARY KEY,
    source    TEXT NOT NULL,
    started   TEXT NOT NULL,
    finished  TEXT,
    n_new     INTEGER DEFAULT 0,
    n_seen    INTEGER DEFAULT 0,
    error     TEXT
);
"""


def connect(path=None):
    # DIGGER_DB points every caller somewhere else, so a smoke test of a CLI
    # command cannot land in the real database. Eight invented verdicts once
    # did, and two real records sat silently excluded from the digest until
    # someone thought to look at the feedback table.
    path = path or os.environ.get("DIGGER_DB") or DEFAULT_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    """Add columns that post-date the first schema, then their indexes.

    Indexes on these columns cannot sit in SCHEMA itself: executescript runs
    against a table that may predate the column and fails before the ALTER.
    """
    existing = {r[1] for r in conn.execute("PRAGMA table_info(sightings)")}
    for col, decl in (("isrc", "TEXT"), ("bpm", "REAL"), ("music_key", "TEXT")):
        if col not in existing:
            conn.execute("ALTER TABLE sightings ADD COLUMN %s %s" % (col, decl))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sightings_isrc ON sightings (isrc)")

    # The Rekordbox XML carries a file:// path per track, so his own library
    # can be analysed from the real files rather than from 30 second previews.
    prof = {r[1] for r in conn.execute("PRAGMA table_info(profile_tracks)")}
    if "location" not in prof:
        conn.execute("ALTER TABLE profile_tracks ADD COLUMN location TEXT")
    # Set function (warm-up, peak, after, tool...) read from his playlist
    # names. His brief asks for exactly this classification.
    if "functions" not in prof:
        conn.execute("ALTER TABLE profile_tracks ADD COLUMN functions TEXT")
    if "playlists" not in prof:
        conn.execute("ALTER TABLE profile_tracks ADD COLUMN playlists TEXT")
    # Documented in Pioneer's own XML spec and missed by the first parser:
    # his colour coding, when he last played a record, the producer credit,
    # his free-text grouping, the duration, and the cues he named himself.
    for col, decl in (("colour", "TEXT"), ("last_played", "TEXT"),
                      ("composer", "TEXT"), ("grouping", "TEXT"),
                      ("duration", "REAL"), ("cues", "TEXT")):
        if col not in prof:
            conn.execute("ALTER TABLE profile_tracks ADD COLUMN %s %s" % (col, decl))
    conn.commit()


def upsert_sightings(conn, rows):
    """Insert sightings, ignoring ones already recorded. Returns count inserted."""
    sql = """
        INSERT OR IGNORE INTO sightings
          (source, source_id, track_key, work_key, artist, title, version,
           url, stream_url, genre, label, location, published_at,
           isrc, bpm, music_key, raw)
        VALUES (:source, :source_id, :track_key, :work_key, :artist, :title,
                :version, :url, :stream_url, :genre, :label, :location,
                :published_at, :isrc, :bpm, :music_key, :raw)
    """
    before = conn.total_changes
    with conn:
        for r in rows:
            r = dict(r)
            if isinstance(r.get("raw"), (dict, list)):
                r["raw"] = json.dumps(r["raw"], ensure_ascii=False)
            r.setdefault("stream_url", None)
            r.setdefault("label", None)
            r.setdefault("location", None)
            r.setdefault("genre", None)
            r.setdefault("published_at", None)
            r.setdefault("url", None)
            r.setdefault("isrc", None)
            r.setdefault("bpm", None)
            r.setdefault("music_key", None)
            r.setdefault("raw", None)
            conn.execute(sql, r)
    return conn.total_changes - before
