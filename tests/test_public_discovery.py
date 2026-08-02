from reel_studio.server import (
    build_llms_txt,
    build_sitemap,
    page_shell,
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
