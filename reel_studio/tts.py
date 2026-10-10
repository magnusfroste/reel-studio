"""Pluggable text-to-speech support."""

import asyncio
import json
import os
from pathlib import Path
import urllib.error
import urllib.request
import uuid

import edge_tts


class TTSProviderError(RuntimeError):
    """Raised when a configured TTS provider cannot synthesize audio."""


def normalize_provider(provider: str | None) -> str:
    selected = (provider or os.environ.get("REEL_TTS_PROVIDER", "edge")).strip().lower()
    if selected not in {"edge", "elevenlabs"}:
        raise TTSProviderError(f"Unknown TTS provider: {selected}")
    return selected


def validate_provider(provider: str) -> None:
    provider = normalize_provider(provider)
    if provider == "elevenlabs" and not os.environ.get("ELEVENLABS_API_KEY"):
        raise TTSProviderError(
            "ELEVENLABS_API_KEY must be set when TTS provider is elevenlabs"
        )


async def _synthesize_elevenlabs(text: str, voice: str, path: Path) -> Path:
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise TTSProviderError(
            "ELEVENLABS_API_KEY must be set when TTS provider is elevenlabs"
        )
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}"
    body = json.dumps({
        "text": text,
        "model_id": "eleven_multilingual_v2",
    }).encode()

    def request_audio() -> bytes:
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Accept": "audio/mpeg",
                "Content-Type": "application/json",
                "xi-api-key": api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise TTSProviderError(
                f"ElevenLabs request failed ({exc.code}): {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise TTSProviderError(f"ElevenLabs request failed: {exc}") from exc

    audio = await asyncio.to_thread(request_audio)
    path.write_bytes(audio)
    return path


async def synthesize(
    text: str,
    voice: str,
    output_dir: Path,
    provider: str | None = None,
) -> Path:
    """Synthesize text with the selected provider and return an MP3 path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"narration-{uuid.uuid4().hex}.mp3"
    selected = normalize_provider(provider)
    validate_provider(selected)
    if selected == "elevenlabs":
        await _synthesize_elevenlabs(text, voice, path)
    else:
        await _synthesize_edge(text, voice, path)
    return path


# Microsoft's free endpoint refuses a share of requests with 403 for minutes at
# a time from some hosts — it held up an agent's take for seven minutes and
# failed a rerender outright (2026-10-10); the next request often succeeds.
EDGE_ATTEMPTS = 5
EDGE_BACKOFF_SECONDS = (2, 4, 8, 16)


async def _synthesize_edge(text: str, voice: str, path: Path) -> None:
    last_error: Exception | None = None
    for attempt in range(EDGE_ATTEMPTS):
        try:
            communicator = edge_tts.Communicate(text, voice)
            await communicator.save(str(path))
            if path.is_file() and path.stat().st_size > 0:
                return
            last_error = TTSProviderError("Edge returned no audio")
        except Exception as exc:  # aiohttp's 403, a dropped socket, no audio
            last_error = exc
        path.unlink(missing_ok=True)
        if attempt < len(EDGE_BACKOFF_SECONDS):
            await asyncio.sleep(EDGE_BACKOFF_SECONDS[attempt])
    raise TTSProviderError(
        f"Edge TTS failed {EDGE_ATTEMPTS} times; the free endpoint is refusing "
        f"this host for now. Last error: {last_error}"
    )
