"""FastMCP entry point for Reel Studio."""

import asyncio
import time
from typing import Annotated, Any, Literal
import hmac
import html
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.parse
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from pydantic import Field, ValidationError
import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.types import ASGIApp

from .engine import BrowserSession, output_root
from . import store
from . import retention
from .render import (
    FONT_PATH,
    RenderConfig,
    probe_duration,
    rerender_narration,
    segmented_render,
    segmented_render_enabled,
    QUIET_FLOOR,
    SEGMENT_FLOOR,
)
from .camera import MAX_ZOOM, Camera
from .schema import ACTION_CONTRACT, ACTION_TYPES, FRAMINGS, Action, action_json_schema, mask_stylesheet
from .tts import (
    TTSProviderError,
    elevenlabs_configured,
    list_elevenlabs_voices,
    normalize_provider,
    synthesize,
    validate_provider,
)


LOCAL_ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
LOCAL_ALLOWED_ORIGINS = [
    "http://127.0.0.1",
    "http://127.0.0.1:*",
    "http://localhost",
    "http://localhost:*",
    "http://[::1]",
    "http://[::1]:*",
]


def transport_security_from_env() -> TransportSecuritySettings:
    """Build DNS-rebinding protection settings for local and public hosts."""
    allowed_hosts = list(LOCAL_ALLOWED_HOSTS)
    allowed_origins = list(LOCAL_ALLOWED_ORIGINS)
    configured = False

    public_base_url = os.environ.get("REEL_PUBLIC_BASE_URL", "").strip()
    if public_base_url:
        parsed = urlparse(public_base_url)
        if parsed.scheme and parsed.netloc and parsed.hostname:
            configured = True
            hostname = parsed.hostname
            host_pattern = f"[{hostname}]:*" if ":" in hostname else f"{hostname}:*"
            allowed_hosts.extend([parsed.netloc, host_pattern])
            origin = f"{parsed.scheme}://{parsed.netloc}"
            origin_pattern = f"{parsed.scheme}://{host_pattern.removesuffix(':*')}:*"
            allowed_origins.extend([origin, origin_pattern])

    for value in os.environ.get("REEL_ALLOWED_HOSTS", "").split(","):
        hostname = value.strip()
        if not hostname:
            continue
        configured = True
        allowed_hosts.extend([hostname, f"{hostname}:*"])

    if not configured:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(dict.fromkeys(allowed_hosts)),
        allowed_origins=list(dict.fromkeys(allowed_origins)),
    )


mcp = FastMCP("reel-studio", transport_security=transport_security_from_env())
sessions: dict[str, BrowserSession] = {}


def feedback_result(payload: dict, screenshot: object = None) -> CallToolResult:
    """Return structured JSON plus an MCP image when one is available."""
    content: list[object] = [
        TextContent(type="text", text=json.dumps(payload)),
    ]
    if screenshot:
        content.append(Image(path=screenshot).to_image_content())
    return CallToolResult(content=content, structuredContent=payload)


PAGE_STYLES = """
    :root { color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
    * { box-sizing: border-box; }
    body { margin: 0; background: #0d111a; color: #edf1f7; }
    main { max-width: 1020px; margin: 0 auto; padding: 34px 24px 88px; }
    nav { display: flex; justify-content: space-between; align-items: center; gap: 20px; }
    nav strong { color: #fff; letter-spacing: -.02em; }
    nav a, a { color: #9eb1ff; text-decoration: none; }
    nav a:hover, a:hover { text-decoration: underline; }
    .eyebrow { color: #8ea7ff; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }
    .hero { max-width: 820px; padding: 112px 0 72px; }
    h1 { font-size: clamp(2.9rem, 8vw, 6.5rem); letter-spacing: -.06em; line-height: .94; margin: 16px 0 28px; }
    h2 { color: #d4dcff; font-size: 1.8rem; margin: 54px 0 18px; }
    h3 { color: #fff; margin: 0 0 10px; }
    p, li { color: #b9c1d2; font-size: 1.05rem; line-height: 1.65; }
    .lede { font-size: 1.25rem; max-width: 680px; }
    .actions { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 30px; }
    .button { background: #8ea7ff; border-radius: 9px; color: #10131a; display: inline-block; font-weight: 750; padding: 12px 18px; }
    .button:hover { background: #b8c6ff; text-decoration: none; }
    .button.secondary { background: #1b2438; color: #dbe2ff; }
    .delete-button { background: #3a1220; border: 1px solid #8f3345; color: #ff9aa8; font-weight: 750; border-radius: 9px; display: inline-block; padding: 6px 12px; font-size: 0.85rem; cursor: pointer; }
    .delete-button:hover { background: #5a1c2e; }
    .video-card .delete-button { display: none; }
    body.managing .video-card .delete-button { display: inline-block; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 14px; }
    .card, .endpoint { background: #151b28; border: 1px solid #2a354d; border-radius: 14px; padding: 20px; }
    .card p { margin: 0; }
    .steps { counter-reset: step; list-style: none; padding: 0; }
    .steps li { counter-increment: step; display: flex; gap: 14px; margin: 15px 0; }
    .steps li::before { content: counter(step); background: #7188ff; border-radius: 50%; color: #10131a; flex: 0 0 28px; font-weight: 800; height: 28px; line-height: 28px; text-align: center; }
    code, pre { background: #1b2130; border: 1px solid #303a52; border-radius: 10px; }
    code { padding: 2px 6px; color: #d5ddff; }
    pre { overflow-x: auto; padding: 18px; color: #d5ddff; }
    .endpoint { border-left: 3px solid #7188ff; border-radius: 0 12px 12px 0; }
    .tool { margin: 18px 0; }
    .tool code { color: #fff; font-size: 1rem; }
    .muted { color: #8490a8; }
    .theater-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 18px; }
    .video-card { overflow: hidden; padding: 0; }
    .video-card video { display: block; width: 100%; background: #080b11; }
    .video-card-body { padding: 18px; }
    .video-card h3 { overflow-wrap: anywhere; }
    .placeholder { border: 1px dashed #405070; border-radius: 14px; padding: 28px; text-align: center; }
    .backlog-item { position: relative; }
    .badges { display: flex; flex-wrap: wrap; gap: 7px; margin: 12px 0; }
    .badge { background: #263453; border-radius: 999px; color: #dbe2ff; font-size: .8rem; padding: 4px 9px; }
    .status-badge { border: 1px solid transparent; }
    .status-open { background: #263453; }
    .status-planned { background: #3d315f; border-color: #8d70d8; }
    .status-in_progress { background: #5b431d; border-color: #d59a37; }
    .status-shipped { background: #1f4d3b; border-color: #4db986; }
    .status-wont_fix { background: #303744; border-color: #68758b; }
    .backlog-item-muted { opacity: .62; }
    .backlog-item-muted h3 { text-decoration: line-through; }
    .status-summary { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 28px; }
"""


FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
<rect width="32" height="32" rx="7" fill="#1f2a44"/>
<path fill="#edf1f7" d="M7 9h18v14H7zM9 7h5l-2 5H7zm8 0h5l-2 5h-5zm-5 15h8v2h-8z"/>
<circle cx="11" cy="16" r="2" fill="#1f2a44"/><circle cx="21" cy="16" r="2" fill="#1f2a44"/>
</svg>"""


def page_shell(
    title: str,
    content: str,
    description: str = "",
    canonical_path: str = "/",
    base_url: str = "",
    structured_data: str = "",
) -> str:
    """Wrap public page content in the shared landing/docs layout."""
    escaped_title = html.escape(title)
    escaped_description = html.escape(description, quote=True)
    canonical_url = html.escape(
        f"{base_url.rstrip('/')}{canonical_path}", quote=True
    )
    og_image = html.escape(f"{base_url.rstrip('/')}/og.png", quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title} · reel-studio</title>
  <meta name="description" content="{escaped_description}">
  <link rel="canonical" href="{canonical_url}">
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <link rel="alternate icon" href="/favicon.png" type="image/png">
  <meta property="og:type" content="website">
  <meta property="og:site_name" content="reel-studio">
  <meta property="og:title" content="{escaped_title} · reel-studio">
  <meta property="og:description" content="{escaped_description}">
  <meta property="og:url" content="{canonical_url}">
  <meta property="og:image" content="{og_image}">
  <meta property="og:image:width" content="1200">
  <meta property="og:image:height" content="630">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{escaped_title} · reel-studio">
  <meta name="twitter:description" content="{escaped_description}">
  <meta name="twitter:image" content="{og_image}">
  {structured_data}
  <style>{PAGE_STYLES}</style>
</head>
<body>
  <main>
    <nav><strong>reel-studio</strong><span><a href="/">Home</a> · <a href="/theater">Theater</a> · <a href="/backlog">Backlog</a> · <a href="/bug_report">Bug reports</a> · <a href="/docs">Docs</a></span></nav>
    {content}
  </main>
</body>
</html>"""


def mcp_endpoint() -> str:
    public_base_url = os.environ.get("REEL_PUBLIC_BASE_URL", "").rstrip("/")
    return f"{public_base_url}/mcp" if public_base_url else "/mcp"


def build_llms_txt(base_url: str) -> str:
    base = base_url.rstrip("/")
    return f"""# reel-studio

reel-studio turns any AI agent into a video director for narrated browser tutorials.
Bring your own agent: observe the UI, direct deliberate actions, narrate the story,
and finish a polished MP4.

## MCP

Endpoint: {base}/mcp
Authentication: `Authorization: Bearer <REEL_API_TOKEN>`

Core loop: `start_session` → `observe` → `act` with optional narration → `finish`.
Core tools include `start_session`, `observe`, `act`, `act_batch`, `finish`,
`get_status`, `get_session`, and `list_sessions`.
`act_batch(session_id, steps)` runs a whole beat of actions in one call; each
step is `{{"action": ..., "narration": ...}}` and execution stops on first error.
Finished sessions can be removed explicitly with `delete_session(session_id,
confirm=true)`; this deletes media and metadata for only that session. Add
`force=true` to also remove a stale active session orphaned by a restart.
The theater's Manage mode deletes over HTTP with
`DELETE /api/videos/{{id}}?confirm=true` and the same bearer token.

Optional `start_session` branding parameters: `title`, `subtitle`, `accent`,
`cta_url`, `cta_text`, and `music` (`none` or `subtle`).
Target text reliably with `scroll_to_text` and non-recording `assert_visible`.
Use `click_and_wait` (or `click` with `wait_for_url`, `wait_for_text`, and
`settle_ms`) so narration starts after the destination page is visibly ready.
The director can declare and verify shot intent with `begin_shot` and `verify_shot`.
Observed refs are semantic (for example `button:new-contact`) rather than
position-only indexes. Use `select_option` for native/custom dropdowns and
`press_key` for keyboard-driven controls.
Use `review_session` before finishing to scan for leaked secrets or focus defects.
Edit finished narration with `update_step_narration`, then use `rerender`.
Storage retention uses `REEL_MAX_CLIPS` and `REEL_KEEP_RAW_CLIPS`; call the
token-protected `prune` tool to clean old finished sessions manually.

## Public resources

- `/theater` — finished narrated videos
- `/backlog` and `/bug_report` — public roadmap and bug reports
- `/docs` — detailed API documentation
- `/api/videos`, `/api/backlog`, `/api/bug_reports` — JSON feeds

"""


def build_sitemap(base_url: str) -> str:
    base = base_url.rstrip("/")
    paths = ["/", "/theater", "/backlog", "/bug_report", "/docs"]
    urls = "\n".join(
        f"  <url><loc>{html.escape(base + path)}</loc></url>"
        for path in paths
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n"
        "</urlset>\n"
    )


def clean_display_title(title_or_url: str | None) -> str:
    """Return a safe, readable title stripped of tokens, passwords, and query blobs."""
    if not title_or_url or not str(title_or_url).strip():
        return "Untitled Demo"
    text = str(title_or_url).strip()
    if text.startswith(("http://", "https://")):
        try:
            parsed = urllib.parse.urlsplit(text)
            path = parsed.path.strip("/")
            if path:
                last_segment = path.split("/")[-1]
                slug = re.sub(r"[-_]+", " ", last_segment).strip()
                if slug:
                    return slug.title()
            netloc = parsed.netloc.split(":")[0]
            parts = [p for p in netloc.split(".") if p and p not in {"www", "com", "org", "net", "io", "app"}]
            if parts:
                return parts[-1].capitalize() + " Demo"
            return "Web Demo"
        except Exception:
            return "Product Demo"
    # Mask parameters or tokens if present
    cleaned = re.sub(r"(?:[?&/]|(?<=\s))(?:token|auth|key|secret|password|access_token)=[^&#\s]+", lambda m: m.group(0).split("=")[0] + "=[REDACTED]", text, flags=re.IGNORECASE)
    # Remove raw token strings
    cleaned = re.sub(r"\b(?:eyJ[a-zA-Z0-9_-]{10,}|[0-9a-fA-F]{32,64})\b", "[REDACTED]", cleaned)
    return cleaned.strip() or "Product Demo"


def sanitize_public_url(url: str | None) -> str:
    """Return a URL safe for public display: scheme, host, and path only.

    Query strings and fragments are dropped entirely so auth tokens or other
    secrets carried in URLs never leak through listings or metadata.
    """
    if not url or not str(url).strip():
        return ""
    try:
        parsed = urllib.parse.urlsplit(str(url).strip())
        if not parsed.scheme or not parsed.netloc:
            return ""
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, "", "")
        )
    except Exception:
        return ""


def build_robots(base_url: str) -> str:
    base = base_url.rstrip("/")
    return (
        "User-agent: *\n"
        "Allow: /\n"
        f"Sitemap: {base}/sitemap.xml\n"
        f"Agents: {base}/llms.txt\n"
    )


def _asset_cache_dir() -> Path:
    try:
        directory = output_root() / ".public-assets"
        directory.mkdir(parents=True, exist_ok=True)
        return directory
    except OSError:
        directory = Path(tempfile.gettempdir()) / "reel-studio-public-assets"
        directory.mkdir(parents=True, exist_ok=True)
        return directory


def _drawtext_escape(text: str) -> str:
    return (
        text.replace("\\", r"\\")
        .replace(":", r"\:")
        .replace("'", r"\'")
        .replace(",", r"\,")
        .replace("%", r"\%")
    )


def _generate_png_asset(path: Path, width: int, height: int) -> None:
    if not Path(FONT_PATH).is_file():
        raise RuntimeError(f"Asset font is missing: {FONT_PATH}")
    if width == 32:
        filters = (
            f"drawtext=fontfile={FONT_PATH}:text='R':fontcolor=white:"
            "fontsize=22:x=(w-text_w)/2:y=(h-text_h)/2"
        )
        source = f"color=c=#1f2a44:s={width}x{height}:r=1:d=1"
    else:
        filters = ",".join(
            [
                f"drawtext=fontfile={FONT_PATH}:text='Reel-studio':"
                "fontcolor=white:fontsize=92:x=80:y=170",
                f"drawtext=fontfile={FONT_PATH}:text='The ultimate tool for agentic directors':"
                "fontcolor=#b9c8ff:fontsize=38:x=84:y=300",
                f"drawtext=fontfile={FONT_PATH}:text='narrated browser tutorials, directed by AI agents':"
                "fontcolor=#9eb1ff:fontsize=24:x=86:y=535",
            ]
        )
        source = f"color=c=#1f2a44:s={width}x{height}:r=1:d=1"
    temporary = path.with_suffix(".tmp.png")
    subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source,
            "-vf", filters, "-frames:v", "1", str(temporary),
        ],
        check=True,
    )
    temporary.replace(path)


def favicon_png_path() -> Path:
    path = _asset_cache_dir() / "favicon.png"
    if not path.is_file():
        _generate_png_asset(path, 32, 32)
    return path


def og_image_path() -> Path:
    path = _asset_cache_dir() / "og.png"
    if not path.is_file():
        _generate_png_asset(path, 1200, 630)
    return path


def video_url(session_id: str) -> str:
    """Return the public relative URL for a finished session video."""
    return f"/videos/{session_id}/video.mp4"


def format_duration(duration: float | None) -> str:
    return f"{duration:.1f}s" if duration is not None else "Duration unavailable"


def video_card(session: dict) -> str:
    session_id = html.escape(session["id"], quote=True)
    raw_title = session.get("title") or session.get("start_url") or "Video Demo"
    display_title = clean_display_title(raw_title)
    title = html.escape(display_title)
    duration = html.escape(format_duration(session.get("duration_seconds")))
    finished_at = html.escape(session.get("finished_at") or "Recently finished")
    return f"""
    <article class="video-card card" data-session-id="{session_id}">
      <a href="/watch/{session_id}" style="display:block; text-decoration:none;">
        <video controls preload="metadata" src="{video_url(session_id)}"></video>
      </a>
      <div class="video-card-body">
        <h3><a href="/watch/{session_id}" style="color:inherit;">{title}</a></h3>
        <p class="muted">{duration} · {finished_at}</p>
        <div style="margin-top:10px; display:flex; gap:10px;">
          <a class="button secondary" style="font-size:0.85rem; padding:6px 12px;" href="/watch/{session_id}">Watch Theater</a>
          <a class="button secondary" style="font-size:0.85rem; padding:6px 12px;" href="{video_url(session_id)}" download>Download</a>
          <button type="button" class="button secondary" style="font-size:0.85rem; padding:6px 12px;" data-share-path="/watch/{session_id}" data-share-title="{title}">Share</button>
          <button type="button" class="delete-button" data-delete-id="{session_id}" data-delete-title="{title}">Delete</button>
        </div>
      </div>
    </article>"""


def video_refresh_script(container_id: str, featured: bool = False) -> str:
    mode = "true" if featured else "false"
    return f"""<script>
    (() => {{
      const container = document.getElementById("{container_id}");
      const featured = {mode};
      const card = (item) => {{
        const article = document.createElement("article");
        article.className = "video-card card";
        article.dataset.sessionId = item.id;
        const player = document.createElement("video");
        player.controls = true;
        player.preload = "metadata";
        player.src = `/videos/${{item.id}}/video.mp4`;
        const body = document.createElement("div");
        body.className = "video-card-body";
        const heading = document.createElement("h3");
        heading.textContent = item.title || item.start_url || "Video Demo";
        const meta = document.createElement("p");
        meta.className = "muted";
        meta.textContent = `${{item.duration_seconds == null ? "Duration unavailable" : item.duration_seconds.toFixed(1) + "s"}} · ${{item.finished_at || "Recently finished"}}`;
        const del = document.createElement("button");
        del.type = "button";
        del.className = "delete-button";
        del.dataset.deleteId = item.id;
        del.dataset.deleteTitle = item.title || item.start_url || "Video Demo";
        del.textContent = "Delete";
        body.append(heading, meta, del);
        article.append(player, body);
        return article;
      }};
      const refresh = async () => {{
        try {{
          const response = await fetch("/api/videos", {{cache: "no-store"}});
          if (!response.ok) return;
          const items = await response.json();
          if (featured) {{
            const latest = items[0];
            if (!latest || container.dataset.sessionId === latest.id) return;
            container.replaceChildren(card(latest));
            container.dataset.sessionId = latest.id;
            return;
          }}
          const existing = new Set([...container.children].map((item) => item.dataset.sessionId));
          items.slice().reverse().forEach((item) => {{
            if (!existing.has(item.id)) container.prepend(card(item));
          }});
          const empty = container.querySelector("[data-empty]");
          if (empty && items.length) empty.remove();
        }} catch (_) {{}}
      }};
      setInterval(refresh, 10000);
    }})();
    </script>"""


def video_admin_script() -> str:
    """Client helpers for token-authorized deletion from public pages.

    The delete buttons never expose the server token. The admin supplies it
    through a browser prompt; it is kept in sessionStorage so a page reload
    does not re-ask, and every DELETE request still passes the same bearer
    check that protects the MCP endpoint.
    """
    return """<script>
    (() => {
      // Share: the watch page's own address. On a phone the system share
      // sheet; elsewhere the link goes to the clipboard and the button says so.
      document.addEventListener("click", async (event) => {
        const button = event.target.closest("[data-share-path]");
        if (!button) return;
        const url = location.origin + button.dataset.sharePath;
        const touch = window.matchMedia && window.matchMedia("(pointer: coarse)").matches;
        if (touch && navigator.share) {
          try { await navigator.share({ title: button.dataset.shareTitle || document.title, url }); return; }
          catch (error) { if (error && error.name === "AbortError") return; }
        }
        try {
          await navigator.clipboard.writeText(url);
        } catch (error) {
          window.prompt("Copy this link:", url);
          return;
        }
        const label = button.dataset.label || button.textContent;
        button.dataset.label = label;
        button.textContent = "Link copied ✓";
        setTimeout(() => { button.textContent = label; }, 2000);
      });
    })();
    (() => {
      const TOKEN_KEY = "reel-studio-admin-token";
      const askToken = (retry) => {
        let token = sessionStorage.getItem(TOKEN_KEY) || "";
        if (!token || retry) {
          token = window.prompt(
            retry
              ? "That token was rejected. Enter the reel-studio admin token:"
              : "Enter the reel-studio admin token (the REEL_API_TOKEN):",
            token);
          if (token === null) return "";
          token = token.trim();
          sessionStorage.setItem(TOKEN_KEY, token);
        }
        return token;
      };
      const deleteVideo = async (id, title, onDone) => {
        if (!window.confirm(`Delete "${title}"? This permanently removes the video, its recording, and all metadata.`)) return;
        let token = askToken(false);
        if (!token) return;
        const send = () => fetch(`/api/videos/${id}?confirm=true`, {
          method: "DELETE",
          headers: { Authorization: `Bearer ${token}` },
        });
        let response = await send();
        if (response.status === 401) {
          sessionStorage.removeItem(TOKEN_KEY);
          token = askToken(true);
          if (!token) return;
          response = await send();
        }
        if (!response.ok) {
          window.alert("Delete failed with status " + response.status + ". The video is still listed.");
          return;
        }
        onDone();
      };
      document.addEventListener("click", (event) => {
        const button = event.target.closest("[data-delete-id]");
        if (!button) return;
        event.preventDefault();
        const article = button.closest(".video-card");
        deleteVideo(
          button.dataset.deleteId,
          button.dataset.deleteTitle || "this video",
          () => {
            if (article) {
              article.remove();
            } else if (window.location.pathname.startsWith("/watch/")) {
              window.location.replace("/theater");
            }
          },
        );
      });
      const toggle = document.getElementById("manage-toggle");
      if (toggle) {
        toggle.addEventListener("click", () => {
          const managing = document.body.classList.toggle("managing");
          toggle.textContent = managing ? "Done managing" : "Manage videos";
        });
      }
    })();
    </script>"""


def landing_page(base_url: str = "/") -> str:
    """Render the marketing landing page without exposing credentials."""
    endpoint = html.escape(mcp_endpoint())
    latest = store.list_finished_sessions()[:1]
    featured = latest[0] if latest else None
    featured_content = (
        f"""<div id="featured-video" data-session-id="{html.escape(featured["id"], quote=True)}">
          {video_card(featured)}
        </div>"""
        if featured
        else """<div id="featured-video" class="placeholder">
          <p>No videos yet. Your next finished storyboard will appear here.</p>
        </div>"""
    )
    content = f"""
    <section class="hero">
      <div class="eyebrow">Your AI product demo team</div>
      <h1>Give your product a demo it deserves.</h1>
      <p class="lede">reel-studio lets an AI agent log into your web app and
      autonomously produce cool, inspiring narrated demo and marketing videos.
      It is like having an employee who demos your product better than you can.</p>
      <div class="actions">
        <a class="button" href="/docs">Read the docs</a>
        <a class="button secondary" href="https://github.com/magnusfroste/reel-studio">View on GitHub</a>
      </div>
    </section>
    <h2>Turn complex flows into compelling stories</h2>
    <p>Products with dozens of modules and intricate workflows are painful to
    record manually. Let an agent explore the UI, follow a storyboard, and
    turn the moments that matter into a polished narrated walkthrough.</p>
    <ol class="steps">
      <li><span><strong>Observe</strong> The agent sees the current page, screenshot, and interactive refs.</span></li>
      <li><span><strong>Act</strong> It clicks, types, scrolls, hovers, or navigates one deliberate step at a time.</span></li>
      <li><span><strong>Narrate</strong> Each step can explain the product story in a natural voice.</span></li>
      <li><span><strong>Render</strong> reel-studio produces a downloadable MP4 with audio and screen capture.</span></li>
    </ol>
    <h2>Latest from the theater</h2>
    {featured_content}
    <p><a class="button secondary" href="/theater">See all videos →</a></p>
    {video_refresh_script("featured-video", featured=True)}
    <h2>Connect your agent</h2>
    <p class="endpoint">MCP endpoint: <code>{endpoint}</code></p>
    <!-- Example placeholder: Bearer <YOUR_TOKEN> -->
    <pre><code>claude mcp add --transport http reel-studio {endpoint} \
  --header "Authorization: Bearer &lt;YOUR_TOKEN&gt;"</code></pre>
    <p class="muted">Use the server's <code>REEL_API_TOKEN</code> as the
    placeholder. Never commit or share the real token.</p>
    """
    description = (
        "Turn any AI agent into a video director for narrated browser tutorials "
        "and polished product demos."
    )
    structured_data = (
        '<script type="application/ld+json">'
        + json.dumps({
            "@context": "https://schema.org",
            "@type": "SoftwareApplication",
            "name": "reel-studio",
            "applicationCategory": [
                "DeveloperApplication",
                "MultimediaApplication",
            ],
            "operatingSystem": "Any",
            "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
            "description": description,
            "url": base_url.rstrip("/") + "/",
        })
        + "</script>"
    )
    return page_shell(
        "Autonomous product demos", content, description, "/", base_url,
        structured_data,
    )


def watch_page(session_id: str, base_url: str = "/") -> str | None:
    """Render a dedicated theater and watch view for a single finished session."""
    session = store.get_session(session_id)
    if not session or session.get("status") != "finished":
        return None
    raw_title = session.get("title") or session.get("start_url") or "Video Demo"
    title = clean_display_title(raw_title)
    duration = format_duration(session.get("duration_seconds"))
    finished_at = session.get("finished_at") or "Recently finished"
    steps = session.get("steps", [])
    
    step_items = []
    for step in steps:
        idx = step.get("idx", 0) + 1
        narration = str(step.get("narration_text") or "")
        action_type = str(step.get("action_type") or "step")
        target = str(step.get("target") or "")
        try:
            offset_seconds = float(step.get("offset_seconds") or 0.0)
        except (TypeError, ValueError):
            offset_seconds = 0.0
        offset = f"{offset_seconds:.1f}s"
        # Outside the f-string: a backslash inside an f-string expression is a
        # SyntaxError before Python 3.12, and CI runs 3.10.
        narration_html = html.escape(narration) if narration else '<span class="muted">(No narration)</span>'
        step_items.append(
            f"""<li style="margin-bottom:12px; padding:10px; background:#1b2130; border-radius:8px;">
                <div style="display:flex; justify-content:space-between; margin-bottom:4px;">
                    <strong style="color:#8ea7ff;">Step {idx}: {html.escape(action_type)} {html.escape(target)}</strong>
                    <span class="muted">{offset}</span>
                </div>
                <p style="margin:0; font-size:0.95rem; color:#dbe2ff;">{narration_html}</p>
            </li>"""
        )
    steps_html = "".join(step_items) if step_items else '<li class="muted">No step breakdown recorded.</li>'

    content = f"""
    <section class="hero" style="padding-bottom: 24px; padding-top: 40px;">
      <div style="margin-bottom: 16px;">
        <a href="/theater" style="color:#8ea7ff; text-decoration:none;">← Back to Theater</a>
      </div>
      <div class="eyebrow">Product Walkthrough</div>
      <h1 style="font-size: clamp(2.2rem, 5vw, 3.8rem);">{html.escape(title)}</h1>
      <p class="muted">{html.escape(duration)} · {html.escape(finished_at)}</p>
    </section>
    
    <div style="background:#080b11; border:1px solid #2a354d; border-radius:14px; overflow:hidden; margin-bottom:32px;">
      <video controls autoplay preload="auto" style="width:100%; max-height:75vh; display:block;" src="{video_url(session_id)}"></video>
    </div>

    <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:16px; margin-bottom:32px;">
      <div style="display:flex; gap:12px;">
        <a class="button" href="{video_url(session_id)}" download>Download MP4</a>
        <button type="button" class="button secondary" data-share-path="/watch/{session_id}" data-share-title="{html.escape(title, quote=True)}">Share link</button>
        <a class="button secondary" href="/theater">All Videos</a>
        <button type="button" class="delete-button" data-delete-id="{session_id}" data-delete-title="{html.escape(title, quote=True)}">Delete video</button>
      </div>
    </div>

    <h2>Storyboard & Narration Script</h2>
    <ol style="list-style:none; padding:0;">
      {steps_html}
    </ol>
    {video_admin_script()}
    """
    return page_shell(
        title,
        content,
        f"Watch the narrated demo for {title}.",
        f"/watch/{session_id}",
        base_url,
    )


def theater_page(base_url: str = "/") -> str:
    """Render the public showcase of finished videos."""
    videos = store.list_finished_sessions()
    cards = "".join(video_card(video) for video in videos)
    if not cards:
        cards = '<div class="placeholder" data-empty><p>No finished videos yet. Check back soon.</p></div>'
    content = f"""
    <section class="hero" style="padding-bottom: 28px;">
      <div class="eyebrow">Public showcase</div>
      <h1>Theater.</h1>
      <p class="lede">Watch the latest narrated product stories created by
      reel-studio agents.</p>
      <button id="manage-toggle" class="button secondary" type="button" style="margin-top:16px;">Manage videos</button>
    </section>
    <div id="theater-videos" class="theater-grid">
      {cards}
    </div>
    {video_refresh_script("theater-videos")}
    {video_admin_script()}
    """
    return page_shell(
        "Public video theater",
        content,
        "Watch narrated browser tutorials directed entirely by AI agents.",
        "/theater",
        base_url,
    )


def backlog_item(item: dict) -> str:
    title = html.escape(item["title"])
    detail = html.escape(item["detail"])
    category = html.escape(item["category"])
    severity = html.escape(item["severity"])
    status_key = (
        item["status"]
        if item["status"] in store.BACKLOG_STATUSES
        else "open"
    )
    status = html.escape(status_key)
    created_at = html.escape(item["created_at"])
    note = html.escape(item.get("note") or "")
    updated_at = html.escape(item.get("updated_at") or created_at)
    muted = " backlog-item-muted" if status_key in {"shipped", "wont_fix"} else ""
    return f"""
    <article class="backlog-item card{muted}" data-backlog-id="{html.escape(item["id"], quote=True)}">
      <h3>{title}</h3>
      <div class="badges">
        <span class="badge">{category}</span>
        <span class="badge">{severity} severity</span>
        <span class="badge status-badge status-{status}">{status}</span>
      </div>
      <p>{detail or '<span class="muted">No additional detail.</span>'}</p>
      {f'<p class="muted">Note: {note}</p>' if note else ''}
      <p class="muted">Updated {updated_at}</p>
    </article>"""


def backlog_status_summary(items: list[dict]) -> str:
    counts = {status: 0 for status in store.BACKLOG_STATUSES}
    for item in items:
        if item["status"] in counts:
            counts[item["status"]] += 1
    return "".join(
        f'<span class="badge status-badge status-{status}">'
        f'{status.replace("_", " ")}: {counts[status]}</span>'
        for status in store.BACKLOG_STATUSES
    )


def backlog_page(base_url: str = "/") -> str:
    """Render the public agent-improvement roadmap."""
    items = store.list_backlog()
    cards = "".join(backlog_item(item) for item in items)
    if not cards:
        cards = '<div class="placeholder"><p>No backlog items yet. Agents can submit the next improvement through MCP.</p></div>'
    content = f"""
    <section class="hero" style="padding-bottom: 28px;">
      <div class="eyebrow">Open-source roadmap</div>
      <h1>Agent backlog.</h1>
    <p class="lede">A public list of the improvements agents ask for while
      directing real product stories.</p>
    <div class="status-summary">{backlog_status_summary(items)}</div>
    </section>
    <div class="theater-grid">
      {cards}
    </div>
    """
    return page_shell(
        "Agent backlog",
        content,
        "Explore the open roadmap of improvements requested by directing agents.",
        "/backlog",
        base_url,
    )


def bug_report_page(base_url: str = "/") -> str:
    """Render the public bug-report roadmap."""
    items = store.list_backlog(category="bug")
    cards = "".join(backlog_item(item) for item in items)
    if not cards:
        cards = '<div class="placeholder"><p>No bug reports yet. Agents can submit one through MCP.</p></div>'
    content = f"""
    <section class="hero" style="padding-bottom: 28px;">
      <div class="eyebrow">Open-source bug reports</div>
      <h1>Bug reports.</h1>
    <p class="lede">Known product and directing-agent problems submitted from
      real recording sessions.</p>
    <div class="status-summary">{backlog_status_summary(items)}</div>
    </section>
    <div class="theater-grid">
      {cards}
    </div>
    """
    return page_shell(
        "Public bug reports",
        content,
        "Track known reel-studio bugs found while directing real product stories.",
        "/bug_report",
        base_url,
    )


def _resolve_output_size(
    requested: str | None,
    capture_width: int,
    capture_height: int,
) -> tuple[int, int] | None:
    value = requested or os.environ.get("REEL_OUTPUT_SIZE")
    if not value:
        return None
    try:
        width_text, height_text = value.lower().replace(" ", "").split("x", 1)
        width, height = int(width_text), int(height_text)
    except (TypeError, ValueError):
        raise ValueError("output_size must use WIDTHxHEIGHT, for example 1280x720")
    if width <= 0 or height <= 0:
        raise ValueError("output_size dimensions must be positive")
    if width > capture_width or height > capture_height:
        raise ValueError("output_size cannot exceed the capture dimensions")
    return width, height


def step_hold_seconds(step: dict, narration_duration: float) -> float:
    """How long a step holds in the video: the longer of its narration and
    its caption or annotation, as when it was recorded.

    rerender used the narration alone, so every caption held longer than its
    line was cut back to the line: a 73.8 s take came back at 67.2 s with the
    holds gone (2026-10-09). The caption's own length is stored with the step
    now; a step recorded before that has none, and keeps the old behaviour.
    """
    return max(narration_duration, float(step.get("annotation_seconds") or 0.0))


def _title_background(value: str) -> str:
    value = (value or "").strip()
    if value.lower() in {"solid", "auto"}:
        return value.lower()
    if value.startswith("https://") and len(value) <= 2000:
        return value
    return "auto"


def _render_config(
    title: str,
    subtitle: str,
    accent: str,
    cta_url: str,
    cta_text: str,
    music: str,
    transitions: str = "smooth",
    title_background: str = "auto",
) -> RenderConfig:
    normalized_accent = accent.strip()
    if not re.fullmatch(r"#?[0-9a-fA-F]{6}", normalized_accent):
        normalized_accent = "#1f2a44"
    elif not normalized_accent.startswith("#"):
        normalized_accent = f"#{normalized_accent}"
    normalized_music = music.strip().lower()
    if normalized_music != "subtle":
        normalized_music = "none"
    return RenderConfig(
        title=title.strip(),
        subtitle=subtitle.strip(),
        accent=normalized_accent,
        cta_url=cta_url.strip(),
        cta_text=cta_text.strip() or "Learn more",
        music=normalized_music,
        transitions="cuts" if transitions.strip().lower() == "cuts" else "smooth",
        title_background=_title_background(title_background),
    )


def docs_page(base_url: str = "/") -> str:
    """Render the detailed MCP and API reference."""
    endpoint = html.escape(mcp_endpoint())
    content = f"""
    <section class="hero" style="padding-bottom: 28px;">
      <div class="eyebrow">MCP API reference</div>
      <h1>Storyboard your product demo.</h1>
      <p class="lede">An agent like Claude loops through observe → act, adding
      narration to each step, until the story is complete.</p>
    </section>
    <p class="endpoint">MCP endpoint: <code>{endpoint}</code></p>
    <h2>Tools</h2>
    <div class="tool card"><h3><code>start_session(start_url, width, height, voice, provider?, output_size?, title?, subtitle?, accent?, cta_url?, cta_text?, music?)</code></h3>
      <p>Launch headed Chromium and begin recording.</p>
      <p><strong>Returns:</strong> <code>{{"session_id"}}</code>. Width defaults to
      1920, height to 1080, and voice to <code>en-US-JennyNeural</code>. The
      optional <code>output_size</code> (for example <code>1280x720</code>)
      downscales only the final MP4; capture stays at the requested viewport
      size. <code>REEL_OUTPUT_SIZE</code> provides the same default. TTS
      defaults to the free <code>edge</code> provider. Set provider to
      <code>elevenlabs</code> for premium voices; this requires
      <code>ELEVENLABS_API_KEY</code>. Optional title/subtitle and
      <code>cta_url</code>/<code>cta_text</code> add server-rendered three-second
      intro/outro cards; <code>accent</code> controls their hex background.
      Set <code>music</code> to <code>subtle</code> for a quiet generated music
      bed; it defaults to <code>none</code>.</p></div>
    <div class="tool card"><h3><code>observe(session_id)</code></h3>
      <p>Capture the current screen and discover interactive elements.</p>
      <p><strong>Returns:</strong> <code>{{"screenshot_path", "url", "title",
      "page_text", "elements":[], "refs_stale": false}}</code> plus a viewable image.
      Each element includes a stable <code>ref</code>, role, text, and bounding box.</p></div>
    <div class="tool card"><h3><code>act(session_id, action, narration?)</code></h3>
      <p>Perform one browser action. Add optional narration to hold the moment
      on screen while its voice clip is scheduled.</p>
      <p><strong>Returns:</strong> <code>{{"ok", "offset_seconds", "url",
      "title", "changed", "narration_duration", "padding_applied",
      "refs_stale"}}</code> plus a viewable image. In-flow failures return
      <code>{{"ok": false, "error": {{"type", "message"}}}}</code> and a
      current image when possible. Re-observe when <code>refs_stale</code> is true.</p>
      <p><strong>Actions:</strong>
      <code>goto{{url}}</code>, <code>click{{ref}}</code>,
      <code>click_and_wait{{ref,target_text?,wait_for_url?,wait_for_text?,settle_ms?}}</code>,
      <code>type{{ref,text}}</code>, <code>select_option{{ref,text}}</code>,
      <code>press_key{{ref,text}}</code>, <code>set_zoom{{text}}</code>,
      <code>annotate{{ref,text,style,ms,dim}}</code>,
      <code>scroll{{dy}}</code>,
      <code>scroll_to_text{{text}}</code>,
      <code>hover{{ref}}</code>, <code>highlight{{ref}}</code>, and
      <code>wait{{ms}}</code>. Refs come from <code>observe</code>. Narration
      defaults to <code>after_settle</code>; use <code>before_action</code> only
      for deliberate pre-action narration. An annotation follows its target
      through scroll, resize, and layout changes and remains visible for the
      longer of its own duration and the narration duration. Use
      <code>target_text</code> when a parent navigation label and its child
      module have similar names.</p></div>
    <div class="tool card"><h3><code>assert_visible(session_id, text)</code></h3>
      <p>Check whether visible text is present without recording a storyboard
      step or changing the rendered video.</p>
      <p><strong>Returns:</strong> <code>{{"visible", "box", "in_viewport"}}</code>.
      A missing text match returns <code>visible: false</code>, not an error.</p></div>
    <div class="tool card"><h3><code>get_status(session_id)</code></h3>
      <p>Returns elapsed seconds, recorded step count, total narrated seconds,
      and estimated final video length.</p></div>
    <div class="tool card"><h3><code>list_sessions(limit=20)</code></h3>
      <p>Lists recent sessions from durable SQLite metadata, including status,
      step count, duration, and video URL.</p></div>
    <div class="tool card"><h3><code>get_session(session_id)</code></h3>
      <p>Returns the stored session row and ordered storyboard steps. Finished
      metadata remains available after a server restart; abandoned active
      sessions are reported as stale and are not resumed. The response also
      includes director storyboard shots.</p></div>
    <div class="tool card"><h3><code>begin_shot(session_id, shot_id, intent, framing, zoom?, focus_ref?, focus_text?)</code></h3>
      <p>Declare the director's intent before recording a scene, and move the
      camera: <code>wide</code> shows the whole page, <code>medium</code> pushes in
      to 1.5x and <code>close</code> to 2x on <code>focus_ref</code> or
      <code>focus_text</code>; an explicit zoom is between <code>1</code> and
      <code>2.5</code>. The move eases in over a second from the shot's first
      step.</p></div>
    <div class="tool card"><h3><code>verify_shot(session_id, shot_id, verified, verification_note?)</code></h3>
      <p>Record whether the intended focus was visible, readable, and aligned
      with the narration. Failed shots become <code>needs_review</code>.</p></div>
    <p class="muted">If a session has storyboard shots, <code>finish</code> refuses
    to publish while any shot is still <code>planned</code> or
    <code>needs_review</code>.</p>
    <div class="tool card"><h3><code>review_session(session_id)</code></h3>
      <p>Analyze storyboard steps and session metadata for security issues
      (accidental tokens, credentials, and URL secrets), focus/narration alignment,
      and ending quality before finishing or publishing.</p></div>
    <div class="tool card"><h3><code>update_step_narration(session_id, index, narration, voice?)</code></h3>
      <p>Replace the narration text (and optionally the voice) for one step
      in a finished session. The recorded browser video is not changed.</p>
      <p><strong>Returns:</strong> <code>{{"ok": true, "step": {{...}}}}</code>,
      or a structured error for an unknown session, unfinished session, or
      invalid step index.</p></div>
    <div class="tool card"><h3><code>rerender(session_id)</code></h3>
      <p>Re-synthesizes every current step narration at its recorded offset
      and replaces the audio on the existing video. The browser is not
      re-recorded and the video URL stays the same.</p>
      <p><strong>Returns:</strong> <code>{{"ok", "duration", "video_url",
      "warnings":[]}}</code>. Warnings identify steps whose narration is
      longer than the available gap; the last frame is held if audio extends
      past the original video.</p></div>
    <div class="tool card"><h3><code>submit_backlog(title, detail?, category?, severity?, session_id?)</code></h3>
      <p>Submit a tool-improvement request when the agent hits friction.
      Categories are <code>feature</code>, <code>bug</code>, or
      <code>friction</code>; severities are <code>low</code>,
      <code>normal</code>, or <code>high</code>. Requests are stored in the
      public roadmap.</p></div>
    <div class="tool card"><h3><code>list_backlog(limit=50, status?)</code></h3>
      <p>List recent agent backlog requests, optionally filtered by status.
      The public roadmap is available at <a href="/backlog">/backlog</a> and
      its JSON feed at <code>/api/backlog</code>.</p></div>
    <div class="tool card"><h3><code>update_backlog(id, status, note?)</code></h3>
      <p>Move a backlog item through <code>open</code>, <code>planned</code>,
      <code>in_progress</code>, <code>shipped</code>, or
      <code>wont_fix</code>, optionally recording resolution context. Unknown
      statuses normalize to <code>open</code>; unknown IDs return a structured
      error.</p></div>
    <p>Bug-category requests are also collected at the public
    <a href="/bug_report">/bug_report</a> page and
    <code>/api/bug_reports</code> feed.</p>
    <div class="tool card"><h3><code>finish(session_id)</code></h3>
      <p>Stop recording, mix narration, and render the final MP4.</p>
      <p><strong>Returns:</strong> <code>{{"video_path", "video_url"}}</code>.
      The URL is present when <code>REEL_PUBLIC_BASE_URL</code> is configured.</p></div>
    <div class="tool card"><h3><code>prune(max_clips?, keep_raw_clips?)</code></h3>
      <p>Run best-effort storage cleanup. <code>max_clips</code> keeps the
      newest finished sessions and removes older session directories and
      metadata. <code>keep_raw_clips</code> keeps <code>screen.mp4</code> only
      for the newest sessions; final public videos remain available after raw
      recordings are removed. Omitted values use
      <code>REEL_MAX_CLIPS</code> (default 50) and
      <code>REEL_KEEP_RAW_CLIPS</code> (default 0, keep all retained raw
      recordings).</p>
      <p><code>REEL_MAX_CLIPS</code> defaults to <code>50</code>; set it to
      <code>0</code> to disable pruning. <code>REEL_KEEP_RAW_CLIPS</code> defaults to <code>0</code>, retaining raw recordings for every retained
      session. Removing raw recordings preserves the public video but makes
      rerender unavailable.</p>
    </div>
    <div class="tool card"><h3><code>delete_session(session_id, confirm, force)</code></h3>
      <p>Delete one finished session, including its public MP4, raw recording,
      screenshots, narration clips, storyboard metadata, and theater entry.
      This requires <code>confirm=true</code>. Active sessions are protected
      unless <code>force=true</code> is passed, which aborts any live runtime
      and removes stale sessions orphaned by a restart.</p>
      <p><strong>HTTP:</strong> <code>DELETE /api/videos/{session_id}?confirm=true</code>
      performs the same deletion with the same bearer token and returns
      <code>{{"deleted": true, "session_id": ...}}</code>. The theater's
      <em>Manage videos</em> mode uses this endpoint.</p>
    </div>
    <div class="tool card"><h3><code>act_batch(session_id, steps)</code></h3>
      <p>Run up to 20 actions in one call. Each step is
      <code>{"action": ..., "narration": ...}</code> with the same contract as
      <code>act</code>. Steps execute in order and stop at the first failure,
      returning one result per executed step plus the final screenshot.</p>
    </div>
    <h2>The storyboard workflow</h2>
    <p>Give the agent a product story, then let it loop: call
    <code>observe</code>, choose one useful next step, call <code>act</code>
    with a concise narration, and repeat. Use the returned refs to target
    controls precisely. When the story lands, call <code>finish</code> to get
    the MP4 and its download URL.</p>
    <p>To refine a finished story cheaply, call
    <code>update_step_narration</code> for one or more steps, then call
    <code>rerender</code>. This rebuilds only the narration track while
    preserving the recorded browser timeline.</p>
    <h2>Auth and video delivery</h2>
    <p>MCP requests use
    <code>Authorization: Bearer &lt;REEL_API_TOKEN&gt;</code>. The token is
    configured with the server's <code>REEL_API_TOKEN</code> environment
    variable. Finished videos are public by default: browse
    <a href="/theater">/theater</a>, query <code>/api/videos</code>, or play
    the relative <code>video_url</code> directly without a token.</p>
    <p><a href="/">← Back to the reel-studio overview</a></p>
    """
    content += '<p class="muted">Agent brief: <a href="/llms.txt">/llms.txt</a></p>'
    return page_shell(
        "MCP and API docs",
        content,
        "Learn how AI agents use reel-studio to direct narrated browser tutorials.",
        "/docs",
        base_url,
    )


@mcp.custom_route("/", methods=["GET"], include_in_schema=False)
async def home(request: Request) -> Response:
    """Serve the public marketing landing page."""
    base = str(request.base_url).rstrip("/")
    return HTMLResponse(landing_page(base))


@mcp.custom_route("/docs", methods=["GET"], include_in_schema=False)
async def docs(request: Request) -> Response:
    """Serve the public MCP and API documentation."""
    base = str(request.base_url).rstrip("/")
    return HTMLResponse(docs_page(base))


@mcp.custom_route("/favicon.svg", methods=["GET"], include_in_schema=False)
async def favicon_svg(request: Request) -> Response:
    """Serve the inline brand favicon."""
    return Response(FAVICON_SVG, media_type="image/svg+xml")


@mcp.custom_route("/favicon.png", methods=["GET"], include_in_schema=False)
async def favicon_png(request: Request) -> Response:
    """Serve the cached raster favicon."""
    return FileResponse(favicon_png_path(), media_type="image/png")


@mcp.custom_route("/og.png", methods=["GET"], include_in_schema=False)
async def og_image(request: Request) -> Response:
    """Serve the cached social-share image."""
    return FileResponse(og_image_path(), media_type="image/png")


@mcp.custom_route("/robots.txt", methods=["GET"], include_in_schema=False)
async def robots(request: Request) -> Response:
    """Serve crawler guidance with a domain-local sitemap and agent brief."""
    base = str(request.base_url).rstrip("/")
    return Response(build_robots(base), media_type="text/plain")


@mcp.custom_route("/sitemap.xml", methods=["GET"], include_in_schema=False)
async def sitemap(request: Request) -> Response:
    """Serve the public static-page sitemap."""
    base = str(request.base_url).rstrip("/")
    return Response(build_sitemap(base), media_type="application/xml")


@mcp.custom_route("/llms.txt", methods=["GET"], include_in_schema=False)
async def llms(request: Request) -> Response:
    """Serve the concise agent-facing service brief."""
    base = str(request.base_url).rstrip("/")
    return Response(build_llms_txt(base), media_type="text/plain")


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(request: Request) -> Response:
    """Return a lightweight service health response."""
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/theater", methods=["GET"], include_in_schema=False)
async def theater(request: Request) -> Response:
    """Serve the public finished-video showcase."""
    base = str(request.base_url).rstrip("/")
    return HTMLResponse(theater_page(base))


@mcp.custom_route("/watch/{session_id}", methods=["GET"], include_in_schema=False)
async def watch(request: Request) -> Response:
    """Serve the dedicated theater watch page for a specific video."""
    session_id = request.path_params["session_id"]
    if not re.fullmatch(r"[0-9a-f]+", session_id):
        return HTMLResponse("<h1>Video not found</h1>", status_code=404)
    base = str(request.base_url).rstrip("/")
    html_content = watch_page(session_id, base)
    if not html_content:
        return HTMLResponse("<h1>Video not found or still processing</h1>", status_code=404)
    return HTMLResponse(html_content)


@mcp.custom_route("/api/videos", methods=["GET"], include_in_schema=False)
async def videos_api(request: Request) -> Response:
    """Return finished videos for public theater refreshes."""
    return JSONResponse(
        [
            {
                "id": video["id"],
                "start_url": sanitize_public_url(video["start_url"]),
                "title": clean_display_title(video.get("title") or video["start_url"]),
                "duration_seconds": video["duration_seconds"],
                "finished_at": video["finished_at"],
            }
            for video in store.list_finished_sessions()
        ]
    )


@mcp.custom_route("/backlog", methods=["GET"], include_in_schema=False)
async def backlog(request: Request) -> Response:
    """Serve the public agent-improvement roadmap."""
    base = str(request.base_url).rstrip("/")
    return HTMLResponse(backlog_page(base))


@mcp.custom_route("/api/backlog", methods=["GET"], include_in_schema=False)
async def backlog_api(request: Request) -> Response:
    """Return public backlog items as JSON."""
    return JSONResponse(store.list_backlog())


@mcp.custom_route("/bug_report", methods=["GET"], include_in_schema=False)
async def bug_report(request: Request) -> Response:
    """Serve the public bug-report roadmap."""
    base = str(request.base_url).rstrip("/")
    return HTMLResponse(bug_report_page(base))


@mcp.custom_route("/api/bug_reports", methods=["GET"], include_in_schema=False)
async def bug_reports_api(request: Request) -> Response:
    """Return public bug-category backlog items as JSON."""
    return JSONResponse(store.list_backlog(category="bug"))


# Sessions nobody is directing any more.
#
# A session records from start_session until finish. An agent that gave up on
# a take and started another left the first one recording — browser, X display
# and a 1080p encoder each — and on 2026-10-07 five of them ran at once on one
# CPU core, which is why every observe took tens of seconds. A session with no
# tool call for REEL_IDLE_TIMEOUT_SECONDS (default 900, 0 disables) is aborted:
# recording stops, its media stays on disk, and it is marked as ended without a
# video. Checked once a minute.
def idle_timeout_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("REEL_IDLE_TIMEOUT_SECONDS", "900")))
    except ValueError:
        return 900.0


def idle_session_ids(live: dict, now: float, timeout: float) -> list[str]:
    """Ids of live sessions untouched for longer than timeout (0 = never)."""
    if timeout <= 0:
        return []
    return [
        session_id for session_id, session in live.items()
        if now - (getattr(session, "last_activity", 0.0) or getattr(session, "t0", now)) > timeout
    ]


async def reap_idle_sessions(now: float | None = None) -> list[str]:
    reaped = []
    for session_id in idle_session_ids(sessions, time.monotonic() if now is None else now, idle_timeout_seconds()):
        session = sessions.pop(session_id, None)
        if session is None:
            continue
        try:
            await session.abort()
        except Exception:
            pass
        store.mark_session_error(session_id)
        reaped.append(session_id)
        print(f"[reel-studio] stopped idle session {session_id}: no tool call for {idle_timeout_seconds():.0f}s", flush=True)
    return reaped


_reaper_task: asyncio.Task | None = None


def ensure_idle_reaper() -> None:
    """Start the once-a-minute idle check, the first time a session starts."""
    global _reaper_task
    if _reaper_task is not None and not _reaper_task.done():
        return

    async def loop() -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await reap_idle_sessions()
            except Exception as exc:  # never let the reaper die
                print(f"[reel-studio] idle check failed: {exc}", flush=True)

    _reaper_task = asyncio.create_task(loop())


def _touch(session_id: str) -> None:
    session = sessions.get(session_id)
    if session is not None and hasattr(session, "touch"):
        session.touch()


DIRECTOR_PROMPT = """You are directing a narrated screen recording with reel-studio.
The screen records from start_session until finish, so plan first and record once.

1. Before start_session: find your way around the site with your own browser
   tools, and write the script: the beats, and one or two sentences of narration
   per beat. Note what you need on the way: sign-in, cookie banner, inputs.
2. One session at a time. Every session you start ends with finish, even a
   failed probe; discard probes with delete_session(confirm=True, force=True).
3. """ + ACTION_CONTRACT + """
4. Use act_batch for each beat (up to 20 steps): one round trip, not one per click.
   An action that starts something slow (a model answering, a build) gets
   wait_for_text with wait_timeout_ms up to 60000; if it still times out, the
   click already happened — wait for the result, do not press it again.
5. observe once per page and reuse its refs; observe(detail="refs") when you only
   need refs. Observe again after the URL changes or an action reports stale_refs.
6. Narrate every step a viewer sees. Steps without narration are silent. Where the
   video will autoplay muted (LinkedIn, X), also put the key line on screen with a
   caption step. A caption with sticky=true stays until the next caption or page:
   use it for a beat that outlasts its line, such as waiting for an answer. A step
   that is silent on purpose, because its line was spoken on the step before (the
   sign-in click after "Signing in"), gets quiet=true and is not flagged, and
   lasts 0.6 s in the video.
   offscreen=true does a step without showing it: sign in, dismiss a cookie
   banner, get to the first page worth showing. Open the video on the product,
   not on a login form — the first seconds decide whether anyone watches.
   The voice is yours to choose: list_voices(language="en") lists them with
   their gender and personality; pass one as start_session(voice=...). It
   stays for the whole video. When list_voices says elevenlabs_available,
   list_voices(provider="elevenlabs") gives natural voices — use
   start_session(provider="elevenlabs", voice=<id>). rerender(voice=...,
   provider=...) re-voices a finished video without recording it again.
   The title and closing cards are a hero section by default: a still from
   your own video, blurred and dimmed under the text (title_background="auto").
   Pass an https image URL for a brand image, or "solid" for the accent colour.
   Joins between steps are chosen for you (transitions="smooth": a cut on a
   new page, a short dissolve where a cut would jump). transitions="cuts"
   makes every join a hard cut, for a brisker, more technical feel.
7. begin_shot before a beat and verify_shot after it; the description goes in
   intent. finish refuses to publish while a shot is unverified. begin_shot is
   also the camera, moved in post with the text kept sharp:
   - wide shows the whole page; medium pushes in 1.5x, close 2x, on focus_ref or
     focus_text. Always give a focus for medium and close.
   - The move starts with the shot's first step and takes a second: put the
     narration that names the detail on that step, so the camera lands as the
     voice gets there.
   - Establish wide on each new page, push in on the one detail that matters,
     pull back to wide before moving on. A new page returns to wide by itself.
   - Three to five push-ins in a 60-90 s video; not every beat. A camera that
     never rests is as tiring as one that never moves.
   - While pushed in, the camera pans to anything you click or type into off
     frame, and captions are drawn inside the frame.
   - Frame what the voice names. A line about a button is a shot of the button:
     push in medium on it before the click, not wide. A line about a result is
     a close on the result.
   - The focus must be on screen when you call begin_shot: scroll it into view
     first. focus_text frames a line from its first word — right for a result,
     a version line, a heading; it takes the first visible match, so pick words
     that appear once (a card's own description, not its title that the
     sidebar repeats). focus_ref centres an element — right for a button. A
     card has no ref of its own: use a line of its text. The reply's
     camera_frame is the rectangle the shot will show, in page pixels;
     camera_note means the focus was not found.
   - A medium frame shows two thirds of the page width: a heading and a button
     at opposite ends of a wide card do not both fit. Frame the one the voice
     is talking about.
   - get_status's estimated_video_length is the length of the video so far:
     the time between your calls is cut, so it does not count.
   - verify_shot right after its beat. A shot can be declared again with the
     same shot_id to correct it.
8. Keep secrets unreadable: start_session(mask=[CSS selectors]) blurs matching
   elements on every page from the first frame; the mask action blurs one element.
9. Several controls can share a name (a "Sign In" tab and a "Sign In" button):
   observe marks them same_name, and the one that submits a form submits_form.
10. Run review_session and re-record what it flags. Then call finish once. Narration
   wording is fixed after finish: update_step_narration for each line, then rerender.
11. Craft. The first three seconds decide whether anyone watches: open on the
   claim or the result, not on a login or a logo. Tell it as problem, what the
   product does about it, proof on screen, then one call to action. Narrate what
   the viewer gains, not the name of each button. Something should change every
   10-20 seconds: a new page, a push-in, a caption. Hold a still frame for a
   beat after a result appears, so it registers.
"""


@mcp.prompt(name="director", description="How to direct a narrated recording with reel-studio, start to finish")
def director() -> str:
    return DIRECTOR_PROMPT


_VOICE_CACHE: list[dict] = []


@mcp.tool()
async def list_voices(
    language: Annotated[
        str,
        Field(description="A language or locale prefix: \"en\", \"en-GB\", \"sv\". Empty lists all. Edge only; ElevenLabs voices speak every language."),
    ] = "en",
    gender: Annotated[
        str,
        Field(description="Female, Male, or empty for both."),
    ] = "",
    provider: Annotated[
        str,
        Field(description="edge (free, default) or elevenlabs (natural voices; needs ELEVENLABS_API_KEY on this reel-studio).",
              json_schema_extra={"enum": ["edge", "elevenlabs"]}),
    ] = "edge",
) -> dict:
    """List the narration voices start_session(voice=...) accepts.

    The free Edge provider has several hundred neural voices across languages;
    every session used to get the default en-US-JennyNeural because nothing
    told an agent what else there was. With an ElevenLabs key on this
    reel-studio, provider="elevenlabs" lists the account's voices — pass one's
    "voice" id with start_session(provider="elevenlabs", voice=...). Pick one
    that suits the audience and the brand — and keep it for the whole video.
    """
    wanted_gender = gender.strip().lower()
    if provider.strip().lower() == "elevenlabs":
        try:
            voices = await list_elevenlabs_voices()
        except TTSProviderError as exc:
            return {"ok": False, "error": {"type": "voices_unavailable", "message": str(exc)}}
        matches = [v for v in voices if not wanted_gender or v["gender"].lower() == wanted_gender]
        return {
            "ok": True,
            "provider": "elevenlabs",
            "use": 'start_session(provider="elevenlabs", voice=<voice>)',
            "count": len(matches),
            "voices": matches[:120],
        }
    if not _VOICE_CACHE:
        import edge_tts

        try:
            voices = await edge_tts.list_voices()
        except Exception as exc:
            return {"ok": False, "error": {"type": "voices_unavailable", "message": str(exc)}}
        _VOICE_CACHE.extend(
            {
                "voice": voice.get("ShortName"),
                "locale": voice.get("Locale"),
                "gender": voice.get("Gender"),
                "personality": ", ".join(
                    (voice.get("VoiceTag") or {}).get("VoicePersonalities") or []
                ),
            }
            for voice in voices
        )
    prefix = language.strip().lower()
    matches = [
        voice for voice in _VOICE_CACHE
        if (not prefix or (voice["locale"] or "").lower().startswith(prefix))
        and (not wanted_gender or (voice["gender"] or "").lower() == wanted_gender)
    ]
    return {
        "ok": True,
        "provider": "edge",
        "default": "en-US-JennyNeural",
        "elevenlabs_available": elevenlabs_configured(),
        "count": len(matches),
        "voices": matches[:120],
    }


@mcp.tool()
async def start_session(
    start_url: str, width: int = 1920, height: int = 1080,
    voice: str = "en-US-JennyNeural",
    provider: str | None = None,
    output_size: str | None = None,
    title: str = "",
    subtitle: str = "",
    accent: str = "#1f2a44",
    cta_url: str = "",
    cta_text: str = "Learn more",
    music: str = "none",
    transitions: Annotated[
        str,
        Field(description="How steps are joined. smooth (default): a hard cut on a page "
                          "change, a short dissolve where a cut would jump on the same page, "
                          "fades on the title and closing cards. cuts: every join a hard cut.",
              json_schema_extra={"enum": ["smooth", "cuts"]}),
    ] = "smooth",
    title_background: Annotated[
        str,
        Field(description="Behind the title and closing cards. auto (default): a still from "
                          "this video, blurred and dimmed under the text, like a hero section. "
                          "solid: the accent colour. Or an https image URL (up to 10 MB)."),
    ] = "auto",
    mask: Annotated[
        list[str] | None,
        Field(description="CSS selectors blurred on every page from the first frame, e.g. "
                          "[\"input[type=password]\", \"[data-secret]\"]. For anything that must "
                          "never be readable in the video: keys, tokens, personal data."),
    ] = None,
) -> dict:
    """Launch a headed browser and begin recording.

    Recording starts now and runs until finish, so plan the script and find
    your way around the site before calling this. Defaults: 1920x1080 capture
    (output_size, e.g. "1280x720", downscales only the final MP4); voice
    en-US-JennyNeural on the free Edge provider — list_voices shows the others
    (provider "elevenlabs" needs ELEVENLABS_API_KEY and takes a voice id); no
    music ("subtle" adds a quiet bed); title/subtitle add a 3-second intro
    card, cta_url/cta_text an outro; transitions smooth (default) or cuts.

    mask blurs matching elements on every page from the first frame; the
    mask action does the same for one element mid-recording.

    One session at a time: every session you start must end with finish, or
    it keeps recording until it has been idle for REEL_IDLE_TIMEOUT_SECONDS.
    The response lists other sessions that are still recording. See the
    "director" prompt for the whole workflow.
    """
    try:
        selected_provider = normalize_provider(provider)
        validate_provider(selected_provider)
    except TTSProviderError as exc:
        return {
            "ok": False,
            "error": {"type": "tts_provider_unconfigured", "message": str(exc)},
        }
    try:
        selected_output_size = _resolve_output_size(output_size, width, height)
    except ValueError as exc:
        return {
            "ok": False,
            "error": {"type": "invalid_output_size", "message": str(exc)},
        }
    try:
        mask_stylesheet(mask)
    except ValueError as exc:
        return {"ok": False, "error": {"type": "invalid_mask", "message": str(exc)}}
    render_config = _render_config(
        title, subtitle, accent, cta_url, cta_text, music, transitions, title_background
    )
    session = await BrowserSession.create(
        start_url, width, height, voice, selected_provider, selected_output_size,
        render_config, mask_selectors=mask,
    )
    session.touch()
    others = [other for other in sessions if other != session.session_id]
    sessions[session.session_id] = session
    ensure_idle_reaper()
    store.create_session(
        session.session_id,
        start_url,
        voice,
        width,
        height,
        str(session.directory),
        selected_provider,
        *(selected_output_size or (None, None)),
        render_config.title,
        render_config.subtitle,
        render_config.accent,
        render_config.cta_url,
        render_config.cta_text,
        render_config.music,
        render_config.transitions,
        render_config.title_background,
    )
    result: dict = {"session_id": session.session_id}
    if others:
        result["active_sessions"] = others
        result["warning"] = (
            f"{len(others)} other session(s) are still recording: {', '.join(others)}. "
            "Finish them, or delete_session(confirm=True, force=True) to discard them; "
            "each one costs a browser and a video encoder."
        )
    return result


def observe_timeout_seconds() -> float:
    try:
        return max(1.0, float(os.environ.get("REEL_OBSERVE_TIMEOUT_SECONDS", "60")))
    except ValueError:
        return 60.0


@mcp.tool()
async def observe(
    session_id: str,
    detail: Annotated[
        Literal["full", "refs"],
        Field(description='"full": screenshot, page text and element boxes. "refs": only ref, role and text per element — much smaller and faster; use it when you only need refs to act.'),
    ] = "full",
) -> CallToolResult:
    """Capture the current browser UI and its interactive elements.

    Refs stay valid until the URL changes or an action reports stale_refs, so
    observe once per page and reuse them.
    """
    session = sessions.get(session_id)
    if session is None:
        return feedback_result(_unknown_session(session_id))
    session.touch()
    try:
        payload, screenshot = await asyncio.wait_for(
            session.observe(detail), timeout=observe_timeout_seconds()
        )
    except asyncio.TimeoutError:
        session.refs_stale = True
        return feedback_result({
            "ok": False,
            "error": {
                "type": "observe_timeout",
                "message": f"observe did not finish within {observe_timeout_seconds():.0f}s",
            },
            "hint": 'Try observe(detail="refs"), which skips the screenshot and page text.',
        })
    return feedback_result(payload, screenshot)


def _unknown_session(session_id: str) -> dict:
    """An unknown id, with the ids that do exist so the caller can recover."""
    return {
        "ok": False,
        "error": {"type": "unknown_session", "message": f"Unknown session_id: {session_id}"},
        "active_sessions": list(sessions),
    }


# Steps a viewer sees happen. Without narration they play in silence; the
# finished take on 2026-10-07 was 44 seconds with one narrated step.
SILENT_WARNING_TYPES = {"goto", "click", "click_and_wait", "type", "select_option", "press_key", "scroll_to_text"}

ActionParam = Annotated[dict[str, Any], Field(json_schema_extra=action_json_schema())]


async def _run_action(
    session_id: str, action: object, narration: str
) -> tuple[dict, object]:
    """Validate and perform one action, persisting its storyboard step."""
    session = sessions[session_id]
    session.touch()
    try:
        parsed_action = Action.model_validate(action)
    except ValidationError as exc:
        payload, screenshot = await session.error_result("invalid_action", str(exc))
        payload["hint"] = ACTION_CONTRACT
        store.append_step(
            session_id,
            action.get("type") if isinstance(action, dict) else None,
            action.get("ref") if isinstance(action, dict) else None,
            payload.get("url"),
            payload.get("title"),
            narration,
            0,
            payload.get("offset_seconds"),
            payload.get("screenshot_path"),
            False,
            "invalid_action",
            session.voice,
        )
        return payload, screenshot
    payload, screenshot = await session.act(parsed_action, narration)
    error_type = (payload.get("error") or {}).get("type")
    if error_type in {"unknown_ref", "stale_refs"}:
        known = list(session.refs)
        payload["hint"] = (
            'Call observe (detail="refs" is fast) and use a ref from it.'
            + (f" Refs from the last observe: {', '.join(known[:40])}" if known and error_type == "unknown_ref" else "")
        )
    elif (payload.get("ok") and not narration.strip() and not parsed_action.quiet
          and parsed_action.type in SILENT_WARNING_TYPES):
        payload["warning"] = "No narration: this step will be silent in the final video."
    store.append_step(
        session_id,
        parsed_action.type,
        parsed_action.ref or parsed_action.url,
        payload.get("url"),
        payload.get("title"),
        narration,
        payload.get("narration_duration", 0),
        payload.get("offset_seconds"),
        payload.get("screenshot_path"),
        payload.get("ok", False),
        (payload.get("error") or {}).get("type"),
        session.voice,
        parsed_action.quiet,
        parsed_action.offscreen,
        payload.get("annotation_duration") or 0.0,
        payload.get("narration_clip"),
    )
    return payload, screenshot


@mcp.tool()
async def act(session_id: str, action: ActionParam, narration: str = "") -> CallToolResult:
    """Perform exactly one browser action, narrating it.

    Narration is spoken over this step in the final video; a step without it
    plays in silence. For a beat of several steps use act_batch instead: one
    round trip instead of one per click.
    """
    if session_id not in sessions:
        return feedback_result(_unknown_session(session_id))
    payload, screenshot = await _run_action(session_id, action, narration)
    return feedback_result(payload, screenshot)


MAX_BATCH_STEPS = 20


@mcp.tool()
async def act_batch(
    session_id: str,
    steps: Annotated[
        list[dict[str, Any]],
        Field(json_schema_extra={"items": {
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": action_json_schema(),
                "narration": {"type": "string"},
                "quiet": {"type": "boolean"},
                "sticky": {"type": "boolean"},
                "offscreen": {"type": "boolean"},
            },
        }}),
    ],
) -> CallToolResult:
    """Perform a sequence of browser actions in a single call.

    Each step is ``{"action": {...}, "narration": "..."}`` using the same
    action contract as ``act``. Steps run in order and execution stops at
    the first failed step, so a director can record a whole beat (click,
    settle, annotate) without extra round trips. The response carries one
    result per executed step plus the screenshot of the last executed step.
    """
    if session_id not in sessions:
        return feedback_result(_unknown_session(session_id))
    if not steps:
        return feedback_result(
            {"ok": False, "error": {"type": "invalid_batch", "message": "steps must be a non-empty list"}}
        )
    if len(steps) > MAX_BATCH_STEPS:
        return feedback_result(
            {"ok": False, "error": {"type": "invalid_batch", "message": f"at most {MAX_BATCH_STEPS} steps per batch"}}
        )
    results: list[dict] = []
    screenshot = None
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or not isinstance(step.get("action"), dict):
            session = sessions[session_id]
            payload, screenshot = await session.error_result(
                "invalid_batch", f"step {index} must be an object with an action"
            )
            results.append(payload)
            break
        action = dict(step["action"])
        # The flags belong inside the action, but a step-level one is plainly
        # meant for it too: an agent that wrote {"action": {...}, "quiet": true}
        # had every flag silently dropped (2026-10-09).
        for flag in ("quiet", "sticky", "offscreen"):
            if flag in step and flag not in action:
                action[flag] = step[flag]
        payload, screenshot = await _run_action(
            session_id, action, str(step.get("narration") or "")
        )
        results.append(payload)
        if not payload.get("ok"):
            break
    summary = {
        "ok": bool(results) and all(step.get("ok") for step in results),
        "completed": sum(1 for step in results if step.get("ok")),
        "executed": len(results),
        "remaining": len(steps) - len(results),
        "steps": results,
    }
    return feedback_result(summary, screenshot)


@mcp.tool()
async def assert_visible(session_id: str, text: str) -> dict:
    """Check for visible text without recording a storyboard step."""
    _touch(session_id)
    session = sessions.get(session_id)
    if session is None:
        return {
            "ok": False,
            "error": {
                "type": "unknown_session",
                "message": f"Unknown session_id: {session_id}",
            },
        }
    return await session.assert_visible(text)


@mcp.tool()
async def get_status(session_id: str) -> dict:
    """Return recording progress and an estimated final video length."""
    _touch(session_id)
    session = sessions.get(session_id)
    if session is not None:
        return session.status()
    status = store.get_status(session_id)
    if status is None:
        raise KeyError(f"Unknown session_id: {session_id}")
    return status


@mcp.tool()
async def list_sessions(limit: int = 20) -> list[dict]:
    """List recent recording sessions with token-free start URLs."""
    sessions_found = store.list_sessions(limit)
    for session in sessions_found:
        session["start_url"] = sanitize_public_url(session.get("start_url"))
    return sessions_found


@mcp.tool()
async def get_session(session_id: str) -> dict:
    """Return a durable session and its ordered storyboard steps."""
    session = store.get_session(session_id)
    if session is None:
        raise KeyError(f"Unknown session_id: {session_id}")
    return session


@mcp.tool()
async def begin_shot(
    session_id: str,
    shot_id: str,
    intent: str,
    framing: Annotated[
        str,
        Field(description="How much of the page the shot shows: wide, medium or close.",
              json_schema_extra={"enum": list(FRAMINGS)}),
    ],
    zoom: Annotated[
        float | None,
        Field(description="Overrides the framing's zoom: 1 (whole page) to 2.5."),
    ] = None,
    focus_ref: str | None = None,
    focus_text: str | None = None,
) -> dict:
    """Declare a director storyboard shot, and move the camera for it.

    wide shows the whole page; medium pushes in 1.5x and close 2x on focus_ref
    or focus_text. The move eases in over a second from the shot's next step.
    """
    _touch(session_id)
    if store.get_session(session_id) is None:
        return {"ok": False, "error": {"type": "unknown_session", "message": session_id}}
    framing = framing.strip().lower()
    if framing not in FRAMINGS:
        # Describe the shot in intent; framing is one of three words.
        return {
            "ok": False,
            "error": {"type": "invalid_framing", "message": framing},
            "hint": f"framing is one of {', '.join(FRAMINGS)}; put the description in intent.",
        }
    if zoom is not None and not 1.0 <= zoom <= MAX_ZOOM:
        return {
            "ok": False,
            "error": {"type": "invalid_zoom", "message": f"zoom must be 1-{MAX_ZOOM:g}"},
            "hint": "zoom is the camera: 1 is the whole page. To shrink the page itself, use the set_zoom action.",
        }
    if not intent.strip() or not shot_id.strip():
        return {"ok": False, "error": {"type": "invalid_shot", "message": "shot_id and intent are required"}}
    try:
        shot = store.begin_shot(
            session_id, shot_id.strip(), intent.strip(), framing, zoom,
            focus_ref.strip() if focus_ref else None,
            focus_text.strip() if focus_text else None,
        )
    except Exception as exc:
        return {"ok": False, "error": {"type": "shot_error", "message": str(exc)}}
    result: dict = {"ok": True, "shot": shot}
    live = sessions.get(session_id)
    if live is not None:
        result.update(await live.set_shot(
            framing, zoom,
            focus_ref.strip() if focus_ref else None,
            focus_text.strip() if focus_text else None,
        ))
    return result


@mcp.tool()
async def verify_shot(
    session_id: str,
    shot_id: str,
    verified: bool,
    verification_note: str = "",
) -> dict:
    """Record whether a storyboard shot met its teaching criterion."""
    _touch(session_id)
    try:
        stored = store.get_shot(session_id, shot_id.strip())
        if stored is None:
            raise ValueError(f"unknown shot: {shot_id}")
        note = verification_note.strip()
        # The live page says something only about the shot being recorded
        # now. Checked for an earlier shot, it failed whenever the director
        # had moved on to another page, and the only way to pass was to go
        # back — which put the detour in the video (2026-10-09).
        shots_so_far = (store.get_session(session_id) or {}).get("shots", [])
        is_current = bool(shots_so_far) and shots_so_far[-1]["shot_id"] == shot_id.strip()
        if verified and stored.get("focus_text") and is_current:
            live = sessions.get(session_id)
            if live is not None:
                visible = await live.assert_visible(stored["focus_text"])
                if not visible.get("visible"):
                    verified = False
                    note = note or f"Focus text not visible: {stored['focus_text']}"
        shot = store.verify_shot(session_id, shot_id.strip(), verified, note)
    except ValueError as exc:
        return {"ok": False, "error": {"type": "shot_not_found", "message": str(exc)}}
    return {"ok": True, "shot": shot}


def pending_shots(session: dict | None) -> list[dict]:
    """Return storyboard shots that are not explicitly verified."""
    if not session:
        return []
    return [shot for shot in session.get("shots", []) if shot.get("status") != "verified"]


def _editable_session(session_id: str) -> tuple[dict | None, dict | None]:
    session = store.get_session(session_id)
    if session is None:
        return None, {
            "ok": False,
            "error": {
                "type": "unknown_session",
                "message": f"Unknown session_id: {session_id}",
            },
        }
    video_path = Path(session["video_path"]) if session.get("video_path") else None
    if session["status"] != "finished" or video_path is None or not video_path.is_file():
        return None, {
            "ok": False,
            "error": {
                "type": "session_not_finished",
                "message": "Narration editing requires a finished session with a video.",
            },
            # The clips of a live take are already laid into its timeline;
            # an edit belongs to the finished video, where rerender re-times it.
            "hint": "Call finish first, then update_step_narration for each line and rerender once.",
        }
    return session, None


@mcp.tool()
async def update_step_narration(
    session_id: str,
    index: int,
    narration: str,
    voice: str | None = None,
) -> dict:
    """Update one finished session's storyboard narration."""
    session, error = _editable_session(session_id)
    if error:
        return error
    if index < 0 or index >= len(session["steps"]):
        return {
            "ok": False,
            "error": {
                "type": "step_not_found",
                "message": f"Unknown step index: {index}",
            },
        }
    updated = store.update_step_narration(
        session_id, index, narration.strip(), voice.strip() if voice else None
    )
    return {"ok": True, "step": updated}


@mcp.tool()
async def rerender(
    session_id: str,
    voice: Annotated[
        str | None,
        Field(description="Re-voice the whole video with this voice (from list_voices). Omit to keep the voice it has."),
    ] = None,
    provider: Annotated[
        str | None,
        Field(description="The voice's provider, edge or elevenlabs, when voice is given."),
    ] = None,
) -> dict:
    """Rebuild narration audio and mux it onto an existing finished video.

    With voice (and provider), every line is spoken again in that voice — the
    same take with another narrator, no re-recording. Without, lines that
    have not changed reuse their recorded audio.
    """
    session, error = _editable_session(session_id)
    if error:
        return error
    new_voice = (voice or "").strip() or None
    new_provider = None
    if new_voice:
        try:
            new_provider = normalize_provider(provider or session.get("provider") or "edge")
            validate_provider(new_provider)
        except TTSProviderError as exc:
            return {"ok": False, "error": {"type": "tts_provider_unconfigured", "message": str(exc)}}
        store.update_session_voice(session_id, new_voice, new_provider)
        session["voice"], session["provider"] = new_voice, new_provider
    video_path = Path(session["video_path"])
    output_dir = video_path.parent
    source_video = output_dir / "screen.mp4"
    if not source_video.is_file() or source_video.stat().st_size == 0:
        return {
            "ok": False,
            "error": {
                "type": "recording_missing",
                "message": f"Recording is missing or empty: {source_video}",
            },
        }
    clips: list[tuple[float, Path]] = []
    render_steps: list[tuple[float, Path | None, float]] = []
    warnings = []
    render_config = _render_config(
        session.get("title", ""),
        session.get("subtitle", ""),
        session.get("accent", "#1f2a44"),
        session.get("cta_url", ""),
        session.get("cta_text", "Learn more"),
        session.get("music", "none"),
        session.get("transitions") or "smooth",
        session.get("title_background") or "auto",
    )
    video_duration = await asyncio.to_thread(probe_duration, source_video)
    render_pages: list[str] = []
    render_floors: list[float] = []
    lead_in = not (session["steps"] and session["steps"][0].get("offscreen"))
    steps = session["steps"]
    for index, step in enumerate(steps):
        offset = step.get("offset_seconds")
        if offset is None:
            continue
        narration = (step.get("narration_text") or "").strip()
        clip: Path | None = None
        duration = 0.0
        if narration:
            voice = new_voice or step.get("voice") or session["voice"]
            # The recorded clip is reused while its line is unchanged: a
            # rerender to fix a camera move or a card no longer depends on the
            # TTS service being up, and is faster. A new voice speaks it again.
            stored = None if new_voice else step.get("narration_clip")
            if stored and (output_dir / stored).is_file():
                clip = output_dir / stored
            else:
                clip = await synthesize(
                    narration, voice, output_dir, session.get("provider", "edge")
                )
            duration = await asyncio.to_thread(probe_duration, clip)
            store.update_step_narration(
                session_id, index, narration, voice, narration_duration=duration,
                narration_clip=clip.name,
            )
            clips.append((offset, clip))
        else:
            duration = float(step.get("narration_duration") or 0.0)
        if step.get("offscreen"):
            continue
        render_steps.append((offset, clip, step_hold_seconds(step, duration)))
        render_pages.append(step.get("url") or "")
        render_floors.append(QUIET_FLOOR if step.get("quiet") else SEGMENT_FLOOR)
    if segmented_render_enabled():
        output_width = session.get("output_width")
        output_height = session.get("output_height")
        output_size = (
            (int(output_width), int(output_height))
            if output_width and output_height
            else None
        )
        result = await asyncio.to_thread(
            segmented_render, source_video, render_steps, video_path, output_size,
            render_config, Camera.load(output_dir), render_pages,
            render_floors, lead_in,
        )
        warnings = result.warnings
        duration = result.duration
    else:
        output_width = session.get("output_width")
        output_height = session.get("output_height")
        output_size = (
            (int(output_width), int(output_height))
            if output_width and output_height
            else None
        )
        await asyncio.to_thread(
            rerender_narration, source_video, clips, video_path, output_size,
            render_config, Camera.load(output_dir),
        )
        duration = await asyncio.to_thread(probe_duration, video_path)
    store.update_session_duration(session_id, duration)
    return {
        "ok": True,
        "duration": round(duration, 3),
        "video_url": session["video_url"],
        "warnings": warnings,
    }


@mcp.tool()
async def submit_backlog(
    title: str,
    detail: str = "",
    category: str = "feature",
    severity: str = "normal",
    session_id: str | None = None,
) -> dict:
    """Submit an agent tool-improvement request to the public backlog."""
    title = title.strip()
    if not title:
        raise ValueError("title must not be empty")
    category = category.strip().lower()
    if category not in {"feature", "bug", "friction"}:
        category = "feature"
    severity = severity.strip().lower()
    if severity not in {"low", "normal", "high"}:
        severity = "normal"
    return store.create_backlog(
        title,
        detail.strip(),
        category,
        severity,
        session_id,
    )


@mcp.tool()
async def list_backlog(limit: int = 50, status: str | None = None) -> list[dict]:
    """List agent backlog requests from durable metadata."""
    return store.list_backlog(limit, status.strip().lower() if status else None)


@mcp.tool()
async def update_backlog(
    id: str,
    status: str,
    note: str | None = None,
) -> dict:
    """Update a backlog item's roadmap status and resolution note."""
    item = store.update_backlog(id.strip(), status, note)
    if item is None:
        return {
            "ok": False,
            "error": {
                "type": "backlog_not_found",
                "message": f"Unknown backlog id: {id}",
            },
        }
    return item


def _extract_review_frames(session_id: str, video_path: Path, duration: float | None) -> list[dict]:
    """Extract stable representative JPEGs for human/editorial review."""
    if not video_path.is_file() or not duration or duration <= 0:
        return []
    review_dir = video_path.parent / "review_frames"
    review_dir.mkdir(parents=True, exist_ok=True)
    labels = (("opening", 0.05), ("transition", 0.35), ("focus", 0.65), ("ending", 0.92))
    frames: list[dict] = []
    for label, ratio in labels:
        timestamp = min(max(duration * ratio, 0.0), max(duration - 0.05, 0.0))
        frame_path = review_dir / f"{label}.jpg"
        command = [
            "ffmpeg", "-y", "-ss", f"{timestamp:.3f}", "-i", str(video_path),
            "-frames:v", "1", "-q:v", "3", str(frame_path),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if frame_path.is_file() and frame_path.stat().st_size > 0:
            frames.append({"label": label, "timestamp_seconds": round(timestamp, 3), "path": str(frame_path)})
    return frames


@mcp.tool()
async def review_session(session_id: str) -> dict:
    """Analyze a recording or finished session for secrets, alignment, pacing, and review frames."""
    session = store.get_session(session_id)
    if session is None:
        return {
            "ok": False,
            "error": {
                "type": "unknown_session",
                "message": f"Unknown session_id: {session_id}",
            },
        }
    
    findings: list[dict] = []
    
    # 1. Secret and Token Leak Scan
    secret_patterns = [
        (re.compile(r"([?&])(?:token|auth|key|secret|password|access_token)=([^&#\s]+)", re.IGNORECASE), "URL auth/token parameter"),
        (re.compile(r"\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\b"), "JWT token string"),
        (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36,}\b"), "GitHub token"),
        (re.compile(r"\b(?:sk|pk)_[a-zA-Z0-9]{20,}\b"), "API key"),
    ]
    
    # Check session metadata
    for field in ("start_url", "title", "subtitle", "cta_url"):
        val = str(session.get(field) or "")
        for pat, label in secret_patterns:
            if pat.search(val):
                findings.append({
                    "category": "security_secret_leak",
                    "severity": "high",
                    "location": f"session.{field}",
                    "message": f"Potential secret or token detected in {field} ({label})",
                })
                
    # Check steps
    steps = session.get("steps", [])
    total_narration_words = 0
    for step in steps:
        idx = step.get("idx", 0)
        for field in ("url", "title", "narration_text", "target"):
            val = str(step.get(field) or "")
            for pat, label in secret_patterns:
                if pat.search(val):
                    findings.append({
                        "category": "security_secret_leak",
                        "severity": "high",
                        "location": f"step[{idx}].{field}",
                        "message": f"Potential secret or token detected in step {idx+1} {field} ({label})",
                    })
        narration = (step.get("narration_text") or "").strip()
        if narration:
            total_narration_words += len(narration.split())
            
        # 2. Focus and Narration Alignment check
        action_type = step.get("action_type")
        target = step.get("target") or ""
        duration = step.get("narration_duration", 0.0) or 0.0
        if (action_type in {"click", "click_and_wait", "annotate"} and not narration
                and len(steps) > 1 and not step.get("quiet") and not step.get("offscreen")):
            findings.append({
                "category": "focus_narration_alignment",
                "severity": "low",
                "location": f"step[{idx}]",
                "message": f"Visual emphasis action '{action_type}' on '{target}' has no accompanying narration",
            })
            
    # 2b. Narration coverage. A take where most visible steps are silent
    # plays as a screen recording with a voice that comes and goes.
    # A silent step right after a narrated one belongs to that line — the
    # common shape is a caption step that carries the narration and the click
    # that follows it. Counting those as silent called a fully narrated take
    # "mostly silent" (2026-10-09).
    previous_narrated: set[int] = set()
    for position in range(1, len(steps)):
        if (steps[position - 1].get("narration_text") or "").strip():
            previous_narrated.add(position)
    visible = [step for position, step in enumerate(steps)
               if step.get("action_type") in SILENT_WARNING_TYPES
               and step.get("ok", True) and not step.get("quiet") and not step.get("offscreen")
               and position not in previous_narrated]
    silent = [step for step in visible if not (step.get("narration_text") or "").strip()]
    if len(visible) >= 4 and len(silent) * 2 > len(visible):
        findings.append({
            "category": "narration_coverage",
            "severity": "medium",
            "location": "session",
            "message": f"{len(silent)} of {len(visible)} visible steps have no narration; the video will be mostly silent",
        })

    # 2c. Camera. A minute of the same wide frame plays as a screen capture,
    # not a film; product videos change something every 10-20 seconds.
    live = sessions.get(session_id)
    camera = live.camera if live is not None else None
    video_path_value = session.get("video_path")
    if camera is None and isinstance(video_path_value, str) and video_path_value:
        camera = Camera.load(Path(video_path_value).parent)
    estimated = float(session.get("duration_seconds") or 0.0) or sum(
        float(step.get("narration_duration") or 0.0) for step in steps
    )
    if camera is not None and estimated >= 40 and not camera.moves():
        findings.append({
            "category": "camera_static",
            "severity": "low",
            "location": "session",
            "message": "The camera never moves: push in on the key detail of a beat with begin_shot(framing='close', focus_ref=...), then back to wide",
        })

    # 3. Ending Quality & Structure
    if not steps:
        findings.append({
            "category": "ending_quality",
            "severity": "high",
            "location": "session",
            "message": "Session has no recorded storyboard steps",
        })
    else:
        last_step = steps[-1]
        last_narration = (last_step.get("narration_text") or "").strip()
        last_duration = last_step.get("narration_duration", 0.0) or 0.0
        if last_step.get("action_type") in {"type", "scroll"} and not last_narration:
            findings.append({
                "category": "ending_quality",
                "severity": "medium",
                "location": f"step[{last_step.get('idx', 0)}]",
                "message": "Video finishes abruptly on a mechanical action without a closing hold or wrap-up narration",
            })
            
    # Pedagogical pacing score
    score = 100
    for f in findings:
        if f["severity"] == "high":
            score -= 35
        elif f["severity"] == "medium":
            score -= 15
        elif f["severity"] == "low":
            score -= 5
    score = max(0, score)
    video_path_value = session.get("video_path")
    video_path = Path(video_path_value) if isinstance(video_path_value, str) and video_path_value else None
    review_frames = (
        _extract_review_frames(session_id, video_path, session.get("duration_seconds"))
        if video_path is not None else []
    )
    
    return {
        "ok": True,
        "session_id": session_id,
        "director_score": score,
        "quality_status": "excellent" if score >= 85 else ("acceptable" if score >= 60 else "needs_revision"),
        "step_count": len(steps),
        "total_narration_words": total_narration_words,
        "review_frames": review_frames,
        "findings": findings,
    }


@mcp.tool()
async def finish(session_id: str) -> dict:
    """Stop recording and render the final MP4."""
    session = sessions.get(session_id)
    if session is None:
        stored = store.get_session(session_id)
        if stored is not None and stored.get("status") == "finished":
            return {
                "ok": False,
                "error": {
                    "type": "already_finished",
                    "message": f"Session already finished: {session_id}",
                },
            }
        return {
            "ok": False,
            "error": {
                "type": "unknown_session",
                "message": f"Unknown session_id: {session_id}",
            },
        }
    stored = store.get_session(session_id)
    steps = (stored or {}).get("steps") or []
    if not any(step.get("ok") for step in steps):
        # Rendering nothing is not a result, and neither is a take in which
        # every step failed: an invalid action is stored as a step too, so
        # counting steps alone let a session of nothing but errors render
        # (found verifying #25 live). Say so, and leave the session running so
        # it can still be recorded — or discarded on purpose.
        return {
            "ok": False,
            "error": {
                "type": "empty_session",
                "message": "No steps were recorded in this session." if not steps
                else f"None of the {len(steps)} recorded steps succeeded.",
            },
            "hint": "Record at least one successful act, or discard the session with delete_session(confirm=True, force=True).",
        }
    pending = pending_shots(stored)
    if pending:
        return {
            "ok": False,
            "error": {
                "type": "shot_review_required",
                "message": "Verify all storyboard shots before publishing: "
                + "; ".join(
                    f"{shot['shot_id']} is {shot.get('status')}"
                    + (f" ({shot['verification_note']})" if shot.get("verification_note") else "")
                    for shot in pending
                ),
                "pending_shots": [shot["shot_id"] for shot in pending],
            },
            "hint": "verify_shot(session_id, shot_id, verified=True) once you have checked the "
            "shot; an earlier shot is not checked against the page on screen now. "
            "begin_shot with the same shot_id replaces a shot.",
        }
    try:
        video_path = await session.finish()
        duration = await asyncio.to_thread(probe_duration, video_path)
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "error": {"type": "recording_missing", "message": str(exc)},
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": {"type": "finish_failed", "message": str(exc)},
        }
    public_base_url = os.environ.get("REEL_PUBLIC_BASE_URL", "").rstrip("/")
    video_url = (
        f"{public_base_url}/videos/{session_id}/video.mp4"
        if public_base_url
        else None
    )
    store.finish_session(
        session_id,
        str(video_path),
        video_url,
        duration,
    )
    # A finished MP4 is not automatically an editorially safe deliverable.
    # Run the durable review while the session metadata is still available and
    # return the report alongside the artifact so callers cannot silently skip
    # the quality/security gate.
    review = await review_session(session_id)
    try:
        await asyncio.to_thread(retention.prune_from_env)
    except Exception:
        pass
    sessions.pop(session_id, None)
    return {"video_path": str(video_path), "video_url": video_url, "review": review}


@mcp.tool()
async def prune(
    max_clips: int | None = None,
    keep_raw_clips: int | None = None,
) -> dict:
    """Prune finished session media using configured or supplied limits."""
    configured_max, configured_raw = retention.retention_settings()
    if max_clips is None:
        max_clips = configured_max
    if keep_raw_clips is None:
        keep_raw_clips = configured_raw
    return await asyncio.to_thread(
        retention.prune_storage, max_clips, keep_raw_clips
    )


async def perform_session_delete(session_id: str, force: bool = False) -> dict:
    """Abort any live runtime when forced, then remove media and metadata."""
    session_id = session_id.strip()
    live = sessions.get(session_id)
    if live is not None:
        if not force:
            return {
                "deleted": False,
                "session_id": session_id,
                "reason": "active_session_requires_force",
            }
        try:
            await live.abort()
        except Exception:
            pass
        sessions.pop(session_id, None)
    return await asyncio.to_thread(
        retention.delete_session_storage, session_id, force
    )


@mcp.tool()
async def delete_session(
    session_id: str, confirm: bool = False, force: bool = False
) -> dict:
    """Delete one finished session after explicit confirmation.

    With ``force=True`` a stale active session (for example one orphaned by
    a server restart) is also removed: any live browser runtime is aborted
    first, then media and metadata are deleted.
    """
    if not confirm:
        return {
            "deleted": False,
            "session_id": session_id,
            "reason": "confirmation_required",
        }
    return await perform_session_delete(session_id, force)


@mcp.custom_route(
    "/api/videos/{session_id}", methods=["DELETE"], include_in_schema=False
)
async def api_delete_video(request: Request) -> Response:
    """Delete one finished session over HTTP (used by the theater UI).

    Protected by the same bearer token as MCP. Requires ``confirm=true``;
    ``force=true`` additionally removes a stale active session.
    """
    session_id = request.path_params["session_id"]
    if not re.fullmatch(r"[0-9a-f]+", session_id):
        return JSONResponse(
            {"deleted": False, "session_id": session_id, "reason": "not_found"},
            status_code=404,
        )

    def flag(name: str) -> bool:
        return request.query_params.get(name, "").strip().lower() in {
            "1", "true", "yes",
        }

    if not flag("confirm"):
        return JSONResponse(
            {
                "deleted": False,
                "session_id": session_id,
                "reason": "confirmation_required",
            },
            status_code=400,
        )
    result = await perform_session_delete(session_id, flag("force"))
    if result.get("deleted"):
        return JSONResponse(result, status_code=200)
    status_map = {
        "not_found": 404,
        "active_session_requires_force": 409,
    }
    return JSONResponse(
        result, status_code=status_map.get(result.get("reason"), 400)
    )


@mcp.custom_route(
    "/videos/{session_id}/video.mp4",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
async def download_video(request: Request) -> Response:
    """Serve a finished video from the configured persistent output directory."""
    session_id = request.path_params["session_id"]
    if not re.fullmatch(r"[0-9a-f]+", session_id):
        return JSONResponse({"detail": "Video not found"}, status_code=404)
    video_path = output_root() / session_id / "video.mp4"
    if not video_path.is_file():
        return JSONResponse({"detail": "Video not found"}, status_code=404)
    return FileResponse(video_path, media_type="video/mp4")


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Require a configured bearer token for every HTTP request."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next) -> Response:
        public_video = re.fullmatch(r"/videos/[^/]+/video\.mp4", request.url.path)
        public_watch = re.fullmatch(r"/watch/[^/]+", request.url.path)
        if request.method in {"GET", "HEAD"} and (
            request.url.path in {
                "/",
                "/docs",
                "/favicon.svg",
                "/favicon.png",
                "/og.png",
                "/robots.txt",
                "/sitemap.xml",
                "/llms.txt",
                "/health",
                "/theater",
                "/api/videos",
                "/backlog",
                "/api/backlog",
                "/bug_report",
                "/api/bug_reports",
            }
            or public_video
            or public_watch
        ):
            return await call_next(request)
        authorization = request.headers.get("authorization", "")
        scheme, _, supplied_token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            supplied_token, self.token
        ):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)


def run_http() -> None:
    token = os.environ.get("REEL_API_TOKEN")
    if not token:
        raise RuntimeError(
            "REEL_API_TOKEN must be set when REEL_TRANSPORT=http; "
            "refusing to start without HTTP authentication"
        )
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    app = BearerAuthMiddleware(mcp.streamable_http_app(), token)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    transport = os.environ.get("REEL_TRANSPORT", "stdio").lower()
    if transport == "stdio":
        store.init_schema()
        mcp.run()
    elif transport == "http":
        run_http()
    else:
        raise RuntimeError(
            f"Unsupported REEL_TRANSPORT={transport!r}; use 'stdio' or 'http'"
        )
