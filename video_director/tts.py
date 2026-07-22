"""Pluggable text-to-speech support."""

from pathlib import Path
import uuid

import edge_tts


async def synthesize(text: str, voice: str, output_dir: Path) -> Path:
    """Synthesize text with Edge TTS and return the generated MP3 path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"narration-{uuid.uuid4().hex}.mp3"
    communicator = edge_tts.Communicate(text, voice)
    await communicator.save(str(path))
    return path
