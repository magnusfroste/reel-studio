# reel-studio

`reel-studio` is an MCP server that lets an AI agent direct a narrated
screen recording of a web app. It launches headed Chromium on Xvfb, records
the display with ffmpeg/x11grab, and renders delayed narration into the final
MP4. Edge TTS is the free default; ElevenLabs is an optional premium backend.

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
# Optional comma-separated extra hostnames:
# REEL_ALLOWED_HOSTS=internal.example.com,another.example.com \
.venv/bin/python -m reel_studio.server
```

HTTP serves a public marketing landing page at `/`, detailed API docs at
`/docs`, a public video theater at `/theater`, the theater feed at
`/api/videos`, a public agent backlog at `/backlog` with JSON feed at
`/api/backlog`, public bug reports at `/bug_report` with JSON feed at
`/api/bug_reports`, a public JSON health check at `/health`, MCP at `/mcp`, and
finished videos at `/videos/{session_id}/video.mp4`. MCP requests require
`Authorization: Bearer <REEL_API_TOKEN>`; finished videos are public by
default. `REEL_OUTPUT_DIR` defaults to
`/home/ubuntu/.video-director/sessions`; set it to a persistent volume path
such as `/data` in a container. The public host is automatically allowed from
`REEL_PUBLIC_BASE_URL`; use optional comma-separated `REEL_ALLOWED_HOSTS` for
additional deployment hostnames. Example Claude Desktop / MCP config for local
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

Tools are `start_session`, `observe`, `act`, `finish`, and `get_status`.
`start_session` captures at 1920x1080 by default; `width` and `height` remain
overridable. Set optional `output_size` (for example `1280x720`) to downscale
only the final MP4 while retaining the larger capture viewport, or set
`REEL_OUTPUT_SIZE` as the default. Without either setting, output matches the
capture resolution.
`start_session` accepts an optional `provider` (`edge` or `elevenlabs`) and
defaults to `edge`. Set `REEL_TTS_PROVIDER=elevenlabs` to make ElevenLabs the
default provider, or select it per session. ElevenLabs requires the
`ELEVENLABS_API_KEY` environment variable, and the `voice` value is passed as
the ElevenLabs voice ID. No API key is needed for the default Edge provider.
`list_sessions` and `get_session` read durable SQLite metadata, including
finished sessions and their storyboard steps after a restart.
The token-gated `submit_backlog`, `list_backlog`, and `update_backlog` tools
manage the public roadmap. Backlog statuses are `open`, `planned`,
`in_progress`, `shipped`, and `wont_fix`; updates can include a resolution
note. Public backlog and bug-report feeds expose the status, note, and update
timestamp.
For a finished session, `update_step_narration` edits one storyboard
narration and `rerender` rebuilds only the audio track onto the existing
recorded video without re-recording the browser. Overlapping narration is
reported as warnings; audio that exceeds the original timeline extends the
last video frame.
`observe` and `act` return structured feedback plus a viewable screenshot.
`observe` also includes truncated visible `page_text` so agents can verify
plain page content such as table rows without reading the image.
`act` accepts `goto`, `click`, `type`, `scroll`, `hover`, `highlight`,
`scroll_to_text`, and `wait` actions. `scroll_to_text` finds visible text,
scrolls it into view, and returns its bounding box. Use `assert_visible` to
check text without adding a storyboard step or changing the recording.
Re-observe after navigation to refresh element refs;
same-page DOM re-renders are handled by re-queryable locators. The scripted
`examples/demo_client.py` runs a complete smoke test against
`https://example.com`.

## Deploy on EasyPanel/Hetzner

Build the image with `docker compose build`, then configure
`REEL_API_TOKEN`, `REEL_PUBLIC_BASE_URL`, and optionally `REEL_DB_PATH` in
EasyPanel. `REEL_DB_PATH` defaults to a SQLite file under the configured output
root, so it persists when `/data` is mounted. Mount a persistent
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
