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
BACKLOG_STATUSES = ("open", "planned", "in_progress", "shipped", "wont_fix")


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
                provider TEXT NOT NULL DEFAULT 'edge',
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                output_width INTEGER,
                output_height INTEGER,
                title TEXT NOT NULL DEFAULT '',
                subtitle TEXT NOT NULL DEFAULT '',
                accent TEXT NOT NULL DEFAULT '#1f2a44',
                cta_url TEXT NOT NULL DEFAULT '',
                cta_text TEXT NOT NULL DEFAULT 'Learn more',
                music TEXT NOT NULL DEFAULT 'none',
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
                voice TEXT,
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
            CREATE TABLE IF NOT EXISTS shots (
                session_id TEXT NOT NULL REFERENCES sessions(id),
                shot_idx INTEGER NOT NULL,
                shot_id TEXT NOT NULL,
                intent TEXT NOT NULL,
                framing TEXT NOT NULL,
                zoom REAL,
                focus_ref TEXT,
                focus_text TEXT,
                status TEXT NOT NULL DEFAULT 'planned',
                verified INTEGER,
                verification_note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (session_id, shot_id)
            );
            CREATE INDEX IF NOT EXISTS shots_session_idx
                ON shots (session_id, shot_idx);
            CREATE TABLE IF NOT EXISTS backlog (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                detail TEXT NOT NULL,
                category TEXT NOT NULL,
                severity TEXT NOT NULL,
                session_id TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                note TEXT,
                updated_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS backlog_created_idx
                ON backlog (created_at DESC);
            """
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(steps)").fetchall()
        }
        if "voice" not in columns:
            connection.execute("ALTER TABLE steps ADD COLUMN voice TEXT")
        session_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "provider" not in session_columns:
            connection.execute(
                "ALTER TABLE sessions ADD COLUMN provider TEXT NOT NULL DEFAULT 'edge'"
            )
        for column in ("output_width", "output_height"):
            if column not in session_columns:
                connection.execute(f"ALTER TABLE sessions ADD COLUMN {column} INTEGER")
        for column, definition in (
            ("title", "TEXT NOT NULL DEFAULT ''"),
            ("subtitle", "TEXT NOT NULL DEFAULT ''"),
            ("accent", "TEXT NOT NULL DEFAULT '#1f2a44'"),
            ("cta_url", "TEXT NOT NULL DEFAULT ''"),
            ("cta_text", "TEXT NOT NULL DEFAULT 'Learn more'"),
            ("music", "TEXT NOT NULL DEFAULT 'none'"),
        ):
            if column not in session_columns:
                connection.execute(
                    f"ALTER TABLE sessions ADD COLUMN {column} {definition}"
                )
        backlog_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(backlog)").fetchall()
        }
        if "note" not in backlog_columns:
            connection.execute("ALTER TABLE backlog ADD COLUMN note TEXT")
        if "updated_at" not in backlog_columns:
            connection.execute("ALTER TABLE backlog ADD COLUMN updated_at TEXT")
        connection.execute(
            "UPDATE backlog SET updated_at = created_at WHERE updated_at IS NULL"
        )


def normalize_backlog_status(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    return normalized if normalized in BACKLOG_STATUSES else "open"


def create_session(
    session_id: str,
    start_url: str,
    voice: str,
    width: int,
    height: int,
    output_dir: str,
    provider: str = "edge",
    output_width: int | None = None,
    output_height: int | None = None,
    title: str = "",
    subtitle: str = "",
    accent: str = "#1f2a44",
    cta_url: str = "",
    cta_text: str = "Learn more",
    music: str = "none",
) -> None:
    init_schema()
    with _lock, _connect() as connection:
        connection.execute(
            """
            INSERT INTO sessions
                (id, start_url, status, voice, provider, width, height,
                 output_width, output_height, title, subtitle, accent, cta_url,
                 cta_text, music, created_at, output_dir)
            VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id, start_url, voice, provider, width, height,
                output_width, output_height, title, subtitle, accent, cta_url,
                cta_text, music, _now(), output_dir,
            ),
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
    voice: str | None = None,
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
                 voice, narration_duration, offset_seconds, screenshot_path, ok,
                 error_type, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                next_idx,
                action_type,
                target,
                url,
                title,
                narration_text,
                voice,
                narration_duration,
                offset_seconds,
                screenshot_path,
                int(ok),
                error_type,
                _now(),
            ),
        )


def update_step_narration(
    session_id: str,
    index: int,
    narration: str,
    voice: str | None = None,
    narration_duration: float | None = None,
) -> dict[str, Any] | None:
    init_schema()
    with _lock, _connect() as connection:
        row = connection.execute(
            "SELECT * FROM steps WHERE session_id = ? AND idx = ?",
            (session_id, index),
        ).fetchone()
        if row is None:
            return None
        if narration_duration is None:
            connection.execute(
                """
                UPDATE steps
                SET narration_text = ?, voice = COALESCE(?, voice)
                WHERE session_id = ? AND idx = ?
                """,
                (narration, voice, session_id, index),
            )
        else:
            connection.execute(
                """
                UPDATE steps
                SET narration_text = ?, voice = COALESCE(?, voice),
                    narration_duration = ?
                WHERE session_id = ? AND idx = ?
                """,
                (narration, voice, narration_duration, session_id, index),
            )
        updated = connection.execute(
            "SELECT * FROM steps WHERE session_id = ? AND idx = ?",
            (session_id, index),
        ).fetchone()
    return dict(updated)


def update_session_duration(session_id: str, duration_seconds: float) -> None:
    init_schema()
    with _lock, _connect() as connection:
        connection.execute(
            "UPDATE sessions SET duration_seconds = ? WHERE id = ?",
            (duration_seconds, session_id),
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
            SELECT id, start_url, title, duration_seconds, finished_at, video_path,
                   video_url, output_dir
            FROM sessions
            WHERE status = 'finished'
            ORDER BY finished_at DESC, created_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def delete_session(session_id: str) -> None:
    """Delete a session and its storyboard steps atomically."""
    init_schema()
    with _lock, _connect() as connection:
        connection.execute("DELETE FROM steps WHERE session_id = ?", (session_id,))
        connection.execute("DELETE FROM shots WHERE session_id = ?", (session_id,))
        connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


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
        "note": None,
        "updated_at": _now(),
        "created_at": _now(),
    }
    with _lock, _connect() as connection:
        connection.execute(
            """
            INSERT INTO backlog
                (id, title, detail, category, severity, session_id, status,
                 note, updated_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            parameters.append(normalize_backlog_status(status))
        if category:
            clauses.append("category = ?")
            parameters.append(category)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        rows = connection.execute(
            f"""
            SELECT id, title, detail, category, severity, session_id, status,
                   note, updated_at, created_at
            FROM backlog
            {where}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            parameters,
        ).fetchall()
    return [dict(row) for row in rows]


def update_backlog(
    item_id: str,
    status: str,
    note: str | None = None,
) -> dict[str, Any] | None:
    init_schema()
    updated_at = _now()
    normalized_status = normalize_backlog_status(status)
    normalized_note = note.strip() if note is not None else None
    with _lock, _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE backlog
            SET status = ?, note = ?, updated_at = ?
            WHERE id = ?
            """,
            (normalized_status, normalized_note, updated_at, item_id),
        )
        if cursor.rowcount == 0:
            return None
        row = connection.execute(
            """
            SELECT id, title, detail, category, severity, session_id, status,
                   note, updated_at, created_at
            FROM backlog WHERE id = ?
            """,
            (item_id,),
        ).fetchone()
    return dict(row)


def begin_shot(
    session_id: str,
    shot_id: str,
    intent: str,
    framing: str,
    zoom: float | None = None,
    focus_ref: str | None = None,
    focus_text: str | None = None,
) -> dict[str, Any]:
    """Persist a director's planned shot for a session."""
    init_schema()
    now = _now()
    with _lock, _connect() as connection:
        next_idx = connection.execute(
            "SELECT COALESCE(MAX(shot_idx) + 1, 0) FROM shots WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO shots
                (session_id, shot_idx, shot_id, intent, framing, zoom, focus_ref,
                 focus_text, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?)
            """,
            (session_id, next_idx, shot_id, intent, framing, zoom, focus_ref,
             focus_text, now, now),
        )
    return get_shot(session_id, shot_id) or {}


def get_shot(session_id: str, shot_id: str) -> dict[str, Any] | None:
    init_schema()
    with _lock, _connect() as connection:
        row = connection.execute(
            "SELECT * FROM shots WHERE session_id = ? AND shot_id = ?",
            (session_id, shot_id),
        ).fetchone()
    return dict(row) if row else None


def verify_shot(
    session_id: str,
    shot_id: str,
    verified: bool,
    verification_note: str = "",
) -> dict[str, Any]:
    """Record whether a planned shot met its visual teaching criterion."""
    init_schema()
    status = "verified" if verified else "needs_review"
    with _lock, _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE shots
            SET status = ?, verified = ?, verification_note = ?, updated_at = ?
            WHERE session_id = ? AND shot_id = ?
            """,
            (status, int(verified), verification_note.strip(), _now(), session_id, shot_id),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"unknown shot: {shot_id}")
    return get_shot(session_id, shot_id) or {}


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
        shots = connection.execute(
            "SELECT * FROM shots WHERE session_id = ? ORDER BY shot_idx",
            (session_id,),
        ).fetchall()
    result = dict(session)
    result["steps"] = [dict(step) for step in steps]
    result["shots"] = [dict(shot) for shot in shots]
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
