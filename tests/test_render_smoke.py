import shutil
import subprocess
from pathlib import Path

import pytest

from reel_studio import render


def test_card_text_wraps_long_titles_to_readable_lines():
    lines = render.wrap_card_text(
        "FlowWink: See the Signal. Move the Business.", max_chars=28
    )
    assert lines == ["FlowWink: See the Signal.", "Move the Business."]
    assert all(len(line) <= 28 for line in lines)


def test_card_text_keeps_short_text_on_one_line():
    assert render.wrap_card_text("Short title", max_chars=28) == ["Short title"]




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


def _clip(path, colour, seconds=1.0, size="320x176"):
    _run_ffmpeg("-f", "lavfi", "-i", f"color=c={colour}:s={size}:r=25:d={seconds}",
                "-pix_fmt", "yuv420p", str(path))
    return path


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
def test_transitions_cut_on_a_new_page_and_dissolve_a_jump(tmp_path):
    a = _clip(tmp_path / "a.mp4", "black")
    b = _clip(tmp_path / "b.mp4", "black")
    c = _clip(tmp_path / "c.mp4", "white")
    d = _clip(tmp_path / "d.mp4", "gray")
    pages = ["https://x/1", "https://x/1", "https://x/1", "https://x/2"]
    choices = render.choose_transitions([a, b, c, d], pages, tmp_path)
    assert choices[0] == 0.0  # nothing visible changed: a cut
    assert choices[1] == render.TIME_DISSOLVE  # black to white on one page
    assert choices[2] == 0.0  # a new page: a cut, however different


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
def test_smooth_assembly_overlaps_and_keeps_the_narration(tmp_path):
    a = _clip(tmp_path / "a.mp4", "black", 2.0)
    b = _clip(tmp_path / "b.mp4", "white", 2.0)
    voice = tmp_path / "voice.m4a"
    _run_ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(voice))
    out = tmp_path / "out.mp4"
    config = render.RenderConfig(title="T", cta_url="https://example.com")
    render._assemble_smooth([a, b], ["p", "p"], [None, voice], out, (320, 176), config, tmp_path)
    expected = (render.CARD_DURATION + 2 + 2 + render.CARD_DURATION
                - render.CARD_FADE - render.TIME_DISSOLVE - render.CARD_FADE)
    assert render.probe_duration(out) == pytest.approx(expected, abs=0.15)
    assert _has_stream(out, "a")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
def test_a_hero_card_is_built_on_a_still_from_the_video(tmp_path):
    video = _clip(tmp_path / "v.mp4", "teal", 1.0, "640x360")
    config = render.RenderConfig(title="Hero", cta_url="https://example.com")
    first, last = render.card_backgrounds(config, video, video, tmp_path)
    assert first is not None and last is not None
    card = tmp_path / "card.mp4"
    render._card(card, 640, 360, "#3b82f6", [("Hero", 36)], first)
    assert render.probe_duration(card) == pytest.approx(render.CARD_DURATION, abs=0.1)
    assert render.probe_video_size(card) == (640, 360)
    solid = render.RenderConfig(title="Hero", title_background="solid")
    assert render.card_backgrounds(solid, video, video, tmp_path) == (None, None)


def test_only_public_https_images_are_fetched():
    assert not render._public_https("http://example.com/a.png")
    assert not render._public_https("https://127.0.0.1/a.png")
    assert not render._public_https("https://localhost/a.png")
    assert not render._public_https("file:///etc/passwd")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
@pytest.mark.parametrize("transitions", ["smooth", "cuts"])
def test_the_render_records_where_each_part_landed(tmp_path, transitions):
    source = _clip(tmp_path / "screen.mp4", "teal", 6.0)
    config = render.RenderConfig(title="T", cta_url="https://example.com", transitions=transitions,
                                 title_background="solid")
    out = tmp_path / "video.mp4"
    result = render.segmented_render(source, [(1.0, None, 1.5), (3.0, None, 1.0)], out, (320, 176), config)
    kinds = [p["kind"] for p in result.timeline]
    assert kinds == ["intro", "lead", "step", "step", "outro"]
    assert [p["step"] for p in result.timeline if p["kind"] == "step"] == [0, 1]
    starts = [p["start"] for p in result.timeline]
    assert starts == sorted(starts) and result.timeline[-1]["start"] < result.duration
    parts = render.write_storyboard(tmp_path, out, result.timeline)
    assert all((tmp_path / "storyboard" / p["frame"]).is_file() for p in parts)
    assert render.read_storyboard(tmp_path) == parts
