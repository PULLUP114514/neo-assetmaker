"""VapourSynth and x264-7mod export pipeline."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from config.constants import get_resolution_spec
from core.media_tools import MediaToolchain
from core.video_processor import X264_PARAMS


def build_vspipe_command(vspipe_path: str, script_path: str) -> list[str]:
    """Build a VSPipe command that emits Y4M to stdout."""
    return [vspipe_path, "-c", "y4m", script_path, "-"]


def build_x264_command(
    x264_path: str,
    output_path: str,
    *,
    crf: int = 26,
    preset: str = "veryslow",
) -> list[str]:
    """Build an x264-7mod command that consumes Y4M from stdin."""
    return [
        x264_path,
        "--demuxer",
        "y4m",
        "--preset",
        preset,
        "--crf",
        str(crf),
        "--profile",
        "high",
        "--output-csp",
        "i420",
        "--output",
        output_path,
        "--x264-params",
        X264_PARAMS,
        "-",
    ]


def _format_fps(fps: float) -> str:
    return f"{float(fps):g}"


def build_mp4box_mux_command(
    muxer_path: str,
    raw_h264_path: str,
    output_path: str,
    fps: float,
) -> list[str]:
    """Build an MP4Box command for wrapping raw H.264 into MP4."""
    return [
        muxer_path,
        "-add",
        f"{raw_h264_path}:fps={_format_fps(fps)}",
        "-new",
        output_path,
    ]


def build_lsmash_mux_command(
    muxer_path: str,
    raw_h264_path: str,
    output_path: str,
    fps: float,
) -> list[str]:
    """Build an lsmash-muxer command for wrapping raw H.264 into MP4."""
    return [
        muxer_path,
        "-i",
        raw_h264_path,
        "--fps",
        _format_fps(fps),
        "-o",
        output_path,
    ]


def build_mux_command(
    muxer_path: str,
    raw_h264_path: str,
    output_path: str,
    fps: float,
) -> list[str]:
    """Build the configured MP4 muxer command."""
    muxer_name = Path(muxer_path).name.lower()
    if "mp4box" in muxer_name:
        return build_mp4box_mux_command(muxer_path, raw_h264_path, output_path, fps)
    return build_lsmash_mux_command(muxer_path, raw_h264_path, output_path, fps)


def _vs_path(path: str) -> str:
    return Path(path).as_posix()


def _quote_vs_string(value: str) -> str:
    return repr(_vs_path(value))


def write_vpy_script(script_path: str | os.PathLike[str], params) -> None:
    """Write a VapourSynth script for one export track."""
    spec = get_resolution_spec(params.resolution)
    target_w = int(spec["width"])
    target_h = int(spec["height"])
    padded_w = int(spec["padded_width"])
    padded_h = int(spec["padded_height"])
    padding_side = spec["padding_side"]
    rotate_180 = bool(spec["rotate_180"])
    start_frame = max(0, int(params.start_frame))
    end_frame = max(start_frame, int(params.end_frame))
    crop_x, crop_y, crop_w, crop_h = [max(0, int(v)) for v in params.cropbox]
    rotation = int(params.rotation) % 360

    lines = [
        "import vapoursynth as vs",
        "core = vs.core",
    ]

    if params.is_image:
        lines.extend(
            [
                f"clip = core.imwri.Read({_quote_vs_string(params.video_path)})",
                "clip = clip if clip.format.id == vs.RGB24 else core.resize.Bicubic(clip, format=vs.RGB24)",
                f"clip = core.std.Loop(clip, length={max(1, end_frame - start_frame)})",
            ]
        )
    else:
        lines.extend(
            [
                f"clip = core.lsmas.LWLibavSource({_quote_vs_string(params.video_path)})",
                f"clip = clip[{start_frame}:{end_frame}]",
            ]
        )
        if rotation == 90:
            lines.append("clip = core.std.Transpose(clip)")
        elif rotation == 180:
            lines.append("clip = core.std.Turn180(clip)")
        elif rotation == 270:
            lines.append("clip = core.std.Transpose(core.std.Turn180(clip))")
        elif rotation != 0:
            lines.append("# Arbitrary-angle rotation is not supported by the bundled VapourSynth v1 script.")

        if crop_w > 0 and crop_h > 0:
            lines.append(
                "clip = core.std.Crop("
                f"clip, left={crop_x}, top={crop_y}, "
                f"right=max(0, clip.width - {crop_x + crop_w}), "
                f"bottom=max(0, clip.height - {crop_y + crop_h}))"
            )

    lines.append(
        f"clip = core.resize.Bicubic(clip, width={target_w}, height={target_h}, format=vs.YUV420P8)"
    )

    if padding_side == "right":
        lines.append(f"clip = core.std.AddBorders(clip, right={padded_w - target_w})")
    elif padding_side == "bottom":
        lines.append(f"clip = core.std.AddBorders(clip, bottom={padded_h - target_h})")

    if rotate_180:
        lines.append("clip = core.std.Turn180(clip)")

    lines.append("clip.set_output()")

    Path(script_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


class MediaEncoder:
    """Run VSPipe and x264-7mod as a cancellable encode pipeline."""

    def __init__(self, toolchain: MediaToolchain):
        self.toolchain = toolchain
        self.active_processes: list[subprocess.Popen] = []

    def terminate_active_processes(self) -> None:
        processes = list(self.active_processes)
        for process in processes:
            if process.poll() is None:
                try:
                    process.terminate()
                except Exception:
                    pass
        for process in processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=2)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass
        self.active_processes.clear()

    def encode_vpy_to_mp4(
        self,
        script_path: str,
        output_path: str,
        fps: float,
        *,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> None:
        if is_cancelled and is_cancelled():
            self.terminate_active_processes()
            raise InterruptedError("Export cancelled")

        missing = self.toolchain.missing_for_export()
        if missing:
            raise RuntimeError("Missing media tools: " + ", ".join(missing))

        output_root, output_ext = os.path.splitext(output_path)
        temp_output = f"{output_root}.tmp{output_ext or '.mp4'}"
        temp_raw = f"{output_root}.tmp.264"
        if os.path.exists(temp_output):
            os.remove(temp_output)
        if os.path.exists(temp_raw):
            os.remove(temp_raw)

        result = self._run_encode_pipeline(script_path, temp_output, is_cancelled)
        if result["x264_returncode"] != 0 and _should_retry_with_muxer(result["stderr"]):
            if not self.toolchain.muxer_path:
                raise RuntimeError(
                    "x264-7mod cannot write MP4 directly and no MP4 muxer was found"
                )
            if os.path.exists(temp_output):
                os.remove(temp_output)
            result = self._run_encode_pipeline(script_path, temp_raw, is_cancelled)
            if result["vspipe_returncode"] != 0:
                raise RuntimeError(
                    f"VSPipe failed with code {result['vspipe_returncode']}"
                )
            if result["x264_returncode"] != 0:
                raise RuntimeError(
                    "x264-7mod failed while writing raw H.264: "
                    + result["stderr"][-500:]
                )
            self._run_muxer(temp_raw, temp_output, fps)
        elif result["vspipe_returncode"] != 0:
            raise RuntimeError(f"VSPipe failed with code {result['vspipe_returncode']}")
        elif result["x264_returncode"] != 0:
            raise RuntimeError(
                f"x264-7mod failed with code {result['x264_returncode']}: "
                + result["stderr"][-500:]
            )

        os.replace(temp_output, output_path)
        if os.path.exists(temp_raw):
            os.remove(temp_raw)

    def _run_encode_pipeline(
        self,
        script_path: str,
        output_path: str,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> dict[str, object]:
        vspipe_cmd = build_vspipe_command(self.toolchain.vspipe_path, script_path)
        x264_cmd = build_x264_command(self.toolchain.x264_path, output_path)
        popen_kwargs = {
            "stderr": subprocess.PIPE,
            "creationflags": subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        }
        if not popen_kwargs["creationflags"]:
            popen_kwargs.pop("creationflags")

        vspipe = subprocess.Popen(vspipe_cmd, stdout=subprocess.PIPE, **popen_kwargs)
        x264 = subprocess.Popen(
            x264_cmd,
            stdin=vspipe.stdout,
            stdout=subprocess.PIPE,
            **popen_kwargs,
        )
        if vspipe.stdout is not None:
            vspipe.stdout.close()
        self.active_processes = [vspipe, x264]

        stderr = b""
        try:
            while x264.poll() is None:
                if is_cancelled and is_cancelled():
                    self.terminate_active_processes()
                    if os.path.exists(output_path):
                        os.remove(output_path)
                    raise InterruptedError("Export cancelled")
                time.sleep(0.1)
            _stdout, stderr = x264.communicate(timeout=5)
            vspipe.wait(timeout=5)
        finally:
            self.terminate_active_processes()

        return {
            "vspipe_returncode": int(vspipe.returncode or 0),
            "x264_returncode": int(x264.returncode or 0),
            "stderr": stderr.decode("utf-8", errors="replace"),
        }

    def _run_muxer(self, raw_h264_path: str, output_path: str, fps: float) -> None:
        cmd = build_mux_command(self.toolchain.muxer_path, raw_h264_path, output_path, fps)
        run_kwargs = {
            "capture_output": True,
            "text": True,
            "timeout": 120,
        }
        if sys.platform == "win32":
            run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(cmd, **run_kwargs)
        if result.returncode != 0:
            raise RuntimeError(f"MP4 muxer failed: {result.stderr[-500:]}")


def _should_retry_with_muxer(stderr: str) -> bool:
    text = stderr.lower()
    if "mp4" not in text:
        return False
    markers = (
        "not compiled",
        "unsupported",
        "not supported",
        "could not open output",
        "can't open output",
    )
    return any(marker in text for marker in markers)


__all__ = [
    "MediaEncoder",
    "MediaToolchain",
    "build_vspipe_command",
    "build_x264_command",
    "build_mp4box_mux_command",
    "build_lsmash_mux_command",
    "build_mux_command",
    "write_vpy_script",
]
