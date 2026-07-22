# reel-studio

`reel-studio` is an MCP server that lets an AI agent direct a narrated
screen recording of a web app. It launches headed Chromium on Xvfb, records
the display with ffmpeg/x11grab, and renders delayed Edge TTS narration into
the final MP4.

## Setup

```bash
python3.10 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/playwright install chromium
```

System packages `Xvfb` and `ffmpeg` are required.

## Run

```bash
.venv/bin/python -m reel_studio.server
```

The server uses MCP stdio. Example Claude Desktop / MCP config:

```json
{
  "mcpServers": {
    "reel-studio": {
      "command": "/home/ubuntu/repos/video-director/.venv/bin/python",
      "args": ["-m", "reel_studio.server"],
      "cwd": "/home/ubuntu/repos/video-director"
    }
  }
}
```

Tools are `start_session`, `observe`, `act`, and `finish`. `act` accepts
`goto`, `click`, `type`, `scroll`, `hover`, `highlight`, and `wait` actions.
The scripted `examples/demo_client.py` runs a complete smoke test against
`https://example.com`.
