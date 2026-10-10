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


def test_a_time_lapse_keeps_all_its_footage_sped_up():
    steps = [(0.0, None, 0.0), (1.0, None, 0.0), (21.0, None, 2.0)]
    plain, _ = render.plan_segments(steps, 25.0, lead_in=False)
    fast, _ = render.plan_segments(steps, 25.0, lead_in=False, speeds=[1, 8, 1])
    # As recorded, the wait is cut to the step's floor; as a time-lapse all
    # twenty seconds are shown, eight times faster.
    assert plain[1].source_duration < 20
    assert fast[1].source_duration == pytest.approx(20.0)
    assert fast[1].output_duration == pytest.approx(2.5)
    assert fast[1].speed == 8
    # A line longer than the sped-up footage still gets its time.
    narrated, _ = render.plan_segments([(0.0, None, 6.0), (4.0, None, 0.0)], 8.0, lead_in=False, speeds=[4, 1])
    assert narrated[0].output_duration >= 6.0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
def test_a_time_lapse_segment_lasts_its_planned_length(tmp_path):
    source = _clip(tmp_path / "s.mp4", "teal", 8.0)
    out = render._render_video_segment(source, 0.0, 8.0, 2.0, tmp_path / "seg.mp4", speed=4.0)
    assert render.probe_duration(out) == pytest.approx(2.0, abs=0.12)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
def test_the_closing_card_carries_a_qr_code_of_the_link(tmp_path):
    pytest.importorskip("segno")
    qr = render.make_qr("https://github.com/magnusfroste/agenthotel", tmp_path / "qr.png")
    assert qr is not None and qr.is_file()
    plain = tmp_path / "plain.mp4"
    with_qr = tmp_path / "qr.mp4"
    config = render.RenderConfig(cta_url="https://example.com", closing_qr=False)
    render._closing_card(plain, 640, 360, config, None, tmp_path)
    render._closing_card(with_qr, 640, 360, render.RenderConfig(cta_url="https://example.com"), None, tmp_path)
    assert render.probe_duration(with_qr) == pytest.approx(render.CARD_DURATION, abs=0.1)

    def corner(path):
        # Mean luma of the bottom-right corner, where the code sits.
        data = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-ss", "1", "-i", str(path), "-frames:v", "1",
             "-vf", "crop=60:60:640-100:360-100,format=gray", "-f", "rawvideo", "-"],
            capture_output=True, check=True).stdout
        return sum(data) / len(data)

    assert corner(with_qr) > corner(plain) + 40  # white quiet zone on a dark card
