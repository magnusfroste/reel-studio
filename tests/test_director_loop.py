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
    assert coverage and "4 of 5" in coverage[0]["message"]


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
