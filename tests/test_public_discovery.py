from reel_studio.server import (
    build_llms_txt,
    build_sitemap,
    clean_display_title,
    page_shell,
    sanitize_public_url,
    watch_page,
)


def test_page_shell_contains_discovery_metadata():
    page = page_shell(
        "Test page",
        "<p>content</p>",
        "A useful description.",
        "/test",
        "https://example.test",
    )

    assert 'property="og:image"' in page
    assert 'name="twitter:card"' in page
    assert 'rel="canonical" href="https://example.test/test"' in page
    assert 'rel="icon" href="/favicon.svg"' in page


def test_llms_and_sitemap_builders():
    llms = build_llms_txt("https://example.test")
    assert "https://example.test/mcp" in llms
    assert "Authorization: Bearer <REEL_API_TOKEN>" in llms
    assert "scroll_to_text" in llms
    assert "update_step_narration" in llms

    sitemap = build_sitemap("https://example.test")
    for path in ("/", "/theater", "/backlog", "/bug_report", "/docs"):
        assert f"https://example.test{path}" in sitemap


def test_clean_display_title_sanitizes_urls_and_tokens():
    assert clean_display_title("https://app.example.com/admin/contacts?auth=secret123") == "Contacts"
    assert clean_display_title("https://saas.io/deals-pipeline") == "Deals Pipeline"
    assert clean_display_title("My App https://example.com/token=xyz") == "My App https://example.com/token=[REDACTED]"
    assert clean_display_title("") == "Untitled Demo"


def test_sanitize_public_url_strips_query_and_fragment():
    assert (
        sanitize_public_url(
            "https://app.example.com/admin#access_token=eyJhbGciOi123&refresh_token=abc"
        )
        == "https://app.example.com/admin"
    )
    assert (
        sanitize_public_url("https://app.example.com/login?next=/admin&token=xyz")
        == "https://app.example.com/login"
    )
    assert sanitize_public_url("not-a-url") == ""
    assert sanitize_public_url("") == ""
    assert sanitize_public_url(None) == ""


def test_watch_page_renders_theater_and_steps(tmp_path, monkeypatch):
    from reel_studio import store
    monkeypatch.setenv("REEL_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("REEL_DB_PATH", str(tmp_path / "test.db"))
    session_id = "a1b2c3d4"
    store.create_session(
        session_id, "https://crm.test/leads", "en-US-JennyNeural", 1920, 1080,
        str(tmp_path / session_id), title="Leads Overview Demo"
    )
    store.append_step(session_id, "click_and_wait", "button:create-lead", "https://crm.test/leads", "Leads", "Let's create a lead.", 2.5, 0.0, None, True, None)
    store.finish_session(session_id, str(tmp_path / session_id / "video.mp4"), None, 5.0)

    html = watch_page(session_id, "https://example.test")
    assert html is not None
    assert "Leads Overview Demo" in html
    assert "Let&#x27;s create a lead." in html or "Let's create a lead." in html
    assert f"/videos/{session_id}/video.mp4" in html
    assert "/theater" in html
