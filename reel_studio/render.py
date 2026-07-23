"""ffmpeg and X11 recording helpers."""

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from typing import Sequence


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
) -> Path:
    """Create a delayed mixed narration track and mux it into the video."""
    if not clips:
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

    command = ["ffmpeg", "-y", "-i", str(video_path)]
    filters: list[str] = []
    for index, (offset, clip) in enumerate(clips, start=1):
        command.extend(["-i", str(clip)])
        delay = max(0, round(offset * 1000))
        filters.append(f"[{index}:a]adelay={delay}|{delay}[a{index}]")
    labels = "".join(f"[a{i}]" for i in range(1, len(clips) + 1))
    filters.append(f"{labels}amix=inputs={len(clips)}:duration=longest:dropout_transition=0[a]")
    video_map = "0:v"
    video_codec = ["-c:v", "copy"]
    if output_size is not None:
        filters.insert(
            0,
            f"[0:v]scale={output_size[0]}:{output_size[1]}[v]",
        )
        video_map = "[v]"
        video_codec = ["-c:v", "libx264", "-preset", "veryfast"]
    command.extend([
        "-filter_complex", ";".join(filters), "-map", video_map, "-map", "[a]",
        *video_codec, "-c:a", "aac", str(output_path),
    ])
    subprocess.run(command, check=True)
    return output_path


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
) -> SegmentedRenderResult:
    """Render kept step windows from the original continuous recording."""
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


def _mux_segment_audio(
    video_path: Path,
    clips: Sequence[tuple[float, Path]],
    output_path: Path,
    duration: float,
) -> None:
    command = ["ffmpeg", "-loglevel", "error", "-y", "-i", str(video_path)]
    if clips:
        filters: list[str] = []
        for index, (offset, clip) in enumerate(clips, start=1):
            command.extend(["-i", str(clip)])
            delay = max(0, round(offset * 1000))
            filters.append(f"[{index}:a]adelay={delay}|{delay}[a{index}]")
        labels = "".join(f"[a{i}]" for i in range(1, len(clips) + 1))
        filters.append(
            f"{labels}amix=inputs={len(clips)}:duration=longest:"
            "dropout_transition=0[a]"
        )
        command.extend([
            "-filter_complex", ";".join(filters),
            "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
            str(output_path),
        ])
    else:
        command.extend([
            "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=24000",
            "-map", "0:v", "-map", "1:a", "-t", f"{duration:.3f}",
            "-c:v", "copy", "-c:a", "aac", str(output_path),
        ])
    subprocess.run(command, check=True)


def rerender_narration(
    video_path: Path,
    clips: Sequence[tuple[float, Path]],
    output_path: Path,
    output_size: tuple[int, int] | None = None,
) -> Path:
    """Replace a video's audio with delayed narration, extending its last frame if needed."""
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
    temp_path.replace(output_path)
    return output_path
