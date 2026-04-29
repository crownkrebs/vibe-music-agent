"""SQLite storage: taste snapshots, feedback, learned rules, corrections, chat."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS taste_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    profile_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    context TEXT,
    playlist TEXT,
    track_id TEXT,
    track_name TEXT,
    track_artist TEXT,
    action TEXT,
    reason TEXT,
    audio_features TEXT
);
CREATE INDEX IF NOT EXISTS idx_feedback_action ON feedback(action);
CREATE INDEX IF NOT EXISTS idx_feedback_playlist ON feedback(playlist);

CREATE TABLE IF NOT EXISTS taste_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    rule TEXT UNIQUE,
    source TEXT,
    confidence REAL DEFAULT 0.5,
    hits INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    song_name TEXT,
    artist TEXT,
    from_playlist TEXT,
    to_playlist TEXT,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS chat_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    role TEXT,
    content TEXT,
    context TEXT
);

CREATE TABLE IF NOT EXISTS playlist_profiles (
    playlist TEXT PRIMARY KEY,
    profile_json TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS track_cache (
    track_id TEXT PRIMARY KEY,
    name TEXT,
    artist TEXT,
    artists_json TEXT,
    album TEXT,
    album_art TEXT,
    features_json TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS artist_genres (
    artist_id TEXT PRIMARY KEY,
    name TEXT,
    genres_json TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


EXPECTED_COLUMNS = {
    "taste_snapshots": {"id", "created_at", "profile_json"},
    "feedback": {"id", "created_at", "context", "playlist", "track_id",
                 "track_name", "track_artist", "action", "reason", "audio_features"},
    "taste_rules": {"id", "created_at", "rule", "source", "confidence", "hits"},
    "corrections": {"id", "created_at", "song_name", "artist",
                    "from_playlist", "to_playlist", "reason"},
    "chat_history": {"id", "created_at", "role", "content", "context"},
    "playlist_profiles": {"playlist", "profile_json", "updated_at"},
    "track_cache": {"track_id", "name", "artist", "artists_json",
                    "album", "album_art", "features_json", "updated_at"},
    "artist_genres": {"artist_id", "name", "genres_json", "updated_at"},
}


class DB:
    def __init__(self, path="music_agent.db"):
        self.path = str(Path(path))
        with self._conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    def _migrate(self, con):
        """Drop and recreate tables that have missing columns (v2 → v3 upgrade)."""
        cur = con.cursor()
        for table, expected in EXPECTED_COLUMNS.items():
            rows = cur.execute(f"PRAGMA table_info({table})").fetchall()
            existing = {r["name"] for r in rows}
            if not existing:
                continue
            if not expected.issubset(existing):
                cur.execute(f"DROP TABLE {table}")
        cur.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # taste snapshots
    def save_taste_snapshot(self, profile):
        with self._conn() as c:
            c.execute(
                "INSERT INTO taste_snapshots (profile_json) VALUES (?)",
                (json.dumps(profile),),
            )

    def latest_taste_snapshot(self):
        with self._conn() as c:
            row = c.execute(
                "SELECT profile_json, created_at FROM taste_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            return {"profile": json.loads(row["profile_json"]), "created_at": row["created_at"]}

    # feedback
    def insert_feedback(self, context, playlist, track_id, track_name, track_artist,
                        action, reason, audio_features):
        with self._conn() as c:
            c.execute(
                """INSERT INTO feedback
                (context, playlist, track_id, track_name, track_artist, action, reason, audio_features)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (context, playlist, track_id, track_name, track_artist, action, reason,
                 audio_features if isinstance(audio_features, str) else json.dumps(audio_features or {})),
            )

    def get_recent_feedback(self, limit=50, action=None, playlist=None):
        q = "SELECT * FROM feedback"
        conds, args = [], []
        if action:
            conds.append("action = ?")
            args.append(action)
        if playlist:
            conds.append("playlist = ?")
            args.append(playlist)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, args).fetchall()]

    # rules
    def upsert_rule(self, rule, confidence, source=None):
        with self._conn() as c:
            existing = c.execute(
                "SELECT id, confidence, hits FROM taste_rules WHERE rule = ?", (rule,)
            ).fetchone()
            if existing:
                new_conf = min(1.0, max(existing["confidence"], confidence))
                c.execute(
                    "UPDATE taste_rules SET confidence = ?, hits = hits + 1 WHERE id = ?",
                    (new_conf, existing["id"]),
                )
            else:
                c.execute(
                    "INSERT INTO taste_rules (rule, source, confidence) VALUES (?, ?, ?)",
                    (rule, source, confidence),
                )

    def get_rules(self, min_confidence=0.4):
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM taste_rules WHERE confidence >= ? ORDER BY confidence DESC",
                (min_confidence,),
            ).fetchall()]

    def delete_rule(self, rule_id):
        with self._conn() as c:
            c.execute("DELETE FROM taste_rules WHERE id = ?", (rule_id,))

    # corrections
    def insert_correction(self, song_name, artist, from_playlist, to_playlist, reason):
        with self._conn() as c:
            c.execute(
                """INSERT INTO corrections (song_name, artist, from_playlist, to_playlist, reason)
                VALUES (?, ?, ?, ?, ?)""",
                (song_name, artist, from_playlist, to_playlist, reason),
            )

    # chat
    def insert_chat(self, role, content, context=None):
        with self._conn() as c:
            c.execute(
                "INSERT INTO chat_history (role, content, context) VALUES (?, ?, ?)",
                (role, content, context),
            )

    def get_chat_history(self, limit=40):
        with self._conn() as c:
            rows = c.execute(
                "SELECT role, content, context, created_at FROM chat_history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    def clear_chat(self):
        with self._conn() as c:
            c.execute("DELETE FROM chat_history")

    # playlist profiles (evolved from feedback)
    def save_playlist_profile(self, playlist, profile):
        with self._conn() as c:
            c.execute(
                """INSERT INTO playlist_profiles (playlist, profile_json, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(playlist) DO UPDATE SET
                    profile_json = excluded.profile_json,
                    updated_at = CURRENT_TIMESTAMP""",
                (playlist, json.dumps(profile)),
            )

    def get_playlist_profile(self, playlist):
        with self._conn() as c:
            row = c.execute(
                "SELECT profile_json FROM playlist_profiles WHERE playlist = ?",
                (playlist,),
            ).fetchone()
            return json.loads(row["profile_json"]) if row else None

    # track cache
    def cache_track(self, track_id, name, artist, artists, album, album_art, features):
        with self._conn() as c:
            c.execute(
                """INSERT INTO track_cache
                (track_id, name, artist, artists_json, album, album_art, features_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(track_id) DO UPDATE SET
                    name=excluded.name, artist=excluded.artist,
                    artists_json=excluded.artists_json, album=excluded.album,
                    album_art=excluded.album_art, features_json=excluded.features_json,
                    updated_at=CURRENT_TIMESTAMP""",
                (track_id, name, artist,
                 json.dumps(artists or []), album, album_art,
                 json.dumps(features or {})),
            )

    def get_cached_track(self, track_id):
        with self._conn() as c:
            row = c.execute("SELECT * FROM track_cache WHERE track_id = ?", (track_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["artists"] = json.loads(d.pop("artists_json") or "[]")
            d["features"] = json.loads(d.pop("features_json") or "{}")
            return d

    # artist genres cache
    def cache_artist_genres(self, artist_id, name, genres):
        with self._conn() as c:
            c.execute(
                """INSERT INTO artist_genres (artist_id, name, genres_json, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(artist_id) DO UPDATE SET
                    name=excluded.name,
                    genres_json=excluded.genres_json,
                    updated_at=CURRENT_TIMESTAMP""",
                (artist_id, name or "", json.dumps(list(genres or []))),
            )

    def get_cached_artist_genres(self, artist_ids):
        """Returns {artist_id: [genres]} for any known ids; missing ids absent."""
        ids = [a for a in (artist_ids or []) if a]
        if not ids:
            return {}
        out = {}
        with self._conn() as c:
            # chunk the IN-list to avoid SQLite variable limits
            for i in range(0, len(ids), 500):
                batch = ids[i:i + 500]
                placeholders = ",".join("?" * len(batch))
                rows = c.execute(
                    f"SELECT artist_id, genres_json FROM artist_genres WHERE artist_id IN ({placeholders})",
                    batch,
                ).fetchall()
                for r in rows:
                    try:
                        out[r["artist_id"]] = json.loads(r["genres_json"] or "[]")
                    except Exception:
                        out[r["artist_id"]] = []
        return out
