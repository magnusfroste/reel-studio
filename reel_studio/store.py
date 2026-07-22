"""Durable SQLite metadata for recording sessions and storyboard steps."""

from datetime import datetime, timezone
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any
import uuid

from .engine import output_root


_lock = threading.Lock()


def db_path() -> Path:
    configured = os.environ.get("REEL_DB_PATH")
    return Path(configured).expanduser() if configured else output_root() / "reel-studio.db"


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_schema() -> None:
    with _lock, _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                start_url TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('active', 'finished', 'error')),
                voice TEXT NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                finished_at TEXT,
                output_dir TEXT NOT NULL,
                video_path TEXT,
                video_url TEXT,
                duration_seconds REAL
            );
            CREATE TABLE IF NOT EXISTS steps (
                session_id TEXT NOT NULL REFERENCES sessions(id),
                idx INTEGER NOT NULL,
                action_type TEXT,
                target TEXT,
                url TEXT,
                title TEXT,
                narration_text TEXT,
                narration_duration REAL NOT NULL DEFAULT 0,
                offset_seconds REAL,
                screenshot_path TEXT,
                ok INTEGER NOT NULL,
                error_type TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (session_id, idx)
            );
            CREATE INDEX IF NOT EXISTS steps_session_idx
                ON steps (session_id, idx);
            CREATE TABLE IF NOT EXISTS backlog (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                detail TEXT NOT NULL,
                category TEXT NOT NULL,
                severity TEXT NOT NULL,
                session_id TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS backlog_created_idx
                ON backlog (created_at DESC);
            """
        )


def create_session(
    session_id: str,
    start_url: str,
    voice: str,
    width: int,
    height: int,
    output_dir: str,
) -> None:
    init_schema()
    with _lock, _connect() as connection:
        connection.execute(
            """
            INSERT INTO sessions
                (id, start_url, status, voice, width, height, created_at, output_dir)
            VALUES (?, ?, 'active', ?, ?, ?, ?, ?)
            """,
            (session_id, start_url, voice, width, height, _now(), output_dir),
        )


def append_step(
    session_id: str,
    action_type: str | None,
    target: str | None,
    url: str | None,
    title: str | None,
    narration_text: str,
    narration_duration: float,
    offset_seconds: float | None,
    screenshot_path: str | None,
    ok: bool,
    error_type: str | None,
) -> None:
    init_schema()
    with _lock, _connect() as connection:
        next_idx = connection.execute(
            "SELECT COALESCE(MAX(idx) + 1, 0) FROM steps WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO steps
                (session_id, idx, action_type, target, url, title, narration_text,
                 narration_duration, offset_seconds, screenshot_path, ok, error_type,
                 created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                next_idx,
                action_type,
                target,
                url,
                title,
                narration_text,
                narration_duration,
                offset_seconds,
                screenshot_path,
                int(ok),
                error_type,
                _now(),
            ),
        )


def finish_session(
    session_id: str,
    video_path: str,
    video_url: str | None,
    duration_seconds: float,
) -> None:
    init_schema()
    with _lock, _connect() as connection:
        connection.execute(
            """
            UPDATE sessions
            SET status = 'finished', finished_at = ?, video_path = ?,
                video_url = ?, duration_seconds = ?
            WHERE id = ?
            """,
            (_now(), video_path, video_url, duration_seconds, session_id),
        )


def list_sessions(limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    init_schema()
    with _lock, _connect() as connection:
        rows = connection.execute(
            """
            SELECT s.*, COUNT(st.idx) AS step_count
            FROM sessions s
            LEFT JOIN steps st ON st.session_id = s.id
            GROUP BY s.id
            ORDER BY s.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_finished_sessions() -> list[dict[str, Any]]:
    init_schema()
    with _lock, _connect() as connection:
        rows = connection.execute(
            """
            SELECT id, start_url, duration_seconds, finished_at, video_path, video_url
            FROM sessions
            WHERE status = 'finished'
            ORDER BY finished_at DESC, created_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def create_backlog(
    title: str,
    detail: str,
    category: str,
    severity: str,
    session_id: str | None,
) -> dict[str, Any]:
    init_schema()
    item = {
        "id": uuid.uuid4().hex,
        "title": title,
        "detail": detail,
        "category": category,
        "severity": severity,
        "session_id": session_id,
        "status": "open",
        "created_at": _now(),
    }
    with _lock, _connect() as connection:
        connection.execute(
            """
            INSERT INTO backlog
                (id, title, detail, category, severity, session_id, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(item.values()),
        )
    return item


def list_backlog(
    limit: int = 50,
    status: str | None = None,
    category: str | None = None,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    init_schema()
    with _lock, _connect() as connection:
        clauses = []
        parameters: list[Any] = []
        if status:
            clauses.append("status = ?")
            parameters.append(status)
        if category:
            clauses.append("category = ?")
            parameters.append(category)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        rows = connection.execute(
            f"""
            SELECT id, title, detail, category, severity, session_id, status, created_at
            FROM backlog
            {where}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            parameters,
        ).fetchall()
    return [dict(row) for row in rows]


def get_session(session_id: str) -> dict[str, Any] | None:
    init_schema()
    with _lock, _connect() as connection:
        session = connection.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if session is None:
            return None
        steps = connection.execute(
            "SELECT * FROM steps WHERE session_id = ? ORDER BY idx",
            (session_id,),
        ).fetchall()
    result = dict(session)
    result["steps"] = [dict(step) for step in steps]
    return result


def get_status(session_id: str) -> dict[str, Any] | None:
    session = get_session(session_id)
    if session is None:
        return None
    steps = session["steps"]
    narrated = sum(step["narration_duration"] or 0 for step in steps)
    duration = session["duration_seconds"]
    return {
        "elapsed_seconds": duration or 0,
        "recorded_steps": len(steps),
        "total_narrated_seconds": round(narrated, 3),
        "estimated_video_length": duration or 0,
        "status": session["status"],
        "stale": session["status"] == "active",
    }
