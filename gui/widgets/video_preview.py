"""Video preview widget backed by mpv playback and metadata."""

from __future__ import annotations

import logging
import os
import json
import sys
import tempfile
import uuid
from typing import Optional, Tuple, TYPE_CHECKING

import numpy as np

try:
    import cv2

    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

from PyQt6.QtCore import QPoint, QProcess, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QImage, QKeyEvent, QMouseEvent, QPainter, QPen, QPixmap
from PyQt6.QtNetwork import QLocalSocket
from PyQt6.QtWidgets import QLabel, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, setCustomStyleSheet

from core.media_tools import MediaToolchain
from core.video_processor import VideoProcessor

if TYPE_CHECKING:
    from config.epconfig import EPConfig

logger = logging.getLogger(__name__)

DEFAULT_TARGET_WIDTH = 360
DEFAULT_TARGET_HEIGHT = 640


class _PreviewLabel(QLabel):
    def __init__(self, owner: "VideoPreviewWidget"):
        super().__init__(owner)
        self._owner = owner

    def paintEvent(self, event):
        super().paintEvent(event)
        self._owner._paint_cropbox(self)

    def mousePressEvent(self, event: QMouseEvent):
        self._owner._handle_mouse_press(self, event)

    def mouseMoveEvent(self, event: QMouseEvent):
        self._owner._handle_mouse_move(self, event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._owner._handle_mouse_release(event)


class _MpvSurface(QWidget):
    def __init__(self, owner: "VideoPreviewWidget"):
        super().__init__(owner)
        self._owner = owner
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)

    def paintEvent(self, event):
        super().paintEvent(event)
        self._owner._paint_cropbox(self)

    def mousePressEvent(self, event: QMouseEvent):
        self._owner._handle_mouse_press(self, event)

    def mouseMoveEvent(self, event: QMouseEvent):
        self._owner._handle_mouse_move(self, event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._owner._handle_mouse_release(event)


class VideoPreviewWidget(QWidget):
    """Preview media, expose crop/trim state, and keep the legacy public API."""

    cropbox_changed = pyqtSignal(int, int, int, int)
    frame_changed = pyqtSignal(int)
    playback_state_changed = pyqtSignal(bool)
    video_loaded = pyqtSignal(int, float)
    rotation_changed = pyqtSignal(int)

    DRAG_NONE = 0
    DRAG_MOVE = 1
    DRAG_RESIZE_TL = 2
    DRAG_RESIZE_TR = 3
    DRAG_RESIZE_BL = 4
    DRAG_RESIZE_BR = 5

    def __init__(self, parent=None):
        super().__init__(parent)
        self.video_path = ""
        self.video_fps = 30.0
        self.video_width = 0
        self.video_height = 0
        self.total_frames = 0
        self.current_frame_index = 0
        self.current_frame: Optional[np.ndarray] = None

        self._reader_thread = None
        self._mpv_process: Optional[QProcess] = None
        self._mpv_socket: Optional[QLocalSocket] = None
        self._mpv_ipc_server = ""
        self._media_toolchain = MediaToolchain.discover()
        self._has_video = False
        self._loop_frame: Optional[np.ndarray] = None
        self._preview_mode = False
        self._epconfig: Optional["EPConfig"] = None
        self._overlay_renderer = None
        self._use_gl = False
        self._gl_renderer = None
        self._rotation = 0

        self.is_playing = False
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self._on_timer_tick)

        self.target_width = DEFAULT_TARGET_WIDTH
        self.target_height = DEFAULT_TARGET_HEIGHT
        self.target_aspect_ratio = self.target_width / self.target_height
        self.cropbox = [0, 0, self.target_width, self.target_height]

        self.display_scale = 1.0
        self.display_offset_x = 0
        self.display_offset_y = 0
        self.drag_mode = self.DRAG_NONE
        self.drag_start_pos: Optional[QPoint] = None
        self.drag_start_cropbox: list[int] = []
        self.handle_size = 15

        self._setup_ui()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        self._display_stack = QStackedWidget()
        self._display_stack.setMinimumSize(320, 180)
        self._display_stack.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

        self.video_label = _PreviewLabel(self)
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setText("No media loaded")
        self.video_label.setMouseTracking(True)
        setCustomStyleSheet(
            self.video_label,
            "background-color: #1a1a1a; border: none; border-radius: 8px; "
            "color: #888; font-size: 14px; font-weight: 500;",
            "background-color: #0a0a0a; border: none; border-radius: 8px; "
            "color: #666; font-size: 14px; font-weight: 500;",
        )
        self._display_stack.addWidget(self.video_label)

        self._mpv_widget = _MpvSurface(self)
        self._mpv_widget.setMouseTracking(True)
        self._mpv_widget.setStyleSheet("background-color: #000; border: none;")
        self._mpv_page_index = self._display_stack.addWidget(self._mpv_widget)

        layout.addWidget(self._display_stack)
        self.info_label = CaptionLabel("Frame 0/0 | Crop: (0, 0, 0, 0)")
        setCustomStyleSheet(
            self.info_label,
            "color: #999; padding: 4px 10px; background-color: transparent; border: none;",
            "color: #777; padding: 4px 10px; background-color: transparent; border: none;",
        )
        layout.addWidget(self.info_label)

    def _stop_reader_thread(self):
        self._stop_mpv_process()
        if self._reader_thread is not None:
            try:
                self._reader_thread.request_stop()
                self._reader_thread.wait(1000)
            except Exception:
                pass
            self._reader_thread = None
        self._has_video = False

    def _stop_mpv_process(self):
        if self._mpv_process is None:
            return
        try:
            if self._mpv_socket is not None:
                self._send_mpv_command(["quit"])
                self._mpv_socket.disconnectFromServer()
                self._mpv_socket.deleteLater()
                self._mpv_socket = None
            if self._mpv_process.state() != QProcess.ProcessState.NotRunning:
                self._mpv_process.waitForFinished(1000)
            if self._mpv_process.state() != QProcess.ProcessState.NotRunning:
                self._mpv_process.kill()
                self._mpv_process.waitForFinished(1000)
        except Exception as exc:
            logger.debug("mpv shutdown failed: %s", exc)
        finally:
            self._mpv_process = None

    def _send_mpv_command(self, command: list):
        if self._mpv_socket is None:
            return
        if self._mpv_socket.state() != QLocalSocket.LocalSocketState.ConnectedState:
            return
        payload = json.dumps({"command": command}, separators=(",", ":"))
        self._mpv_socket.write((payload + "\n").encode("utf-8"))
        self._mpv_socket.waitForBytesWritten(100)

    def _make_mpv_ipc_server(self) -> str:
        name = f"neo_assetmaker_mpv_{os.getpid()}_{uuid.uuid4().hex}"
        if sys.platform == "win32":
            return name
        return os.path.join(tempfile.gettempdir(), name)

    def _connect_mpv_ipc(self) -> None:
        socket = QLocalSocket(self)
        for _ in range(30):
            socket.connectToServer(self._mpv_ipc_server)
            if socket.waitForConnected(100):
                self._mpv_socket = socket
                return
            socket.abort()
        logger.warning("mpv JSON IPC connection was not established")
        socket.deleteLater()

    def _start_mpv_preview(self, path: str) -> bool:
        self._stop_mpv_process()
        process = QProcess(self)
        self._mpv_ipc_server = self._make_mpv_ipc_server()
        args = [
            "--no-config",
            "--force-window=yes",
            "--keep-open=yes",
            "--pause=yes",
            f"--input-ipc-server={self._mpv_ipc_server}",
            "--osc=no",
            f"--wid={int(self._mpv_widget.winId())}",
            path,
        ]
        process.setProgram(self._media_toolchain.mpv_path)
        process.setArguments(args)
        process.start()
        if not process.waitForStarted(3000):
            logger.error("mpv failed to start: %s", process.errorString())
            self.video_label.setText("mpv failed to start")
            return False
        self._mpv_process = process
        self._connect_mpv_ipc()
        self._display_stack.setCurrentIndex(self._mpv_page_index)
        if self._rotation:
            self._send_mpv_command(["set_property", "video-rotate", self._rotation])
        return True

    def set_target_resolution(self, width: int, height: int):
        if self.target_width == width and self.target_height == height:
            return
        self.target_width = width
        self.target_height = height
        self.target_aspect_ratio = width / height
        if self.video_width > 0 and self.video_height > 0:
            self._init_cropbox()
            self._refresh_display()

    def load_video(self, path: str) -> bool:
        logger.info("Loading video with mpv: %s", path)
        if not os.path.exists(path):
            self.video_label.setText(f"File not found: {path}")
            return False

        self._media_toolchain = MediaToolchain.discover()
        if not self._media_toolchain.mpv_path:
            self.video_label.setText("mpv not found")
            return False

        info = VideoProcessor(self._media_toolchain.mpv_path).get_video_info(path)
        if info is None:
            self.video_label.setText("Unable to load video metadata")
            return False

        self._stop_reader_thread()
        self.pause()
        self._loop_frame = None
        self.video_path = path
        self.video_fps = info.fps or 30.0
        self.video_width = max(1, info.width)
        self.video_height = max(1, info.height)
        self.total_frames = max(1, info.total_frames)
        self.current_frame_index = 0
        self.current_frame = np.zeros(
            (self.video_height, self.video_width, 3), dtype=np.uint8
        )
        self._has_video = True
        self._init_cropbox()
        self._update_info_label()

        if not self._start_mpv_preview(path):
            self._has_video = False
            self.current_frame = None
            return False

        self.video_loaded.emit(self.total_frames, self.video_fps)
        return True

    def load_static_image_from_file(self, image_path: str) -> bool:
        if not HAS_CV2:
            logger.error("OpenCV is required to load images")
            return False
        if not os.path.exists(image_path):
            logger.error("Image file does not exist: %s", image_path)
            return False
        with open(image_path, "rb") as fh:
            data = np.frombuffer(fh.read(), dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
        if img is None:
            logger.error("Unable to read image: %s", image_path)
            return False
        if len(img.shape) == 3 and img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        return self._load_static_frame(img)

    def load_static_image_from_array(self, frame: np.ndarray) -> bool:
        if frame is None:
            return False
        return self._load_static_frame(frame.copy())

    def update_static_frame(self, frame: np.ndarray) -> bool:
        if frame is None:
            return False
        self.current_frame = frame.copy()
        self.video_width = frame.shape[1]
        self.video_height = frame.shape[0]
        self._bound_cropbox()
        self._display_frame(self.current_frame)
        return True

    def load_image_as_loop(
        self, path: str, fps: float = 30.0, duration: float = 5.0
    ) -> bool:
        if not self.load_static_image_from_file(path):
            return False
        self.video_path = path
        self._loop_frame = self.current_frame.copy()
        self.video_fps = fps
        self.total_frames = max(1, int(fps * duration))
        self._has_video = True
        self.video_loaded.emit(self.total_frames, self.video_fps)
        self._update_info_label()
        return True

    def _load_static_frame(self, frame: np.ndarray) -> bool:
        self.pause()
        self._stop_reader_thread()
        self._loop_frame = None
        self._display_stack.setCurrentIndex(0)
        self.video_width = frame.shape[1]
        self.video_height = frame.shape[0]
        self.current_frame = frame
        self.total_frames = 1
        self.current_frame_index = 0
        self._has_video = False
        self._init_cropbox()
        self._display_frame(frame)
        return True

    def _init_cropbox(self):
        rotated_w, rotated_h = self._get_rotated_video_size()
        if rotated_w <= 0 or rotated_h <= 0:
            self.cropbox = [0, 0, self.target_width, self.target_height]
            return
        if rotated_w / rotated_h > self.target_aspect_ratio:
            max_h = rotated_h
            max_w = int(max_h * self.target_aspect_ratio)
        else:
            max_w = rotated_w
            max_h = int(max_w / self.target_aspect_ratio)
        crop_w = max(1, int(max_w * 0.75))
        crop_h = max(1, int(crop_w / self.target_aspect_ratio))
        self.cropbox = [
            max(0, (rotated_w - crop_w) // 2),
            max(0, (rotated_h - crop_h) // 2),
            crop_w,
            crop_h,
        ]
        self._emit_cropbox_changed()

    def _bound_cropbox(self):
        rotated_w, rotated_h = self._get_rotated_video_size()
        if rotated_w <= 0 or rotated_h <= 0:
            return
        x, y, w, h = [int(v) for v in self.cropbox]
        w = max(1, min(w, rotated_w))
        h = max(1, min(h, rotated_h))
        x = max(0, min(x, max(0, rotated_w - w)))
        y = max(0, min(y, max(0, rotated_h - h)))
        self.cropbox = [x, y, w, h]

    def _emit_cropbox_changed(self):
        x, y, w, h = self.cropbox
        self.cropbox_changed.emit(x, y, w, h)
        self._update_info_label()

    def _update_info_label(self):
        x, y, w, h = self.cropbox
        rotation = f" | Rotation: {self._rotation}" if self._rotation else ""
        self.info_label.setText(
            f"Frame {self.current_frame_index}/{self.total_frames} | "
            f"Crop: ({x}, {y}, {w}, {h}){rotation}"
        )

    def _display_frame(self, frame: np.ndarray):
        if frame is None:
            return
        display_frame = self._make_display_frame(frame)
        rgb = self._to_rgb(display_frame)
        rgb = np.ascontiguousarray(rgb)
        h, w = rgb.shape[:2]
        qimage = QImage(rgb.data, w, h, rgb.strides[0], QImage.Format.Format_RGB888)
        pixmap = QPixmap.fromImage(qimage.copy()).scaled(
            self.video_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.video_label.setPixmap(pixmap)
        self._update_display_geometry(self.video_label, w, h)
        self.frame_changed.emit(self.current_frame_index)
        self._update_info_label()

    def _make_display_frame(self, frame: np.ndarray) -> np.ndarray:
        rotated = self._apply_rotation(frame)
        if not self._preview_mode:
            return rotated
        x, y, w, h = self.cropbox
        y2 = min(rotated.shape[0], y + h)
        x2 = min(rotated.shape[1], x + w)
        cropped = rotated[max(0, y):y2, max(0, x):x2]
        if cropped.size == 0:
            return rotated
        return cv2.resize(cropped, (self.target_width, self.target_height))

    def _to_rgb(self, frame: np.ndarray) -> np.ndarray:
        if len(frame.shape) == 2:
            return cv2.cvtColor(frame, cv2.COLOR_GRAY2RGB)
        if frame.shape[2] == 4:
            return cv2.cvtColor(frame, cv2.COLOR_BGRA2RGB)
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    def _refresh_display(self):
        if self.current_frame is not None and self._display_stack.currentIndex() == 0:
            self._display_frame(self.current_frame)
        else:
            self._update_display_geometry(
                self._mpv_widget,
                *self._get_rotated_video_size(),
            )
            self._mpv_widget.update()
        self._update_info_label()

    def _update_display_geometry(self, widget: QWidget, media_w: int, media_h: int):
        if media_w <= 0 or media_h <= 0:
            self.display_scale = 1.0
            self.display_offset_x = 0
            self.display_offset_y = 0
            return
        area = widget.size()
        scale = min(area.width() / media_w, area.height() / media_h)
        shown_w = int(media_w * scale)
        shown_h = int(media_h * scale)
        self.display_scale = scale if scale > 0 else 1.0
        self.display_offset_x = (area.width() - shown_w) // 2
        self.display_offset_y = (area.height() - shown_h) // 2

    def _paint_cropbox(self, widget: QWidget):
        if self._preview_mode or self.video_width <= 0 or self.video_height <= 0:
            return
        rotated_w, rotated_h = self._get_rotated_video_size()
        self._update_display_geometry(widget, rotated_w, rotated_h)
        x, y, w, h = self.cropbox
        painter = QPainter(widget)
        pen = QPen(Qt.GlobalColor.cyan, 2)
        painter.setPen(pen)
        painter.drawRect(
            int(self.display_offset_x + x * self.display_scale),
            int(self.display_offset_y + y * self.display_scale),
            int(w * self.display_scale),
            int(h * self.display_scale),
        )

    def _on_timer_tick(self):
        if not (self._has_video or self._loop_frame is not None):
            return
        self.current_frame_index += 1
        if self.current_frame_index >= max(1, self.total_frames):
            self.current_frame_index = 0
        self.frame_changed.emit(self.current_frame_index)
        self._update_info_label()

    def play(self):
        if self.is_playing or not (self._has_video or self._loop_frame is not None):
            return
        if self._mpv_process is not None:
            self._send_mpv_command(["set_property", "pause", False])
        interval = max(1, round(1000 / max(self.video_fps, 1.0)))
        self.timer.start(interval)
        self.is_playing = True
        self.playback_state_changed.emit(True)

    def pause(self):
        self.timer.stop()
        if self._mpv_process is not None:
            self._send_mpv_command(["set_property", "pause", True])
        was_playing = self.is_playing
        self.is_playing = False
        if was_playing:
            self.playback_state_changed.emit(False)

    def toggle_play(self):
        if self.is_playing:
            self.pause()
        else:
            self.play()

    def next_frame(self):
        if not (self._has_video or self._loop_frame is not None):
            return
        self.pause()
        self.current_frame_index = min(
            self.current_frame_index + 1, max(0, self.total_frames - 1)
        )
        self._seek_mpv_to_current_frame()
        self.frame_changed.emit(self.current_frame_index)
        self._update_info_label()

    def prev_frame(self):
        if not (self._has_video or self._loop_frame is not None):
            return
        self.pause()
        self.current_frame_index = max(self.current_frame_index - 1, 0)
        self._seek_mpv_to_current_frame()
        self.frame_changed.emit(self.current_frame_index)
        self._update_info_label()

    def seek_to_frame(self, index: int):
        if not (self._has_video or self._loop_frame is not None):
            return
        self.pause()
        self.current_frame_index = max(0, min(index, max(0, self.total_frames - 1)))
        self._seek_mpv_to_current_frame()
        self.frame_changed.emit(self.current_frame_index)
        self._update_info_label()

    def _seek_mpv_to_current_frame(self):
        if self._mpv_process is None or self.video_fps <= 0:
            return
        seconds = self.current_frame_index / self.video_fps
        self._send_mpv_command(["seek", seconds, "absolute+exact"])

    def get_current_frame(self) -> int:
        return self.current_frame_index

    def get_cropbox(self) -> Tuple[int, int, int, int]:
        return tuple(self.cropbox)

    def get_cropbox_in_rotated_space(self) -> Tuple[int, int, int, int]:
        return tuple(self.cropbox)

    def get_cropbox_for_export(self) -> Tuple[int, int, int, int]:
        return self._cropbox_to_original_coords(*self.cropbox)

    def set_cropbox(self, x: int, y: int, w: int, h: int):
        self.cropbox = [x, y, w, h]
        self._bound_cropbox()
        self._emit_cropbox_changed()
        self._refresh_display()

    def get_video_info(self) -> Tuple[float, int, int, int]:
        return self.video_fps, self.total_frames, self.video_width, self.video_height

    def set_preview_mode(self, enabled: bool):
        self._preview_mode = enabled
        self._refresh_display()

    def is_preview_mode(self) -> bool:
        return self._preview_mode

    def set_use_gl(self, enabled: bool):
        self._use_gl = False
        if enabled:
            logger.debug("OpenGL preview path is retired for mpv playback")

    def set_rotation(self, degrees: int):
        degrees = degrees % 360
        if self._rotation == degrees:
            return
        self._rotation = degrees
        if self._mpv_process is not None:
            self._send_mpv_command(["set_property", "video-rotate", degrees])
        if self.video_width > 0 and self.video_height > 0:
            self._init_cropbox()
        self.rotation_changed.emit(degrees)
        self._refresh_display()

    def get_rotation(self) -> int:
        return self._rotation

    def set_epconfig(self, config: "EPConfig"):
        self._epconfig = config

    def _get_rotated_video_size(self) -> Tuple[int, int]:
        if self._rotation in (90, 270):
            return self.video_height, self.video_width
        if self._rotation in (0, 180):
            return self.video_width, self.video_height
        import math

        rad = math.radians(self._rotation)
        cos_a = abs(math.cos(rad))
        sin_a = abs(math.sin(rad))
        return (
            int(self.video_width * cos_a + self.video_height * sin_a),
            int(self.video_width * sin_a + self.video_height * cos_a),
        )

    def _apply_rotation(self, frame: np.ndarray) -> np.ndarray:
        return self.apply_rotation_to_frame(frame, self._rotation)

    @staticmethod
    def apply_rotation_to_frame(frame: np.ndarray, rotation: int) -> np.ndarray:
        if rotation == 0:
            return frame
        if not HAS_CV2:
            return frame
        rotation = rotation % 360
        if rotation == 90:
            return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
        if rotation == 180:
            return cv2.rotate(frame, cv2.ROTATE_180)
        if rotation == 270:
            return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
        h, w = frame.shape[:2]
        matrix = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), -rotation, 1.0)
        cos_a = abs(matrix[0, 0])
        sin_a = abs(matrix[0, 1])
        new_w = int(w * cos_a + h * sin_a)
        new_h = int(w * sin_a + h * cos_a)
        matrix[0, 2] += (new_w - w) / 2.0
        matrix[1, 2] += (new_h - h) / 2.0
        return cv2.warpAffine(
            frame,
            matrix,
            (new_w, new_h),
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(0, 0, 0),
        )

    def _cropbox_to_original_coords(
        self, x: int, y: int, w: int, h: int
    ) -> Tuple[int, int, int, int]:
        if self._rotation == 0:
            return x, y, w, h
        if self._rotation == 90:
            return y, self.video_height - x - w, h, w
        if self._rotation == 180:
            return self.video_width - x - w, self.video_height - y - h, w, h
        if self._rotation == 270:
            return self.video_width - y - h, x, h, w
        return x, y, w, h

    def _display_to_rotated_coords(self, widget: QWidget, pos: QPoint) -> Tuple[int, int]:
        rotated_w, rotated_h = self._get_rotated_video_size()
        self._update_display_geometry(widget, rotated_w, rotated_h)
        x = int((pos.x() - self.display_offset_x) / max(self.display_scale, 1e-6))
        y = int((pos.y() - self.display_offset_y) / max(self.display_scale, 1e-6))
        return max(0, x), max(0, y)

    def _get_drag_mode(self, vx: int, vy: int) -> int:
        x, y, w, h = self.cropbox
        hs = self.handle_size
        if abs(vx - x) < hs and abs(vy - y) < hs:
            return self.DRAG_RESIZE_TL
        if abs(vx - (x + w)) < hs and abs(vy - y) < hs:
            return self.DRAG_RESIZE_TR
        if abs(vx - x) < hs and abs(vy - (y + h)) < hs:
            return self.DRAG_RESIZE_BL
        if abs(vx - (x + w)) < hs and abs(vy - (y + h)) < hs:
            return self.DRAG_RESIZE_BR
        if x <= vx <= x + w and y <= vy <= y + h:
            return self.DRAG_MOVE
        return self.DRAG_NONE

    def _handle_mouse_press(self, widget: QWidget, event: QMouseEvent):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        vx, vy = self._display_to_rotated_coords(widget, event.pos())
        self.drag_mode = self._get_drag_mode(vx, vy)
        if self.drag_mode != self.DRAG_NONE:
            self.drag_start_pos = event.pos()
            self.drag_start_cropbox = self.cropbox.copy()
            self.setFocus()

    def _handle_mouse_move(self, widget: QWidget, event: QMouseEvent):
        if self.drag_mode == self.DRAG_NONE or self.drag_start_pos is None:
            vx, vy = self._display_to_rotated_coords(widget, event.pos())
            mode = self._get_drag_mode(vx, vy)
            cursors = {
                self.DRAG_RESIZE_TL: Qt.CursorShape.SizeFDiagCursor,
                self.DRAG_RESIZE_BR: Qt.CursorShape.SizeFDiagCursor,
                self.DRAG_RESIZE_TR: Qt.CursorShape.SizeBDiagCursor,
                self.DRAG_RESIZE_BL: Qt.CursorShape.SizeBDiagCursor,
                self.DRAG_MOVE: Qt.CursorShape.SizeAllCursor,
            }
            widget.setCursor(cursors.get(mode, Qt.CursorShape.ArrowCursor))
            return

        crx, cry = self._display_to_rotated_coords(widget, event.pos())
        srx, sry = self._display_to_rotated_coords(widget, self.drag_start_pos)
        dx, dy = crx - srx, cry - sry
        sx, sy, sw, sh = self.drag_start_cropbox
        if self.drag_mode == self.DRAG_MOVE:
            self.cropbox = [sx + dx, sy + dy, sw, sh]
        elif self.drag_mode == self.DRAG_RESIZE_BR:
            new_w = max(1, sw + dx)
            self.cropbox = [sx, sy, new_w, int(new_w / self.target_aspect_ratio)]
        elif self.drag_mode == self.DRAG_RESIZE_TL:
            new_w = max(1, sw - dx)
            new_h = int(new_w / self.target_aspect_ratio)
            self.cropbox = [sx + sw - new_w, sy + sh - new_h, new_w, new_h]
        elif self.drag_mode == self.DRAG_RESIZE_TR:
            new_w = max(1, sw + dx)
            new_h = int(new_w / self.target_aspect_ratio)
            self.cropbox = [sx, sy + sh - new_h, new_w, new_h]
        elif self.drag_mode == self.DRAG_RESIZE_BL:
            new_w = max(1, sw - dx)
            new_h = int(new_w / self.target_aspect_ratio)
            self.cropbox = [sx + sw - new_w, sy, new_w, new_h]
        self._bound_cropbox()
        self._emit_cropbox_changed()
        self._refresh_display()

    def _handle_mouse_release(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_mode = self.DRAG_NONE
            self.drag_start_pos = None

    def keyPressEvent(self, event: QKeyEvent):
        if not (self._has_video or self.current_frame is not None):
            super().keyPressEvent(event)
            return
        has_modifier = event.modifiers() != Qt.KeyboardModifier.NoModifier
        key = event.key()
        if key == Qt.Key.Key_Space and not has_modifier and self._has_video:
            self.toggle_play()
        elif key == Qt.Key.Key_Left and not has_modifier and self._has_video:
            self.prev_frame()
        elif key == Qt.Key.Key_Right and not has_modifier and self._has_video:
            self.next_frame()
        elif key == Qt.Key.Key_W and not has_modifier:
            self.cropbox[1] -= 10
        elif key == Qt.Key.Key_S and not has_modifier:
            self.cropbox[1] += 10
        elif key == Qt.Key.Key_A and not has_modifier:
            self.cropbox[0] -= 10
        elif key == Qt.Key.Key_D and not has_modifier:
            self.cropbox[0] += 10
        else:
            super().keyPressEvent(event)
            return
        self._bound_cropbox()
        self._emit_cropbox_changed()
        self._refresh_display()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_display()

    def closeEvent(self, event):
        self.clear()
        super().closeEvent(event)

    def clear(self):
        self.pause()
        self._stop_reader_thread()
        self._loop_frame = None
        self.video_path = ""
        self.total_frames = 0
        self.current_frame_index = 0
        self.video_width = 0
        self.video_height = 0
        self.current_frame = None
        self._display_stack.setCurrentIndex(0)
        self.video_label.clear()
        self.video_label.setText("No media loaded")
        self.cropbox = [0, 0, 0, 0]
        self._update_info_label()
