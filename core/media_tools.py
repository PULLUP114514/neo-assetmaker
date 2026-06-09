"""Media tool discovery for the bundled preview and export pipeline."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from utils.file_utils import get_app_dir


def _exe_names(base_name: str) -> tuple[str, ...]:
    if sys.platform == "win32" and not base_name.lower().endswith(".exe"):
        return (f"{base_name}.exe", base_name)
    return (base_name,)


def _candidate_dirs(app_dir: Path) -> tuple[Path, ...]:
    return (
        app_dir / "tools" / "media",
        app_dir / "media",
        app_dir / "tools",
        app_dir,
    )


def _find_tool(app_dir: Path, names: Iterable[str]) -> str:
    for directory in _candidate_dirs(app_dir):
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return str(candidate)

    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return ""


@dataclass(frozen=True)
class MediaToolchain:
    """Resolved paths for the preview and export media toolchain."""

    mpv_path: str = ""
    vspipe_path: str = ""
    x264_path: str = ""
    muxer_path: str = ""

    @classmethod
    def discover(cls, app_dir: Optional[os.PathLike[str] | str] = None) -> "MediaToolchain":
        root = Path(app_dir) if app_dir is not None else Path(get_app_dir())
        return cls(
            mpv_path=_find_tool(root, _exe_names("mpv")),
            vspipe_path=_find_tool(root, _exe_names("VSPipe")),
            x264_path=_find_tool(root, ("x264-7mod.exe", "x264-7mod", "x264.exe", "x264")),
            muxer_path=_find_tool(
                root,
                (
                    "MP4Box.exe",
                    "MP4Box",
                    "mp4box.exe",
                    "mp4box",
                    "lsmash-muxer.exe",
                    "lsmash-muxer",
                    "muxer.exe",
                    "muxer",
                ),
            ),
        )

    def missing_for_export(self) -> list[str]:
        missing = []
        if not self.vspipe_path:
            missing.append("VSPipe")
        if not self.x264_path:
            missing.append("x264-7mod")
        return missing

    def missing_for_preview(self) -> list[str]:
        return [] if self.mpv_path else ["mpv"]

    def describe(self) -> str:
        parts = {
            "mpv": self.mpv_path,
            "VSPipe": self.vspipe_path,
            "x264-7mod": self.x264_path,
            "MP4 muxer": self.muxer_path,
        }
        return ", ".join(f"{name}={'found' if path else 'missing'}" for name, path in parts.items())
