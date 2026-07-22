"""FastMCP entry point for Reel Studio."""

import hmac
import os
import re
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.types import ASGIApp

from .engine import BrowserSession, output_root
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


def landing_page() -> str:
    """Render the public HTTP landing page without exposing credentials."""
    public_base_url = os.environ.get("REEL_PUBLIC_BASE_URL", "").rstrip("/")
    mcp_endpoint = f"{public_base_url}/mcp" if public_base_url else "/mcp"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>reel-studio · narrated web app videos</title>
  <style>
    :root {{ color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
    body {{ margin: 0; background: #10131a; color: #edf1f7; }}
    main {{ max-width: 860px; margin: 0 auto; padding: 64px 24px 80px; }}
    .eyebrow {{ color: #8ea7ff; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }}
    h1 {{ font-size: clamp(2.5rem, 8vw, 5rem); line-height: .98; margin: 16px 0 24px; }}
    h2 {{ margin-top: 48px; color: #cbd5ff; }}
    p, li {{ color: #b9c1d2; font-size: 1.05rem; line-height: 1.65; }}
    code, pre {{ background: #1b2130; border: 1px solid #303a52; border-radius: 10px; }}
    code {{ padding: 2px 6px; color: #d5ddff; }}
    pre {{ overflow-x: auto; padding: 18px; color: #d5ddff; }}
    a {{ color: #9eb1ff; }}
    .tools {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; padding: 0; list-style: none; }}
    .tools li {{ background: #171c27; border: 1px solid #2a3348; border-radius: 12px; padding: 16px; }}
    .tools strong {{ display: block; color: #edf1f7; margin-bottom: 6px; }}
    .endpoint {{ border-left: 3px solid #7188ff; padding: 12px 16px; background: #171c27; }}
  </style>
</head>
<body>
  <main>
    <div class="eyebrow">reel-studio</div>
    <h1>Direct narrated videos of any web app.</h1>
    <p>reel-studio is an MCP server that lets an AI agent observe a web app,
    perform browser actions one at a time, narrate each step, and render the
    result as a downloadable screen-recording video.</p>

    <h2>How it works</h2>
    <p>The agent starts a headed Chromium session, observes the current UI,
    directs individual actions with optional narration, then finishes to get
    an MP4 with the narration mixed into the recording.</p>

    <h2>MCP tools</h2>
    <ul class="tools">
      <li><strong>start_session</strong>Launch a browser and begin recording.</li>
      <li><strong>observe</strong>Capture a screenshot and interactive element refs.</li>
      <li><strong>act</strong>Perform one browser action with optional narration.</li>
      <li><strong>finish</strong>Render the MP4 and return its path and URL.</li>
    </ul>

    <h2>Connect from Claude Code</h2>
    <p class="endpoint">MCP endpoint: <code>{mcp_endpoint}</code></p>
    <!-- Example placeholder: Bearer <YOUR_TOKEN> -->
    <pre><code>claude mcp add --transport http reel-studio {mcp_endpoint} \
  --header "Authorization: Bearer &lt;YOUR_TOKEN&gt;"</code></pre>
    <p>The placeholder is the server's <code>REEL_API_TOKEN</code> environment
    variable. Never commit or share the real token.</p>
    <p><a href="https://github.com/magnusfroste/reel-studio">View reel-studio on GitHub</a></p>
  </main>
</body>
</html>"""


@mcp.custom_route("/", methods=["GET"], include_in_schema=False)
async def home(request: Request) -> Response:
    """Serve the public documentation landing page."""
    return HTMLResponse(landing_page())


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(request: Request) -> Response:
    """Return a lightweight service health response."""
    return JSONResponse({"status": "ok"})


@mcp.tool()
async def start_session(
    start_url: str, width: int = 1280, height: int = 720,
    voice: str = "en-US-JennyNeural",
) -> dict:
    """Launch a headed browser and begin recording."""
    session = await BrowserSession.create(start_url, width, height, voice)
    sessions[session.session_id] = session
    return {"session_id": session.session_id}


@mcp.tool()
async def observe(session_id: str) -> dict:
    """Capture the current browser UI and its interactive elements."""
    return await sessions[session_id].observe()


@mcp.tool()
async def act(session_id: str, action: dict, narration: str = "") -> dict:
    """Perform exactly one browser action, optionally narrating it."""
    return await sessions[session_id].act(Action.model_validate(action), narration)


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
    return FileResponse(video_path, media_type="video/mp4", filename="video.mp4")


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Require a configured bearer token for every HTTP request."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method == "GET" and request.url.path in {"/", "/health"}:
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
        mcp.run()
    elif transport == "http":
        run_http()
    else:
        raise RuntimeError(
            f"Unsupported REEL_TRANSPORT={transport!r}; use 'stdio' or 'http'"
        )
