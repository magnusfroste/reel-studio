"""Scripted stdio MCP smoke test (no LLM required)."""

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "video_director.server"],
        cwd=root,
    )
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            started = await client.call_tool("start_session", {
                "start_url": "https://example.com", "width": 1280, "height": 720,
            })
            session_id = json.loads(started.content[0].text)["session_id"]
            observed = await client.call_tool("observe", {"session_id": session_id})
            payload = json.loads(observed.content[0].text)
            link = next((e for e in payload["elements"] if e["role"] == "a"), None)
            if link:
                await client.call_tool("act", {
                    "session_id": session_id,
                    "action": {"type": "highlight", "ref": link["ref"]},
                    "narration": "This is a simple example web page.",
                })
                await client.call_tool("act", {
                    "session_id": session_id,
                    "action": {"type": "click", "ref": link["ref"]},
                    "narration": "Now we open the example link.",
                })
            else:
                await client.call_tool("act", {
                    "session_id": session_id,
                    "action": {"type": "wait", "ms": 1000},
                    "narration": "This is a simple example web page.",
                })
            finished = await client.call_tool("finish", {"session_id": session_id})
            print(json.loads(finished.content[0].text)["video_path"])


if __name__ == "__main__":
    asyncio.run(main())
