from __future__ import annotations

import os
from pathlib import Path

import pytest
from starlette.testclient import TestClient


TOKEN = "test-admin-token"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("reel-delete")
    os.environ["REEL_OUTPUT_DIR"] = str(tmp_path)
    os.environ["REEL_DB_PATH"] = str(tmp_path / "test.db")
    from reel_studio import server, store

    store.init_schema()
    app = server.BearerAuthMiddleware(server.mcp.streamable_http_app(), TOKEN)
    with TestClient(app) as test_client:
        yield test_client, store, server


@pytest.fixture()
def base_dir(request):
    from reel_studio import store  # noqa: F401  (ensures env is applied first)

    return os.environ["REEL_OUTPUT_DIR"]


def make_finished(store, base_dir, session_id="a1b2c3d4"):
    base = Path(base_dir)
    store.create_session(
        session_id, "https://crm.test/leads", "en-US-JennyNeural", 1920, 1080,
        str(base / session_id), title="Leads Overview Demo",
    )
    video = base / session_id
    video.mkdir(parents=True, exist_ok=True)
    (video / "video.mp4").write_bytes(b"fake")
    store.finish_session(session_id, str(video / "video.mp4"), None, 5.0)
    return session_id


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_delete_requires_auth(client):
    http, store, server = client
    response = http.delete("/api/videos/deadbeef?confirm=true")
    assert response.status_code == 401


def test_delete_requires_confirmation(client):
    http, store, server = client
    tmp_path = os.environ["REEL_OUTPUT_DIR"]
    session_id = make_finished(store, tmp_path)
    response = http.delete(f"/api/videos/{session_id}", headers=auth())
    assert response.status_code == 400
    assert response.json()["reason"] == "confirmation_required"
    assert store.get_session(session_id) is not None


def test_delete_removes_media_and_metadata(client):
    http, store, server = client
    tmp_path = os.environ["REEL_OUTPUT_DIR"]
    session_id = make_finished(store, tmp_path, "beef0001")
    response = http.delete(
        f"/api/videos/{session_id}?confirm=true", headers=auth()
    )
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert store.get_session(session_id) is None
    assert not (Path(tmp_path) / session_id).exists()


def test_delete_unknown_session_is_404(client):
    http, store, server = client
    response = http.delete(
        "/api/videos/deadbeef?confirm=true", headers=auth()
    )
    assert response.status_code == 404


def test_theater_page_contains_manage_controls(client):
    http, store, server = client
    tmp_path = os.environ["REEL_OUTPUT_DIR"]
    session_id = make_finished(store, tmp_path, "cafe1234")
    page = http.get("/theater").text
    assert "manage-toggle" in page
    assert f'data-delete-id="{session_id}"' in page


def test_watch_page_contains_delete_button(client):
    http, store, server = client
    tmp_path = os.environ["REEL_OUTPUT_DIR"]
    session_id = make_finished(store, tmp_path, "cafe5678")
    page = http.get(f"/watch/{session_id}").text
    assert f'data-delete-id="{session_id}"' in page


def test_mcp_delete_tool_still_shares_helper(client):
    _, store, server = client
    tmp_path = os.environ["REEL_OUTPUT_DIR"]
    session_id = make_finished(store, tmp_path, "cafe9012")
    import asyncio

    result = asyncio.run(server.perform_session_delete(session_id, False))
    assert result["deleted"] is True
    assert store.get_session(session_id) is None


def test_llms_txt_documents_http_delete():
    from reel_studio.server import build_llms_txt

    text = build_llms_txt("https://example.test")
    assert "DELETE /api/videos/{id}?confirm=true" in text
