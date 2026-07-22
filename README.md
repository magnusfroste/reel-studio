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

The server uses MCP stdio by default. For HTTP mode:

```bash
REEL_TRANSPORT=http \
REEL_API_TOKEN=replace-with-a-long-random-token \
REEL_PUBLIC_BASE_URL=https://reel.example.com \
.venv/bin/python -m reel_studio.server
```

HTTP serves MCP at `/mcp` and finished videos at
`/videos/{session_id}/video.mp4`. Every HTTP request requires
`Authorization: Bearer <REEL_API_TOKEN>`. `REEL_OUTPUT_DIR` defaults to
`/home/ubuntu/.video-director/sessions`; set it to a persistent volume path
such as `/data` in a container. Example Claude Desktop / MCP config for local
stdio:

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

## Deploy on EasyPanel/Hetzner

Build the image with `docker compose build`, then configure
`REEL_API_TOKEN` and `REEL_PUBLIC_BASE_URL` in EasyPanel. Mount a persistent
volume at `/data` (for example, the shared
`/etc/easypanel/projects/reel-studio/data` host path) and expose container port
`8000`. Point a domain at the service; EasyPanel/Traefik handles TLS. The
included `docker-compose.yml` documents these settings.

For a remote MCP client, connect to
`https://reel.example.com/mcp` using the streamable HTTP transport and send
`Authorization: Bearer <REEL_API_TOKEN>` on each request. `finish` returns both
the local `video_path` and a downloadable `video_url`.

## Auto-merge

Label a pull request `auto-merge` and it will be merged automatically once the
workflow's package import and compile checks pass.
