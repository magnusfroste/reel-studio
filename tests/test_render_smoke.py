import shutil
import subprocess
from pathlib import Path

import pytest

from reel_studio import render


def _run_ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)


def _has_stream(path, stream_type: str) -> bool:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", f"{stream_type}:0",
            "-show_entries", "stream=codec_type",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def _video_codec(path) -> str:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _audio_codec(path) -> str:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def media_dir(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required for render smoke tests")
    if not Path(render.FONT_PATH).is_file():
        pytest.skip(f"title-card font is missing: {render.FONT_PATH}")
    source = tmp_path / "input.mp4"
    clip_one = tmp_path / "clip-one.m4a"
    clip_two = tmp_path / "clip-two.m4a"
    _run_ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:d=6",
        "-pix_fmt", "yuv420p", "-c:v", "libx264", str(source),
    )
    _run_ffmpeg(
        "-f", "lavfi", "-i", "sine=frequency=300:duration=1.2",
        "-c:a", "aac", str(clip_one),
    )
    _run_ffmpeg(
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1.0",
        "-c:a", "aac", str(clip_two),
    )
    return source, clip_one, clip_two


def _assert_playable(path) -> None:
    assert path.is_file()
    assert _video_codec(path) == "h264"
    assert _has_stream(path, "a")
    assert _audio_codec(path) == "aac"
    assert render.probe_duration(path) > 0


def test_render_paths(media_dir, tmp_path):
    source, clip_one, clip_two = media_dir
    steps = [(0.5, clip_one, 1.2), (3.0, clip_two, 1.0)]

    segmented = tmp_path / "segmented.mp4"
    body = render.segmented_render(source, steps, segmented)
    _assert_playable(segmented)
    assert body.duration > 0
    assert render.probe_video_size(segmented) == (640, 360)

    continuous = tmp_path / "continuous.mp4"
    render.mux_narration(
        source,
        [(offset, clip) for offset, clip, _ in steps],
        continuous,
    )
    _assert_playable(continuous)

    carded = tmp_path / "carded.mp4"
    carded_result = render.segmented_render(
        source,
        steps,
        carded,
        config=render.RenderConfig(
            title="Demo",
            subtitle="Sub",
            accent="#123456",
            cta_url="https://example.com",
            cta_text="Learn more",
            music="subtle",
        ),
    )
    _assert_playable(carded)
    assert carded_result.duration >= body.duration + 5.0
    assert render.probe_video_size(carded) == (640, 360)

    no_cards = tmp_path / "no-cards.mp4"
    render.segmented_render(source, steps, no_cards, config=render.RenderConfig())
    _assert_playable(no_cards)

    rerendered = tmp_path / "rerendered.mp4"
    render.rerender_narration(
        source,
        [(offset, clip) for offset, clip, _ in steps],
        rerendered,
    )
    _assert_playable(rerendered)
