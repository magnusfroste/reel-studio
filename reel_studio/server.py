"""FastMCP entry point for Reel Studio."""

import hmac
import html
import json
import os
import re
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError
import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.types import ASGIApp

from .engine import BrowserSession, output_root
from . import store
from .render import probe_duration
from .schema import Action


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
"""


def page_shell(title: str, content: str) -> str:
    """Wrap public page content in the shared landing/docs layout."""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} · reel-studio</title>
  <style>{PAGE_STYLES}</style>
</head>
<body>
  <main>
    <nav><strong>reel-studio</strong><span><a href="/">Home</a> · <a href="/theater">Theater</a> · <a href="/docs">Docs</a></span></nav>
    {content}
  </main>
</body>
</html>"""


def mcp_endpoint() -> str:
    public_base_url = os.environ.get("REEL_PUBLIC_BASE_URL", "").rstrip("/")
    return f"{public_base_url}/mcp" if public_base_url else "/mcp"


def video_url(session_id: str) -> str:
    """Return the public relative URL for a finished session video."""
    return f"/videos/{session_id}/video.mp4"


def format_duration(duration: float | None) -> str:
    return f"{duration:.1f}s" if duration is not None else "Duration unavailable"


def video_card(session: dict) -> str:
    session_id = html.escape(session["id"], quote=True)
    title = html.escape(session["start_url"])
    duration = html.escape(format_duration(session.get("duration_seconds")))
    finished_at = html.escape(session.get("finished_at") or "Recently finished")
    return f"""
    <article class="video-card card" data-session-id="{session_id}">
      <video controls preload="metadata" src="{video_url(session_id)}"></video>
      <div class="video-card-body">
        <h3>{title}</h3>
        <p class="muted">{duration} · {finished_at}</p>
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
        heading.textContent = item.start_url;
        const meta = document.createElement("p");
        meta.className = "muted";
        meta.textContent = `${{item.duration_seconds == null ? "Duration unavailable" : item.duration_seconds.toFixed(1) + "s"}} · ${{item.finished_at || "Recently finished"}}`;
        body.append(heading, meta);
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


def landing_page() -> str:
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
    return page_shell("Autonomous product demos", content)


def theater_page() -> str:
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
    </section>
    <div id="theater-videos" class="theater-grid">
      {cards}
    </div>
    {video_refresh_script("theater-videos")}
    """
    return page_shell("Public video theater", content)


def docs_page() -> str:
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
    <div class="tool card"><h3><code>start_session(start_url, width, height, voice)</code></h3>
      <p>Launch headed Chromium and begin recording.</p>
      <p><strong>Returns:</strong> <code>{{"session_id"}}</code>. Width defaults to
      1280, height to 720, and voice to <code>en-US-JennyNeural</code>.</p></div>
    <div class="tool card"><h3><code>observe(session_id)</code></h3>
      <p>Capture the current screen and discover interactive elements.</p>
      <p><strong>Returns:</strong> <code>{{"screenshot_path", "url", "title",
      "elements":[], "refs_stale": false}}</code> plus a viewable image.
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
      <code>type{{ref,text}}</code>, <code>scroll{{dy}}</code>,
      <code>hover{{ref}}</code>, <code>highlight{{ref}}</code>, and
      <code>wait{{ms}}</code>. Refs come from <code>observe</code>.</p></div>
    <div class="tool card"><h3><code>get_status(session_id)</code></h3>
      <p>Returns elapsed seconds, recorded step count, total narrated seconds,
      and estimated final video length.</p></div>
    <div class="tool card"><h3><code>list_sessions(limit=20)</code></h3>
      <p>Lists recent sessions from durable SQLite metadata, including status,
      step count, duration, and video URL.</p></div>
    <div class="tool card"><h3><code>get_session(session_id)</code></h3>
      <p>Returns the stored session row and ordered storyboard steps. Finished
      metadata remains available after a server restart; abandoned active
      sessions are reported as stale and are not resumed.</p></div>
    <div class="tool card"><h3><code>finish(session_id)</code></h3>
      <p>Stop recording, mix narration, and render the final MP4.</p>
      <p><strong>Returns:</strong> <code>{{"video_path", "video_url"}}</code>.
      The URL is present when <code>REEL_PUBLIC_BASE_URL</code> is configured.</p></div>
    <h2>The storyboard workflow</h2>
    <p>Give the agent a product story, then let it loop: call
    <code>observe</code>, choose one useful next step, call <code>act</code>
    with a concise narration, and repeat. Use the returned refs to target
    controls precisely. When the story lands, call <code>finish</code> to get
    the MP4 and its download URL.</p>
    <h2>Auth and video delivery</h2>
    <p>MCP requests use
    <code>Authorization: Bearer &lt;REEL_API_TOKEN&gt;</code>. The token is
    configured with the server's <code>REEL_API_TOKEN</code> environment
    variable. Finished videos are public by default: browse
    <a href="/theater">/theater</a>, query <code>/api/videos</code>, or play
    the relative <code>video_url</code> directly without a token.</p>
    <p><a href="/">← Back to the reel-studio overview</a></p>
    """
    return page_shell("MCP and API docs", content)


@mcp.custom_route("/", methods=["GET"], include_in_schema=False)
async def home(request: Request) -> Response:
    """Serve the public marketing landing page."""
    return HTMLResponse(landing_page())


@mcp.custom_route("/docs", methods=["GET"], include_in_schema=False)
async def docs(request: Request) -> Response:
    """Serve the public MCP and API documentation."""
    return HTMLResponse(docs_page())


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(request: Request) -> Response:
    """Return a lightweight service health response."""
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/theater", methods=["GET"], include_in_schema=False)
async def theater(request: Request) -> Response:
    """Serve the public finished-video showcase."""
    return HTMLResponse(theater_page())


@mcp.custom_route("/api/videos", methods=["GET"], include_in_schema=False)
async def videos_api(request: Request) -> Response:
    """Return finished videos for public theater refreshes."""
    return JSONResponse(
        [
            {
                "id": video["id"],
                "start_url": video["start_url"],
                "title": video["start_url"],
                "duration_seconds": video["duration_seconds"],
                "finished_at": video["finished_at"],
            }
            for video in store.list_finished_sessions()
        ]
    )


@mcp.tool()
async def start_session(
    start_url: str, width: int = 1280, height: int = 720,
    voice: str = "en-US-JennyNeural",
) -> dict:
    """Launch a headed browser and begin recording."""
    session = await BrowserSession.create(start_url, width, height, voice)
    sessions[session.session_id] = session
    store.create_session(
        session.session_id,
        start_url,
        voice,
        width,
        height,
        str(session.directory),
    )
    return {"session_id": session.session_id}


@mcp.tool()
async def observe(session_id: str) -> CallToolResult:
    """Capture the current browser UI and its interactive elements."""
    payload, screenshot = await sessions[session_id].observe()
    return feedback_result(payload, screenshot)


@mcp.tool()
async def act(session_id: str, action: dict, narration: str = "") -> CallToolResult:
    """Perform exactly one browser action, optionally narrating it."""
    session = sessions[session_id]
    try:
        parsed_action = Action.model_validate(action)
    except ValidationError as exc:
        payload, screenshot = await session.error_result("invalid_action", str(exc))
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
        )
        return feedback_result(payload, screenshot)
    payload, screenshot = await session.act(parsed_action, narration)
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
    )
    return feedback_result(payload, screenshot)


@mcp.tool()
async def get_status(session_id: str) -> dict:
    """Return recording progress and an estimated final video length."""
    session = sessions.get(session_id)
    if session is not None:
        return session.status()
    status = store.get_status(session_id)
    if status is None:
        raise KeyError(f"Unknown session_id: {session_id}")
    return status


@mcp.tool()
async def list_sessions(limit: int = 20) -> list[dict]:
    """List recent recording sessions from durable metadata."""
    return store.list_sessions(limit)


@mcp.tool()
async def get_session(session_id: str) -> dict:
    """Return a durable session and its ordered storyboard steps."""
    session = store.get_session(session_id)
    if session is None:
        raise KeyError(f"Unknown session_id: {session_id}")
    return session


@mcp.tool()
async def finish(session_id: str) -> dict:
    """Stop recording and render the final MP4."""
    session = sessions.pop(session_id)
    video_path = await session.finish()
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
        probe_duration(video_path),
    )
    return {"video_path": str(video_path), "video_url": video_url}


@mcp.custom_route("/videos/{session_id}/video.mp4", methods=["GET"], include_in_schema=False)
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
        if request.method == "GET" and (
            request.url.path in {"/", "/docs", "/health", "/theater", "/api/videos"}
            or public_video
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
