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
from starlette.responses import FileResponse, JSONResponse, Response
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
