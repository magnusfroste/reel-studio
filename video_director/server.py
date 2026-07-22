"""FastMCP entry point for Video Director."""

from mcp.server.fastmcp import FastMCP

from .engine import BrowserSession
from .schema import Action


mcp = FastMCP("video-director")
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
    return {"video_path": str(await session.finish())}


if __name__ == "__main__":
    mcp.run()
