import shutil
import subprocess

import pytest

from reel_studio import render
from reel_studio.camera import EASE_SECONDS, MAX_ZOOM, Camera, capture_scale


def test_capture_scale_picks_whole_even_frames(monkeypatch):
    monkeypatch.delenv("REEL_CAPTURE_SCALE", raising=False)
    assert capture_scale(1920, 1080) == pytest.approx(4 / 3)
    # 1280 * 4/3 is not whole; 1.5 gives 1920x1080.
    assert capture_scale(1280, 720) == 1.5
    assert capture_scale(1080, 1350) == pytest.approx(4 / 3)
    # Already as large as a 2-core host records smoothly.
    assert capture_scale(2560, 1440) == 1.0


def test_capture_scale_can_be_configured(monkeypatch):
    monkeypatch.setenv("REEL_CAPTURE_SCALE", "1")
    assert capture_scale(1920, 1080) == 1.0
    monkeypatch.setenv("REEL_CAPTURE_SCALE", "2")
    assert capture_scale(1280, 720) == 2.0
    assert capture_scale(1920, 1080) == 1.0  # 3840x2160 is over the cap


def test_output_size_defaults_to_css_size_only_when_scaled():
    assert Camera(1920, 1080, 4 / 3).output_size(None) == (1920, 1080)
    assert Camera(1920, 1080, 4 / 3).output_size((1280, 720)) == (1280, 720)
    assert Camera(1920, 1080, 1.0).output_size(None) is None
    assert Camera(1920, 1080, 4 / 3).physical_size == (2560, 1440)


def test_moves_build_a_keyframe_track():
    camera = Camera(1920, 1080, 4 / 3)
    assert not camera.move(1.0, 1.0)  # already wide
    assert camera.move(2.0, 2.0, 1800, 100)
    zoom, cx, cy = camera.final_state()
    assert (zoom, cx, cy) == (2.0, 1800, 100)
    view = camera.view()
    # Clamped to the page, like zoompan: the frame cannot leave the recording.
    assert view == {"x": 960.0, "y": 0.0, "w": 960.0, "h": 540.0, "zoom": 2.0}
    assert not camera.move(3.0, 2.0, 1800, 100)
    assert camera.move(5.0, 1.0, 10, 10)
    assert camera.final_state() == (1.0, 960.0, 540.0)
    assert camera.state_at(4.0)[0] == 2.0
    assert camera.moves()


def test_zoom_is_clamped_and_same_instant_replaces():
    camera = Camera(1920, 1080)
    camera.move(1.0, 9.0, 960, 540)
    assert camera.final_state()[0] == MAX_ZOOM
    camera.move(1.0, 1.5, 960, 540)
    assert len(camera.keys) == 1
    assert camera.keys[0]["zoom"] == 1.5


def test_follow_pans_only_to_targets_outside_the_frame():
    camera = Camera(1920, 1080)
    assert camera.needs_follow({"x": 0, "y": 0, "width": 10, "height": 10}) is None
    camera.move(0.0, 2.0, 480, 270)  # top-left quarter
    inside = {"x": 400, "y": 250, "width": 100, "height": 40}
    assert camera.needs_follow(inside) is None
    outside = {"x": 1500, "y": 900, "width": 100, "height": 40}
    assert camera.needs_follow(outside) == (1550, 920)
    # Half in frame is not in frame.
    straddling = {"x": 900, "y": 250, "width": 120, "height": 40}
    assert camera.needs_follow(straddling) == (960, 270)


def test_a_wide_target_is_framed_from_its_start():
    camera = Camera(1920, 1080)
    # The test result panel: full width, its words at the left.
    panel = {"x": 330, "y": 640, "width": 1530, "height": 80}
    cx, cy = camera.aim(panel, 2.0)
    view_w = 1920 / 2.0
    assert cx - view_w / 2 < 330 < cx - view_w / 2 + view_w * 0.1  # first words in frame
    assert cy == 680
    # A target that fits is centred.
    assert camera.aim({"x": 100, "y": 100, "width": 200, "height": 50}, 2.0) == (200, 125)


def test_zoompan_is_skipped_without_moves_and_scoped_to_its_segment():
    camera = Camera(1920, 1080, 4 / 3)
    assert camera.zoompan(0.0, 5.0, (1920, 1080)) is None
    camera.move(10.0, 2.0, 1440, 270)
    camera.move(20.0, 1.0)
    # Before the push-in, and well after it pulled back: plain scale.
    assert camera.zoompan(0.0, 9.0, (1920, 1080)) is None
    assert camera.zoompan(30.0, 35.0, (1920, 1080)) is None
    # A segment that starts mid-push-in still eases it.
    mid = camera.zoompan(10.5, 12.0, (1920, 1080))
    assert mid is not None and "10.000" in mid and "s=1920x1080" in mid
    # Held at 2x: a constant zoom, no key of its own.
    held = camera.zoompan(15.0, 18.0, (1920, 1080))
    assert held is not None and "it-" not in held and "2.0000" in held
    # The pull-back at 20 s belongs to the next segment, not this one's padding.
    assert "20.000" not in camera.zoompan(15.0, 20.0, (1920, 1080))
    assert "20.000" in camera.zoompan(20.0, 22.0, (1920, 1080))


def test_camera_round_trips_through_its_file(tmp_path):
    camera = Camera(1920, 1080, 4 / 3)
    camera.move(1.0, 1.5, 300, 200)
    camera.save(tmp_path)
    loaded = Camera.load(tmp_path)
    assert loaded is not None
    assert loaded.keys == camera.keys and loaded.scale == camera.scale
    assert Camera.load(tmp_path / "missing") is None
    (tmp_path / "camera.json").write_text("{not json")
    assert Camera.load(tmp_path) is None


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
def test_segment_keeps_its_padding_and_renders_the_camera(tmp_path):
    source = tmp_path / "screen.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "testsrc2=size=320x176:rate=25", "-t", "4", "-pix_fmt", "yuv420p",
         str(source)],
        check=True,
    )
    camera = Camera(240, 132, 4 / 3)
    camera.move(1.2, 2.0, 60, 40)
    segment = render._render_video_segment(
        source, 1.0, 1.0, 1.0 + EASE_SECONDS + 0.5, tmp_path / "segment.mp4",
        camera.output_size(None), camera,
    )
    # The tpad frames used to be cut off by an output-side -t.
    assert render.probe_duration(segment) == pytest.approx(2.5, abs=0.08)
    assert render.probe_video_size(segment) == (240, 132)


def test_a_line_of_text_is_framed_from_its_first_word():
    camera = Camera(1920, 1080)
    # "The model answered" just right of a 260 px sidebar.
    line = {"x": 400, "y": 640, "width": 180, "height": 20}
    centred = camera.view((2.0, *camera.aim(line, 2.0)))
    led = camera.view((2.0, *camera.aim(line, 2.0, lead=True)))
    assert centred["x"] < 260  # centred, the sidebar is in the frame
    assert 260 < led["x"] < 400  # led: the sidebar is out, the line opens the frame
    assert led["x"] == pytest.approx(400 - 960 * 0.08)


def test_plan_segments_cuts_the_thinking_time():
    steps = [(10.0, None, 2.0), (40.0, None, 0.0), (41.0, None, 5.0)]
    segments, warnings = render.plan_segments(steps, 100.0)
    assert [s.name for s in segments] == ["lead", "0000", "0001", "0002"]
    assert segments[0].output_duration == 1.0  # lead-in capped
    assert segments[1].output_duration == pytest.approx(2.4)  # narration + tail, not 30 s
    assert segments[2].output_duration == pytest.approx(1.0)  # floor, capped by the next step
    assert segments[3].output_duration == pytest.approx(5.4)
    assert warnings == []
    held, warnings = render.plan_segments([(0.0, None, 3.0), (1.0, None, 0.0)], 5.0)
    assert held[0].source_duration == 1.0 and held[0].output_duration == pytest.approx(3.4)
    assert warnings[0]["needed_seconds"] == 3.0


def test_the_length_estimate_is_the_render_plan_not_the_clock():
    from types import SimpleNamespace
    from reel_studio.engine import BrowserSession
    from reel_studio.render import CARD_DURATION, RenderConfig

    fake = SimpleNamespace(
        timeline=[(10.0, None, 2.0), (400.0, None, 3.0)],
        render_config=RenderConfig(title="T", cta_url="https://example.com"),
    )
    estimate = BrowserSession.estimated_length(fake, 800.0)
    assert estimate == pytest.approx(1.0 + 2.4 + 3.4 + 2 * CARD_DURATION)


def test_a_shot_moves_the_camera_where_its_footage_starts():
    """The move is keyed to the step's video offset (after the action settled),
    not to when the act call began: that stretch is cut from the video, and a
    push-in eased there reached the video as a jump."""
    import inspect
    from reel_studio.engine import BrowserSession

    source = inspect.getsource(BrowserSession.act)
    applied = source.index("self.camera.move(offset, *self.pending_shot)")
    assert applied > source.index("offset = action_completed_at")


def test_a_new_page_cuts_to_wide_instead_of_easing():
    camera = Camera(1920, 1080, 4 / 3)
    camera.move(10.0, 2.0, 400, 300)
    camera.move(20.0, 1.0, ease=0.0)
    assert camera.keys[-1]["ease"] == 0.0
    assert "ease" not in camera.keys[0]
    expression = camera.zoompan(19.5, 21.0, (1920, 1080))
    assert "/0.001," in expression  # a step, not a one-second ease
    assert "/1.000," not in expression.split("20.000")[1].split(")")[0]
