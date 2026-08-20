"""Best-effort retention of finished session media and metadata."""

import os
from pathlib import Path
import re
import shutil
from typing import Any

from . import store
from .engine import output_root


SESSION_ID_PATTERN = re.compile(r"^[0-9a-f]+$")
DEFAULT_MAX_CLIPS = 50
DEFAULT_KEEP_RAW_CLIPS = 0


def _env_int(name: str, default: int) -> int:
    return normalize_limit(os.environ.get(name), default)


def normalize_limit(value: object, default: int) -> int:
    """Normalize a configured or explicitly supplied retention limit."""
    try:
        return max(0, int(str(value).strip())) if value is not None else default
    except (AttributeError, TypeError, ValueError):
        return default


def _session_directory(session: dict[str, Any], root: Path) -> Path:
    configured = session.get("output_dir")
    return Path(configured).expanduser() if configured else root / session["id"]


def _safe_session_directory(
    session_id: str,
    configured: Path,
    root: Path,
) -> Path:
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError(f"unsafe session id: {session_id}")
    target = configured.resolve()
    if target == root or target.parent != root or target.name != session_id:
        raise ValueError(f"unsafe session directory: {target}")
    return target


def delete_session_storage(session_id: str) -> dict[str, Any]:
    """Delete one finished session's media and metadata safely."""
    session = store.get_session(session_id)
    if session is None:
        return {"deleted": False, "session_id": session_id, "reason": "not_found"}
    if session.get("status") != "finished":
        raise ValueError("only finished sessions can be deleted")
    root = output_root().resolve()
    target = _safe_session_directory(
        session_id, _session_directory(session, root), root
    )
    if target.exists():
        shutil.rmtree(target)
    store.delete_session(session_id)
    return {"deleted": True, "session_id": session_id}


def prune_storage(max_clips: int, keep_raw_clips: int) -> dict[str, Any]:
    """Prune old finished sessions without touching shared storage."""
    max_clips = normalize_limit(max_clips, DEFAULT_MAX_CLIPS)
    keep_raw_clips = normalize_limit(keep_raw_clips, DEFAULT_KEEP_RAW_CLIPS)
    if max_clips > 0:
        keep_raw_clips = min(keep_raw_clips, max_clips)
    else:
        keep_raw_clips = 0
    root = output_root().resolve()
    sessions = store.list_finished_sessions()
    summary: dict[str, Any] = {
        "removed_sessions": [],
        "removed_raw": [],
        "kept": len(sessions) if max_clips == 0 else min(max_clips, len(sessions)),
        "errors": [],
    }
    for rank, session in enumerate(sessions):
        session_id = session["id"]
        directory = _session_directory(session, root)
        try:
            target = _safe_session_directory(session_id, directory, root)
            if max_clips > 0 and rank >= max_clips:
                shutil.rmtree(target)
                store.delete_session(session_id)
                summary["removed_sessions"].append(session_id)
            elif keep_raw_clips > 0 and rank >= keep_raw_clips:
                raw_path = target / "screen.mp4"
                if raw_path.is_file():
                    raw_path.unlink()
                    summary["removed_raw"].append(session_id)
        except Exception as exc:
            summary["errors"].append({
                "id": session_id,
                "message": str(exc),
            })
    return summary


def prune_from_env() -> dict[str, Any]:
    """Prune using REEL_MAX_CLIPS and REEL_KEEP_RAW_CLIPS."""
    max_clips, keep_raw_clips = retention_settings()
    return prune_storage(max_clips, keep_raw_clips)


def retention_settings() -> tuple[int, int]:
    """Return normalized retention limits from the environment."""
    return (
        _env_int("REEL_MAX_CLIPS", DEFAULT_MAX_CLIPS),
        _env_int("REEL_KEEP_RAW_CLIPS", DEFAULT_KEEP_RAW_CLIPS),
    )
