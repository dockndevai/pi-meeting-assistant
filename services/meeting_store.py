"""
services/meeting_store.py

SQLite persistence for live meetings and their transcript segments.

Decision: SQLite over the old in-memory-only session
  A live meeting runs up to an hour and is watched by several screens.
  Losing it to a server restart, or showing a late-joining screen an
  empty feed, is not acceptable. SQLite is a single file, needs no extra
  container, and one insert per utterance is negligible on a Pi.

Decision: stdlib sqlite3, one shared connection, called from the event loop
  Writes are tiny (one row per utterance, a few per minute). Pushing them
  to a thread pool would add more overhead than the sub-millisecond write.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  REAL NOT NULL,
    ended_at    REAL,
    status      TEXT NOT NULL,          -- live | ended | interrupted
    source      TEXT NOT NULL DEFAULT 'mic',
    stt_backend TEXT
);
CREATE TABLE IF NOT EXISTS segments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id  INTEGER NOT NULL REFERENCES meetings(id),
    seq         INTEGER NOT NULL,
    text        TEXT NOT NULL,
    language    TEXT,
    offset_s    REAL NOT NULL,          -- seconds since meeting start
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_segments_meeting ON segments(meeting_id, seq);
"""


def _default_db_path() -> Path:
    env = os.environ.get("DB_PATH", "").strip()
    if env:
        return Path(env)
    if Path("/data").is_dir():
        return Path("/data/pmbot.db")
    return Path(__file__).parent.parent / "scratch" / "pmbot.db"


class MeetingStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or _default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ── Meetings ───────────────────────────────────────────────────────────
    def create_meeting(self, source: str) -> dict:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO meetings (started_at, status, source) VALUES (?, 'live', ?)",
                (now, source),
            )
            self._conn.commit()
            return self._get_meeting(cur.lastrowid)

    def get_meeting(self, meeting_id: int) -> Optional[dict]:
        with self._lock:
            return self._get_meeting(meeting_id)

    def _get_meeting(self, meeting_id: int) -> Optional[dict]:
        row = self._conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        return dict(row) if row else None

    _LAST_ACTIVITY = (
        "COALESCE((SELECT MAX(created_at) FROM segments s WHERE s.meeting_id = meetings.id), started_at)"
    )

    def latest_live_meeting(self, idle_s: float) -> Optional[dict]:
        """Most recent meeting still marked live with activity in the last idle_s seconds."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM meetings WHERE status = 'live' AND {self._LAST_ACTIVITY} > ? "
                "ORDER BY started_at DESC LIMIT 1",
                (time.time() - idle_s,),
            ).fetchone()
            return dict(row) if row else None

    def mark_stale_live_meetings(self, idle_s: float) -> None:
        """Meetings left 'live' by a crash, with no activity in idle_s, become 'interrupted'."""
        with self._lock:
            self._conn.execute(
                "UPDATE meetings SET status = 'interrupted', ended_at = ? "
                f"WHERE status = 'live' AND {self._LAST_ACTIVITY} <= ?",
                (time.time(), time.time() - idle_s),
            )
            self._conn.commit()

    def set_stt_backend(self, meeting_id: int, backend: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE meetings SET stt_backend = ? WHERE id = ?", (backend, meeting_id))
            self._conn.commit()

    def end_meeting(self, meeting_id: int) -> Optional[dict]:
        with self._lock:
            self._conn.execute(
                "UPDATE meetings SET status = 'ended', ended_at = ? WHERE id = ? AND status = 'live'",
                (time.time(), meeting_id),
            )
            self._conn.commit()
            return self._get_meeting(meeting_id)

    # ── Segments ───────────────────────────────────────────────────────────
    def add_segment(self, meeting_id: int, text: str, language: Optional[str], offset_s: float) -> dict:
        with self._lock:
            seq = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM segments WHERE meeting_id = ?", (meeting_id,)
            ).fetchone()[0]
            cur = self._conn.execute(
                "INSERT INTO segments (meeting_id, seq, text, language, offset_s, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (meeting_id, seq, text, language, offset_s, time.time()),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM segments WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(row)

    def segments(self, meeting_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM segments WHERE meeting_id = ? ORDER BY seq", (meeting_id,)
            ).fetchall()
            return [dict(r) for r in rows]


def format_transcript(segments: list[dict]) -> str:
    """Plain transcript for the LLM: one line per utterance with an [mm:ss] offset."""
    lines = []
    for s in segments:
        total = int(s["offset_s"])
        lines.append(f"[{total // 60:02d}:{total % 60:02d}] {s['text']}")
    return "\n".join(lines)
