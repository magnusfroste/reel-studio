"""ffmpeg and X11 recording helpers."""

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from typing import Sequence


FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
CARD_DURATION = 3.0


@dataclass(frozen=True)
class RenderConfig:
    title: str = ""
    subtitle: str = ""
    accent: str = "#1f2a44"
    cta_url: str = ""
    cta_text: str = "Learn more"
    music: str = "none"


def start_recording(display: str, width: int, height: int, output: Path) -> subprocess.Popen[bytes]:
    output.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [
            "ffmpeg", "-loglevel", "error", "-nostats", "-y", "-f", "x11grab",
            "-video_size", f"{width}x{height}",
            "-framerate", "25", "-i", f"{display}.0", "-draw_mouse", "1",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-threads", "0", "-pix_fmt", "yuv420p", str(output),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_recording(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.send_signal(signal.SIGINT)
        process.wait(timeout=12)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=4)
    finally:
        if process.stdin:
            process.stdin.close()


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(result.stdout.strip())


def mux_narration(
    video_path: Path,
    clips: Sequence[tuple[float, Path]],
    output_path: Path,
    output_size: tuple[int, int] | None = None,
    config: RenderConfig | None = None,
) -> Path:
    """Create a delayed mixed narration track and mux it into the video."""
    config = config or RenderConfig()
    if not clips and not _has_branding(config) and config.music == "none":
        if output_size is None:
            video_path.replace(output_path)
        else:
            subprocess.run(
                [
                    "ffmpeg", "-loglevel", "error", "-y", "-i", str(video_path),
                    "-vf", f"scale={output_size[0]}:{output_size[1]}",
                    "-an", "-c:v", "libx264", "-preset", "veryfast",
                    "-pix_fmt", "yuv420p", str(output_path),
                ],
                check=True,
            )
        return output_path

    video_duration = probe_duration(video_path)
    with tempfile.TemporaryDirectory(
        prefix=".continuous-", dir=output_path.parent
    ) as temporary:
        body_path = Path(temporary) / "body.mp4"
        _render_video_only(video_path, body_path, output_size)
        _compose_video(
            body_path,
            clips,
            output_path,
            video_duration,
            output_size,
            config,
        )
    return output_path


def _render_video_only(
    video_path: Path,
    output_path: Path,
    output_size: tuple[int, int] | None,
) -> None:
    command = ["ffmpeg", "-loglevel", "error", "-y", "-i", str(video_path)]
    if output_size is not None:
        command.extend([
            "-vf", f"scale={output_size[0]}:{output_size[1]}",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        ])
    else:
        command.extend(["-c:v", "copy"])
    command.extend(["-an", str(output_path)])
    subprocess.run(command, check=True)


def _has_branding(config: RenderConfig) -> bool:
    return bool(config.title.strip() or config.cta_url.strip())


def _escape_drawtext(text: str) -> str:
    return (
        text.replace("\\", r"\\")
        .replace(":", r"\:")
        .replace("'", r"\'")
        .replace(",", r"\,")
        .replace("%", r"\%")
    )


def _card(
    output_path: Path,
    width: int,
    height: int,
    accent: str,
    lines: Sequence[tuple[str, int]],
) -> None:
    if not Path(FONT_PATH).is_file():
        raise RuntimeError(f"Title-card font is missing: {FONT_PATH}")
    drawtext = []
    center_offset = (len(lines) - 1) * 0.7
    for index, (text, size) in enumerate(lines):
        drawtext.append(
            "drawtext="
            f"fontfile={FONT_PATH}:text='{_escape_drawtext(text)}':"
            f"fontcolor=white:fontsize={size}:"
            f"x=(w-text_w)/2:y=(h-text_h)/2+{index * 1.4 - center_offset}*{size}"
        )
    subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i",
            f"color=c={accent}:s={width}x{height}:r=25:d={CARD_DURATION}",
            "-vf", ",".join(drawtext),
            "-an", "-c:v", "libx264", "-preset", "veryfast",
            "-pix_fmt", "yuv420p", str(output_path),
        ],
        check=True,
    )


def _compose_video(
    body_path: Path,
    clips: Sequence[tuple[float, Path]],
    output_path: Path,
    body_duration: float,
    output_size: tuple[int, int] | None,
    config: RenderConfig,
) -> None:
    width, height = output_size or probe_video_size(body_path)
    temporary = body_path.parent
    parts: list[Path] = []
    intro_duration = 0.0
    if config.title.strip():
        intro = temporary / "intro.mp4"
        _card(
            intro, width, height, config.accent,
            [(config.title.strip(), max(36, width // 22)),
             (config.subtitle.strip(), max(20, width // 48))]
            if config.subtitle.strip()
            else [(config.title.strip(), max(36, width // 22))],
        )
        parts.append(intro)
        intro_duration = CARD_DURATION
    parts.append(body_path)
    if config.cta_url.strip():
        outro = temporary / "outro.mp4"
        _card(
            outro, width, height, config.accent,
            [
                (config.cta_text.strip() or "Learn more", max(28, width // 32)),
                (config.cta_url.strip(), max(22, width // 44)),
            ],
        )
        parts.append(outro)
    base = temporary / "branded-base.mp4"
    concat = temporary / "branded.txt"
    concat.write_text("".join(f"file '{path}'\n" for path in parts))
    subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-y", "-f", "concat",
            "-safe", "0", "-i", str(concat), "-c", "copy", str(base),
        ],
        check=True,
    )
    shifted = [
        (offset + intro_duration, clip)
        for offset, clip in clips
    ]
    total_duration = intro_duration + body_duration
    if config.cta_url.strip():
        total_duration += CARD_DURATION
    _mux_segment_audio(
        base, shifted, output_path, total_duration, config.music
    )


def probe_video_size(path: Path) -> tuple[int, int]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0:s=x", str(path),
        ],
        check=True, capture_output=True, text=True,
    )
    width, height = result.stdout.strip().split("x")
    return int(width), int(height)


def _mux_segment_audio(
    video_path: Path,
    clips: Sequence[tuple[float, Path]],
    output_path: Path,
    duration: float,
    music: str = "none",
) -> None:
    command = ["ffmpeg", "-loglevel", "error", "-y", "-i", str(video_path)]
    filters: list[str] = []
    for index, (offset, clip) in enumerate(clips, start=1):
        command.extend(["-i", str(clip)])
        delay = max(0, round(offset * 1000))
        filters.append(f"[{index}:a]adelay={delay}|{delay}[a{index}]")
    input_count = len(clips)
    if music == "subtle":
        command.extend([
            "-f", "lavfi", "-i",
            (
                "anoisesrc=color=pink:amplitude=0.015:"
                f"sample_rate=48000:d={duration:.3f}"
            ),
        ])
        input_count += 1
        music_index = input_count
        filters.append(
            f"[{music_index}:a]highpass=f=120,lowpass=f=1800,"
            "volume=0.4[bed]"
        )
    labels = "".join(f"[a{i}]" for i in range(1, len(clips) + 1))
    if labels:
        if music == "subtle":
            labels += "[bed]"
        filters.append(
            f"{labels}amix=inputs={input_count}:duration=longest:"
            "dropout_transition=0:normalize=0[a]"
        )
    elif music == "subtle":
        filters.append("[bed]anull[a]")
    if filters:
        command.extend(["-filter_complex", ";".join(filters), "-map", "0:v", "-map", "[a]"])
    else:
        command.extend([
            "-f", "lavfi", "-i",
            "anullsrc=channel_layout=mono:sample_rate=24000",
            "-map", "0:v", "-map", "1:a",
        ])
    command.extend([
        "-t", f"{duration:.3f}", "-c:v", "copy", "-c:a", "aac",
        str(output_path),
    ])
    subprocess.run(command, check=True)


SEGMENT_FLOOR = 1.0
SEGMENT_TAIL_PAD = 0.4
LEAD_IN_CAP = 1.0


@dataclass(frozen=True)
class SegmentedRenderResult:
    path: Path
    duration: float
    warnings: list[dict]


def segmented_render_enabled() -> bool:
    return os.environ.get("REEL_SEGMENTED", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def segmented_render(
    video_path: Path,
    steps: Sequence[tuple[float, Path | None, float]],
    output_path: Path,
    output_size: tuple[int, int] | None = None,
    config: RenderConfig | None = None,
) -> SegmentedRenderResult:
    """Render kept step windows from the original continuous recording."""
    config = config or RenderConfig()
    video_duration = probe_duration(video_path)
    ordered = sorted(
        (offset, clip, max(0.0, duration))
        for offset, clip, duration in steps
        if 0 <= offset < video_duration
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".segments-", dir=output_path.parent
    ) as temporary:
        temporary_path = Path(temporary)
        segment_paths: list[Path] = []
        audio_clips: list[tuple[float, Path]] = []
        warnings: list[dict] = []
        cumulative = 0.0

        if ordered and ordered[0][0] > 0:
            lead = min(LEAD_IN_CAP, ordered[0][0])
            segment_paths.append(
                _render_video_segment(
                    video_path, 0.0, lead, lead,
                    temporary_path / "segment-lead.mp4",
                    output_size,
                )
            )
            cumulative += lead

        for index, (offset, clip, narration_duration) in enumerate(ordered):
            next_offset = (
                ordered[index + 1][0]
                if index + 1 < len(ordered)
                else video_duration
            )
            available = max(0.0, next_offset - offset)
            if available <= 0:
                continue
            target = max(narration_duration, SEGMENT_FLOOR) + SEGMENT_TAIL_PAD
            keep_duration = min(available, target)
            if narration_duration > available:
                warnings.append({
                    "index": index,
                    "needed_seconds": round(narration_duration, 3),
                    "available_seconds": round(available, 3),
                })
                keep_duration = narration_duration + SEGMENT_TAIL_PAD
            segment_path = temporary_path / f"segment-{index:04d}.mp4"
            segment_paths.append(
                _render_video_segment(
                    video_path,
                    offset,
                    min(available, keep_duration),
                    keep_duration,
                    segment_path,
                    output_size,
                )
            )
            if clip is not None:
                audio_clips.append((cumulative, clip))
            cumulative += keep_duration

        if not segment_paths:
            segment_paths.append(
                _render_video_segment(
                    video_path, 0.0, video_duration, video_duration,
                    temporary_path / "segment-full.mp4",
                    output_size,
                )
            )
            cumulative = video_duration

        joined = temporary_path / "joined.mp4"
        concat_list = temporary_path / "segments.txt"
        concat_list.write_text(
            "".join(f"file '{path}'\n" for path in segment_paths)
        )
        subprocess.run(
            [
                "ffmpeg", "-loglevel", "error", "-y", "-f", "concat",
                "-safe", "0", "-i", str(concat_list), "-c", "copy",
                str(joined),
            ],
            check=True,
        )
        if _has_branding(config) or config.music == "subtle":
            _compose_video(
                joined, audio_clips, output_path, cumulative,
                output_size, config,
            )
        else:
            _mux_segment_audio(joined, audio_clips, output_path, cumulative)
    return SegmentedRenderResult(output_path, probe_duration(output_path), warnings)


def _render_video_segment(
    video_path: Path,
    offset: float,
    source_duration: float,
    output_duration: float,
    output_path: Path,
    output_size: tuple[int, int] | None = None,
) -> Path:
    command = [
        "ffmpeg", "-loglevel", "error", "-y",
        "-ss", f"{offset:.3f}", "-i", str(video_path),
        "-t", f"{source_duration:.3f}",
    ]
    extension = output_duration - source_duration
    if extension > 0.01:
        filters = [f"tpad=stop_mode=clone:stop_duration={extension:.3f}"]
    else:
        filters = []
    if output_size is not None:
        filters.append(f"scale={output_size[0]}:{output_size[1]}")
    if filters:
        command.extend(["-vf", ",".join(filters)])
    command.extend([
        "-an", "-c:v", "libx264", "-preset", "veryfast",
        "-pix_fmt", "yuv420p", str(output_path),
    ])
    subprocess.run(command, check=True)
    return output_path


def rerender_narration(
    video_path: Path,
    clips: Sequence[tuple[float, Path]],
    output_path: Path,
    output_size: tuple[int, int] | None = None,
    config: RenderConfig | None = None,
) -> Path:
    """Replace a video's audio with delayed narration, extending its last frame if needed."""
    config = config or RenderConfig()
    video_duration = probe_duration(video_path)
    temp_path = output_path.with_name(f".{output_path.stem}.rerender.mp4")
    if not clips:
        video_filter = (
            f"scale={output_size[0]}:{output_size[1]}"
            if output_size is not None else None
        )
        command = [
            "ffmpeg", "-y", "-i", str(video_path),
            *(["-vf", video_filter] if video_filter else []),
            "-map", "0:v:0", "-an",
            *(["-c:v", "libx264", "-preset", "veryfast"]
              if output_size is not None else ["-c:v", "copy"]),
            str(temp_path),
        ]
        subprocess.run(command, check=True)
        if _has_branding(config) or config.music == "subtle":
            _compose_video(
                temp_path, clips, output_path, video_duration,
                output_size, config,
            )
            temp_path.unlink(missing_ok=True)
        else:
            temp_path.replace(output_path)
        return output_path

    clip_durations = [(offset, clip, probe_duration(clip)) for offset, clip in clips]
    audio_end = max(offset + duration for offset, _, duration in clip_durations)
    extend_by = max(0.0, audio_end - video_duration)
    command = ["ffmpeg", "-y", "-i", str(video_path)]
    filters: list[str] = []
    for index, (offset, clip, _) in enumerate(clip_durations, start=1):
        command.extend(["-i", str(clip)])
        delay = max(0, round(offset * 1000))
        filters.append(f"[{index}:a]adelay={delay}|{delay}[a{index}]")
    labels = "".join(f"[a{i}]" for i in range(1, len(clip_durations) + 1))
    filters.append(
        f"{labels}amix=inputs={len(clip_durations)}:duration=longest:"
        "dropout_transition=0[a]"
    )
    if extend_by > 0.05:
        filters.insert(
            0,
            f"[0:v]tpad=stop_mode=clone:stop_duration={extend_by:.3f}[v]",
        )
        video_map = "[v]"
        video_codec = ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
    else:
        video_map = "0:v:0"
        video_codec = ["-c:v", "copy"]
    if output_size is not None:
        if extend_by > 0.05:
            filters[0] = (
                f"[0:v]tpad=stop_mode=clone:stop_duration={extend_by:.3f}[padded];"
                f"[padded]scale={output_size[0]}:{output_size[1]}[scaled]"
            )
        else:
            filters.insert(
                0,
                f"[0:v]scale={output_size[0]}:{output_size[1]}[scaled]",
            )
        video_map = "[scaled]"
        video_codec = ["-c:v", "libx264", "-preset", "veryfast"]
    command.extend([
        "-filter_complex", ";".join(filters),
        "-map", video_map, "-map", "[a]",
        *video_codec, "-c:a", "aac", str(temp_path),
    ])
    subprocess.run(command, check=True)
    if _has_branding(config) or config.music == "subtle":
        _compose_video(
            temp_path, clips, output_path,
            max(video_duration, audio_end), output_size, config,
        )
        temp_path.unlink(missing_ok=True)
    else:
        temp_path.replace(output_path)
    return output_path
