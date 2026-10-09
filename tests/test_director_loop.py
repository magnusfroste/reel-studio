"""What an agent directing a recording sees: the action contract, hints that
say how to recover, sessions that stop when abandoned, and a finish that
refuses to render nothing. From agent feedback on a 57-minute recording,
2026-10-07. No browser needed: sessions are stand-ins."""
from __future__ import annotations

import asyncio
import json
import os

import pytest


@pytest.fixture(scope="module")
def mods(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("director")
    os.environ["REEL_OUTPUT_DIR"] = str(tmp)
    os.environ["REEL_DB_PATH"] = str(tmp / "director.db")
    from reel_studio import server, store
    from reel_studio.schema import ACTION_TYPES

    store.init_schema()
    return server, store, ACTION_TYPES


class FakeSession:
    """Just enough of BrowserSession for the tool layer."""

    def __init__(self, act_result=None, refs=None):
        self.voice = "en-US-JennyNeural"
        self.refs = refs or {}
        self.refs_stale = False
        self.last_activity = 0.0
        self.t0 = 0.0
        self.aborted = False
        self._act_result = act_result or {"ok": True}

    def touch(self):
        import time
        self.last_activity = time.monotonic()

    async def error_result(self, error_type, message):
        return {"ok": False, "error": {"type": error_type, "message": message}}, None

    async def act(self, action, narration=""):
        return dict(self._act_result), None

    async def abort(self):
        self.aborted = True


def _new_session(store, session_id):
    store.create_session(session_id, "https://example.com", "en-US-JennyNeural", 1920, 1080,
                         "/tmp", "edge", None, None, "", "", "#1f2a44", "", "Learn more", "none")


def _tools(server):
    return {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}


def test_act_tells_clients_every_verb_without_refs(mods):
    server, _, action_types = mods
    schema = _tools(server)["act"].inputSchema
    action = schema["properties"]["action"]
    assert action["properties"]["type"]["enum"] == list(action_types)
    assert action["required"] == ["type"]
    assert "$ref" not in json.dumps(schema), "some model providers reject $ref in tool schemas"


def test_act_batch_steps_carry_the_same_contract(mods):
    server, _, action_types = mods
    items = _tools(server)["act_batch"].inputSchema["properties"]["steps"]["items"]
    assert items["properties"]["action"]["properties"]["type"]["enum"] == list(action_types)
    assert "narration" in items["properties"]


def test_observe_offers_a_light_mode(mods):
    server, _, _ = mods
    detail = _tools(server)["observe"].inputSchema["properties"]["detail"]
    assert detail["enum"] == ["full", "refs"]
    assert detail.get("default") == "full"


def test_the_director_prompt_teaches_the_loop(mods):
    server, _, _ = mods
    names = [prompt.name for prompt in asyncio.run(server.mcp.list_prompts())]
    assert "director" in names
    text = server.director()
    for must in ("act_batch", 'detail="refs"', "verify_shot", "finish", "type: ref, text"):
        assert must in text


def test_an_invalid_action_says_what_a_valid_one_is(mods):
    server, store, _ = mods
    _new_session(store, "s-invalid")
    server.sessions["s-invalid"] = FakeSession()
    payload, _ = asyncio.run(server._run_action("s-invalid", {"type": "fill", "ref": "input:x"}, ""))
    assert payload["error"]["type"] == "invalid_action"
    assert "type: ref, text (the verb is type, not fill)" in payload["hint"]


def test_an_unknown_ref_lists_the_refs_there_are(mods):
    server, store, _ = mods
    _new_session(store, "s-ref")
    server.sessions["s-ref"] = FakeSession(
        act_result={"ok": False, "error": {"type": "unknown_ref", "message": "Unknown element ref: x"}},
        refs={"button:save": "#save", "input:email": "#email"},
    )
    payload, _ = asyncio.run(server._run_action("s-ref", {"type": "click", "ref": "x"}, ""))
    assert "observe" in payload["hint"]
    assert "button:save" in payload["hint"] and "input:email" in payload["hint"]


def test_a_visible_step_without_narration_is_flagged_and_a_wait_is_not(mods):
    server, store, _ = mods
    _new_session(store, "s-silent")
    server.sessions["s-silent"] = FakeSession(refs={"button:go": "#go"})
    clicked, _ = asyncio.run(server._run_action("s-silent", {"type": "click", "ref": "button:go"}, ""))
    assert "silent" in clicked["warning"]
    waited, _ = asyncio.run(server._run_action("s-silent", {"type": "wait", "ms": 200}, ""))
    assert "warning" not in waited
    narrated, _ = asyncio.run(server._run_action("s-silent", {"type": "click", "ref": "button:go"}, "Open the deal."))
    assert "warning" not in narrated


def test_finish_refuses_a_session_with_nothing_recorded(mods):
    server, store, _ = mods
    _new_session(store, "s-empty")
    server.sessions["s-empty"] = FakeSession()
    result = asyncio.run(server.finish("s-empty"))
    assert result["error"]["type"] == "empty_session"
    assert "delete_session" in result["hint"]
    assert "s-empty" in server.sessions, "still recording: it can be used, or discarded on purpose"


def test_an_unknown_session_names_the_live_ones(mods):
    server, _, _ = mods
    server.sessions["s-live"] = FakeSession()
    result = asyncio.run(server.act("nope", {"type": "wait", "ms": 1}))
    payload = json.loads(result.content[0].text)
    assert payload["error"]["type"] == "unknown_session"
    assert "s-live" in payload["active_sessions"]


def test_idle_sessions_are_chosen_by_last_activity(mods):
    server, _, _ = mods
    a, b = FakeSession(), FakeSession()
    a.last_activity, b.last_activity = 1000.0, 1890.0
    assert server.idle_session_ids({"a": a, "b": b}, now=2000.0, timeout=900) == ["a"]
    assert server.idle_session_ids({"a": a}, now=99999.0, timeout=0) == [], "0 disables"


def test_an_abandoned_session_stops_recording_and_is_marked(mods, monkeypatch):
    server, store, _ = mods
    monkeypatch.setenv("REEL_IDLE_TIMEOUT_SECONDS", "60")
    _new_session(store, "s-abandoned")
    session = FakeSession()
    session.last_activity = 0.0
    server.sessions["s-abandoned"] = session
    reaped = asyncio.run(server.reap_idle_sessions(now=10_000.0))
    assert "s-abandoned" in reaped
    assert session.aborted
    assert "s-abandoned" not in server.sessions
    assert store.get_session("s-abandoned")["status"] == "error"


def test_review_flags_a_mostly_silent_take(mods):
    server, store, _ = mods
    _new_session(store, "s-quiet")
    for i in range(5):
        store.append_step("s-quiet", "click", f"button:{i}", "https://example.com", "t",
                          "Narrated." if i == 0 else "", 0, float(i), None, True, None, "en-US-JennyNeural")
    review = asyncio.run(server.review_session("s-quiet"))
    coverage = [f for f in review["findings"] if f["category"] == "narration_coverage"]
    # The click right after the narrated one belongs to its line; the other
    # three of the remaining four are silent.
    assert coverage and "3 of 4" in coverage[0]["message"]


def test_finish_refuses_a_take_where_every_step_failed(mods):
    server, store, _ = mods
    _new_session(store, "s-failed")
    server.sessions["s-failed"] = FakeSession()
    store.append_step("s-failed", "fill", "x", "https://example.com", "t", "", 0, 0.0, None, False,
                      "invalid_action", "en-US-JennyNeural")
    result = asyncio.run(server.finish("s-failed"))
    assert result["error"]["type"] == "empty_session"
    assert "None of the 1 recorded steps succeeded" in result["error"]["message"]
    assert "s-failed" in server.sessions


def test_one_successful_step_is_enough_to_render(mods):
    server, store, _ = mods
    _new_session(store, "s-one-ok")
    server.sessions["s-one-ok"] = FakeSession()
    store.append_step("s-one-ok", "wait", None, "https://example.com", "t", "Hello.", 0, 0.0, None, True,
                      None, "en-US-JennyNeural")
    result = asyncio.run(server.finish("s-one-ok"))
    # A stand-in session cannot render; what matters is that it got past the check.
    assert result.get("error", {}).get("type") != "empty_session"


# ---- Round two: from the agent's second recording, 2026-10-08 -------------

def test_mask_is_a_verb_and_framing_lists_its_values(mods):
    server, _, _ = mods
    tools = _tools(server)
    verbs = tools["act"].inputSchema["properties"]["action"]["properties"]["type"]["enum"]
    assert "mask" in verbs and "unmask" in verbs
    assert tools["begin_shot"].inputSchema["properties"]["framing"]["enum"] == ["wide", "medium", "close"]
    assert "mask" in tools["start_session"].inputSchema["properties"]


def test_a_mask_selector_becomes_one_rule_and_braces_are_refused():
    from reel_studio.schema import mask_stylesheet
    css = mask_stylesheet(["input[type=password]", "  ", "[data-secret]"])
    assert css.count("blur(9px)") == 2 and "[data-secret] {" in css
    with pytest.raises(ValueError):
        mask_stylesheet(["a } body { display: none"])


def test_start_session_refuses_a_bad_mask_before_any_browser_starts(mods):
    server, _, _ = mods
    result = asyncio.run(server.start_session("https://example.com", mask=["a{}"]))
    assert result["error"]["type"] == "invalid_mask"


def test_a_described_framing_is_refused_with_the_three_words(mods):
    server, store, _ = mods
    _new_session(store, "s-framing")
    result = asyncio.run(server.begin_shot("s-framing", "opener", "Show the shop", "full page, top of the shop"))
    assert result["error"]["type"] == "invalid_framing"
    assert "wide, medium, close" in result["hint"]
    ok = asyncio.run(server.begin_shot("s-framing", "opener", "Show the shop", " Wide "))
    assert ok["ok"]


def test_editing_narration_before_finish_says_the_order_that_works(mods):
    server, store, _ = mods
    _new_session(store, "s-early-edit")
    result = asyncio.run(server.update_step_narration("s-early-edit", 0, "Better line."))
    assert result["error"]["type"] == "session_not_finished"
    assert "finish first" in result["hint"].lower() and "rerender" in result["hint"]


def test_the_director_prompt_covers_masks_shared_names_and_captions(mods):
    server, _, _ = mods
    text = server.director()
    for must in ("mask=", "same_name", "submits_form", "caption", "medium pushes in", "update_step_narration"):
        assert must in text


def test_the_screen_is_one_pixel_larger_than_the_recording_and_the_browser_fullscreen():
    # Measured: a fullscreen window under Xvfb comes out one pixel short each
    # way, so a screen of W+1 x H+1 gives a page of exactly W x H, recorded from
    # the top-left. --kiosk did nothing in a new context and left the tab bar.
    from reel_studio.engine import screen_geometry
    g = screen_geometry(1920, 1080)
    assert g["screen"] == "1921x1081x24"
    assert "--start-fullscreen" in g["browser_args"] and "--window-size=1921,1081" in g["browser_args"]
    assert not any("kiosk" in arg for arg in g["browser_args"])


def test_a_session_profile_never_offers_to_save_a_password(tmp_path):
    # Chrome's own popups sit on top of the recording where no mask reaches.
    # After a sign-in, "Save password?" stayed in the corner of a whole video,
    # showing the account's email (2026-10-08).
    import json as _json
    from reel_studio.engine import screen_geometry, write_quiet_profile
    write_quiet_profile(tmp_path)
    prefs = _json.loads((tmp_path / "Default" / "Preferences").read_text())
    assert prefs["credentials_enable_service"] is False
    assert prefs["profile"]["password_manager_enabled"] is False
    assert prefs["translate"]["enabled"] is False
    assert "--no-first-run" in screen_geometry(1920, 1080)["browser_args"]


class FocusSession(FakeSession):
    """A live session whose page never shows the focus text."""

    camera = None

    async def assert_visible(self, text):
        return {"visible": False, "box": None, "in_viewport": False}

    async def set_shot(self, framing, zoom=None, focus_ref=None, focus_text=None):
        return {"camera_zoom": 1.0}


def test_an_earlier_shot_verifies_after_the_page_has_moved_on(mods):
    server, store, _ = mods
    _new_session(store, "s-verify")
    server.sessions["s-verify"] = FocusSession()
    try:
        asyncio.run(server.begin_shot("s-verify", "card", "The template card", "medium",
                                      focus_text="Hand the camera to an agent"))
        asyncio.run(server.begin_shot("s-verify", "close", "Back on the dashboard", "wide"))
        # The card's page is gone; verifying it no longer needs it on screen.
        result = asyncio.run(server.verify_shot("s-verify", "card", True))
        assert result["shot"]["status"] == "verified"
        # The current shot is still checked against the live page.
        asyncio.run(server.begin_shot("s-verify", "now", "A detail", "close", focus_text="Missing"))
        result = asyncio.run(server.verify_shot("s-verify", "now", True))
        assert result["shot"]["status"] == "needs_review"
    finally:
        server.sessions.pop("s-verify", None)


def test_a_shot_can_be_declared_again(mods):
    server, store, _ = mods
    _new_session(store, "s-redeclare")
    asyncio.run(server.begin_shot("s-redeclare", "a", "First try", "close", focus_text="x"))
    store.verify_shot("s-redeclare", "a", False, "missed")
    again = asyncio.run(server.begin_shot("s-redeclare", "a", "Second try", "medium", focus_text="y"))
    assert again["ok"] and again["shot"]["status"] == "planned"
    assert again["shot"]["framing"] == "medium" and again["shot"]["focus_text"] == "y"
    assert len(store.get_session("s-redeclare")["shots"]) == 1


def test_finish_says_which_shot_blocks_it_and_why(mods):
    server, store, _ = mods
    _new_session(store, "s-blocked")
    store.append_step("s-blocked", "click", "button", "https://example.com", "Shop",
                      "", 0.0, 1.0, None, True, None)
    asyncio.run(server.begin_shot("s-blocked", "card", "The card", "medium"))
    store.verify_shot("s-blocked", "card", False, "Focus text not visible: Reel-studio")
    server.sessions["s-blocked"] = FakeSession()
    try:
        result = asyncio.run(server.finish("s-blocked"))
    finally:
        server.sessions.pop("s-blocked", None)
    assert result["error"]["type"] == "shot_review_required"
    assert "card is needs_review (Focus text not visible: Reel-studio)" in result["error"]["message"]
    assert "same shot_id" in result["hint"]


def test_the_wait_for_a_slow_answer_is_configurable():
    from pydantic import ValidationError
    from reel_studio.schema import Action, action_json_schema

    assert Action(type="click", ref="r").wait_timeout_ms == 8000
    assert Action(type="click", ref="r", wait_timeout_ms=45000).wait_timeout_ms == 45000
    with pytest.raises(ValidationError):
        Action(type="click", ref="r", wait_timeout_ms=600000)
    assert "wait_timeout_ms" in action_json_schema()["properties"]


def test_a_quiet_step_is_not_flagged_as_silent(mods):
    server, store, _ = mods
    _new_session(store, "s-quiet-step")
    store.append_step("s-quiet-step", "caption", None, "https://example.com", "Shop",
                      "Signing in.", 1.5, 0.0, None, True, None)
    store.append_step("s-quiet-step", "click", "button:sign-in", "https://example.com", "Shop",
                      "", 0.0, 2.0, None, True, None, quiet=True)
    store.append_step("s-quiet-step", "click", "button:other", "https://example.com", "Shop",
                      "", 0.0, 3.0, None, True, None)
    review = asyncio.run(server.review_session("s-quiet-step"))
    flagged = [f["location"] for f in review["findings"] if f["category"] == "focus_narration_alignment"]
    assert flagged == ["step[2]"]
    assert store.get_session("s-quiet-step")["steps"][1]["quiet"] == 1


def test_quiet_silences_the_warning_and_sticky_is_a_caption_option():
    from reel_studio.schema import ACTION_CONTRACT, Action, action_json_schema

    assert Action(type="caption", text="x", sticky=True).sticky
    assert Action(type="click", ref="r", quiet=True).quiet
    props = action_json_schema()["properties"]
    assert "sticky" in props and "quiet" in props
    assert "sticky" in ACTION_CONTRACT and "quiet" in ACTION_CONTRACT


def test_start_session_offers_transitions_and_there_is_a_voice_list(mods):
    server, _, _ = mods
    tools = _tools(server)
    props = tools["start_session"].inputSchema["properties"]
    assert props["transitions"]["enum"] == ["smooth", "cuts"]
    assert "list_voices" in tools
    assert server._render_config("", "", "#000000", "", "", "none", "CUTS").transitions == "cuts"
    assert server._render_config("", "", "#000000", "", "", "none", "fancy").transitions == "smooth"
    assert "list_voices" in server.director()


def test_act_batch_takes_flags_written_beside_the_action(mods):
    server, store, _ = mods
    _new_session(store, "s-flags")
    server.sessions["s-flags"] = FakeSession(act_result={"ok": True, "offset_seconds": 1.0})
    try:
        asyncio.run(server.act_batch("s-flags", [
            {"action": {"type": "click", "ref": "r"}, "quiet": True},
            {"action": {"type": "goto", "url": "https://example.com"}, "offscreen": True},
        ]))
    finally:
        server.sessions.pop("s-flags", None)
    steps = store.get_session("s-flags")["steps"]
    assert steps[0]["quiet"] == 1 and steps[1]["offscreen"] == 1


def test_a_click_after_a_narrated_caption_is_not_called_silent(mods):
    server, store, _ = mods
    _new_session(store, "s-coverage")
    for i in range(5):
        store.append_step("s-coverage", "caption", None, "https://example.com", "t",
                          f"Line {i}.", 2.0, i * 10.0, None, True, None)
        store.append_step("s-coverage", "click", f"button:{i}", "https://example.com", "t",
                          "", 0.0, i * 10.0 + 5, None, True, None)
    review = asyncio.run(server.review_session("s-coverage"))
    assert "narration_coverage" not in [f["category"] for f in review["findings"]]


def test_offscreen_is_part_of_the_contract():
    from reel_studio.schema import ACTION_CONTRACT, Action, action_json_schema

    assert Action(type="type", ref="r", text="x", offscreen=True).offscreen
    assert "offscreen" in action_json_schema()["properties"] and "offscreen" in ACTION_CONTRACT


def test_title_background_is_validated(mods):
    server, _, _ = mods
    assert server._title_background("SOLID") == "solid"
    assert server._title_background("https://example.com/hero.png") == "https://example.com/hero.png"
    assert server._title_background("http://10.0.0.1/x.png") == "auto"
    assert server._title_background("") == "auto"
    props = _tools(server)["start_session"].inputSchema["properties"]
    assert "title_background" in props


@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="ffmpeg is required")
def test_a_caption_hold_survives_a_rerender(mods, monkeypatch, tmp_path):
    server, store, _ = mods
    import subprocess
    from types import SimpleNamespace

    session_dir = tmp_path / "s-hold"
    session_dir.mkdir()
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=black:s=64x36:r=25:d=6", "-pix_fmt", "yuv420p",
                    str(session_dir / "screen.mp4")], check=True)
    (session_dir / "video.mp4").write_bytes((session_dir / "screen.mp4").read_bytes())
    store.create_session("s-hold", "https://example.com", "en-US-JennyNeural", 64, 36,
                         str(session_dir), "edge", None, None, "", "", "#1f2a44", "", "Learn more", "none")
    store.append_step("s-hold", "caption", None, "https://example.com", "t", "", 0.0, 1.0,
                      None, True, None, annotation_seconds=3.5)
    store.append_step("s-hold", "click", "button:x", "https://example.com", "t", "", 0.0, 5.0,
                      None, True, None)
    store.finish_session("s-hold", str(session_dir / "video.mp4"), None, 5.0)
    assert store.get_session("s-hold")["steps"][0]["annotation_seconds"] == 3.5

    captured = {}

    def fake_render(source, steps, video_path, *args, **kwargs):
        captured["steps"] = steps
        return SimpleNamespace(warnings=[], duration=4.9)

    monkeypatch.setattr(server, "segmented_render", fake_render)
    result = asyncio.run(server.rerender("s-hold"))
    assert result["ok"]
    assert [round(hold, 2) for _, _, hold in captured["steps"]] == [3.5, 0.0]
