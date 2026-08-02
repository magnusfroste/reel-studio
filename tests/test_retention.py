import time

from reel_studio import store
from reel_studio.retention import prune_storage


def _finished_session(tmp_path, session_id: str) -> None:
    directory = tmp_path / session_id
    directory.mkdir()
    (directory / "screen.mp4").write_bytes(b"raw")
    (directory / "video.mp4").write_bytes(b"final")
    store.create_session(
        session_id,
        "https://example.com",
        "en-US-JennyNeural",
        640,
        360,
        str(directory),
    )
    store.finish_session(session_id, str(directory / "video.mp4"), None, 1.0)
    time.sleep(0.01)


def test_prune_finished_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("REEL_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("REEL_DB_PATH", str(tmp_path / "reel-studio.db"))

    oldest = "a" * 32
    middle = "b" * 32
    newest = "c" * 32
    _finished_session(tmp_path, oldest)
    _finished_session(tmp_path, middle)
    _finished_session(tmp_path, newest)
    store.append_step(
        oldest,
        "wait",
        None,
        None,
        None,
        "A step to delete",
        1.0,
        0.0,
        None,
        True,
        None,
    )
    backlog = store.create_backlog(
        "Keep this row",
        "Retention must not delete backlog metadata.",
        "feature",
        "low",
        oldest,
    )

    summary = prune_storage(max_clips=2, keep_raw_clips=1)

    assert summary["removed_sessions"] == [oldest]
    assert summary["removed_raw"] == [middle]
    assert summary["errors"] == []
    assert (tmp_path / newest / "screen.mp4").exists()
    assert (tmp_path / newest / "video.mp4").exists()
    assert store.get_session(newest) is not None
    assert not (tmp_path / middle / "screen.mp4").exists()
    assert (tmp_path / middle / "video.mp4").exists()
    assert store.get_session(middle) is not None
    assert not (tmp_path / oldest).exists()
    assert store.get_session(oldest) is None
    assert any(item["id"] == backlog["id"] for item in store.list_backlog())

    no_prune = prune_storage(max_clips=0, keep_raw_clips=1)
    assert no_prune["removed_sessions"] == []
    assert no_prune["removed_raw"] == []
    assert (tmp_path / newest / "screen.mp4").exists()
