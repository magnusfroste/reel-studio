from reel_studio import store


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
