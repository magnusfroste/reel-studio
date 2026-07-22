"""ffmpeg and X11 recording helpers."""

from pathlib import Path
import signal
import subprocess
from typing import Sequence


def start_recording(display: str, width: int, height: int, output: Path) -> subprocess.Popen[bytes]:
    output.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [
            "ffmpeg", "-y", "-f", "x11grab", "-video_size", f"{width}x{height}",
            "-framerate", "25", "-i", f"{display}.0", "-draw_mouse", "1",
            "-pix_fmt", "yuv420p", str(output),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def stop_recording(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if process.stdin:
        try:
            process.stdin.write(b"q\n")
            process.stdin.flush()
        except BrokenPipeError:
            pass
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.send_signal(signal.SIGINT)
        process.wait(timeout=15)


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
) -> Path:
    """Create a delayed mixed narration track and mux it into the video."""
    if not clips:
        video_path.replace(output_path)
        return output_path

    command = ["ffmpeg", "-y", "-i", str(video_path)]
    filters: list[str] = []
    for index, (offset, clip) in enumerate(clips, start=1):
        command.extend(["-i", str(clip)])
        delay = max(0, round(offset * 1000))
        filters.append(f"[{index}:a]adelay={delay}|{delay}[a{index}]")
    labels = "".join(f"[a{i}]" for i in range(1, len(clips) + 1))
    filters.append(f"{labels}amix=inputs={len(clips)}:duration=longest:dropout_transition=0[a]")
    command.extend([
        "-filter_complex", ";".join(filters), "-map", "0:v", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac", "-shortest", str(output_path),
    ])
    subprocess.run(command, check=True)
    return output_path
