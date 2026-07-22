"""FastMCP entry point for Reel Studio."""

import hmac
import os
import re

from mcp.server.fastmcp import FastMCP
import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.types import ASGIApp

from .engine import BrowserSession, output_root
from .schema import Action


mcp = FastMCP("reel-studio")
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
    uvicorn.run(app, host=host, port=port, log_level="info")


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
