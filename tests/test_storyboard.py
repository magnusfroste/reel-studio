import asyncio
from reel_studio import store
from reel_studio.server import pending_shots, review_session


def test_storyboard_shot_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("REEL_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("REEL_DB_PATH", str(tmp_path / "story.db"))
    session_id = "f" * 32
    store.create_session(session_id, "https://example.com", "voice", 1280, 720, str(tmp_path / session_id))

    shot = store.begin_shot(
        session_id,
        "lead-focus",
        "Explain why this lead is not qualified",
        "close",
        1.2,
        "button:create-deal",
        "Create deal",
    )
    assert shot["shot_id"] == "lead-focus"
    assert shot["status"] == "planned"

    verified = store.verify_shot(session_id, "lead-focus", True, "Target visible and readable")
    assert verified["status"] == "verified"
    assert verified["verification_note"] == "Target visible and readable"
    assert store.get_session(session_id)["shots"][0]["shot_id"] == "lead-focus"
    assert pending_shots(store.get_session(session_id)) == []


def test_pending_shots_identifies_unverified_work(tmp_path, monkeypatch):
    monkeypatch.setenv("REEL_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("REEL_DB_PATH", str(tmp_path / "story.db"))
    session_id = "e" * 32
    store.create_session(session_id, "https://example.com", "voice", 1280, 720, str(tmp_path / session_id))
    store.begin_shot(session_id, "opening", "Establish context", "wide")
    assert [shot["shot_id"] for shot in pending_shots(store.get_session(session_id))] == ["opening"]


def test_review_session_scans_tokens_and_quality(tmp_path, monkeypatch):
    monkeypatch.setenv("REEL_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("REEL_DB_PATH", str(tmp_path / "review.db"))
    session_id = "9" * 32
    store.create_session(
        session_id,
        "https://app.test/login?access_token=secret123",
        "voice",
        1280,
        720,
        str(tmp_path / session_id),
    )
    store.append_step(
        session_id,
        "click",
        "button:next",
        "https://app.test/dashboard",
        "Dashboard",
        "Welcome to the overview.",
        3.0,
        0.0,
        None,
        True,
        None,
    )
    store.append_step(
        session_id,
        "annotate",
        "card:metrics",
        "https://app.test/dashboard",
        "Dashboard",
        "",
        0.0,
        3.0,
        None,
        True,
        None,
    )

    review = asyncio.run(review_session(session_id))
    assert review["ok"] is True
    assert review["step_count"] == 2
    assert any(f["category"] == "security_secret_leak" for f in review["findings"])
    assert any(f["category"] == "focus_narration_alignment" for f in review["findings"])
