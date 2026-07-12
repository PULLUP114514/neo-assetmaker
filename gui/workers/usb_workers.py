"""Qt workers for EPass USB bulk-transfer remote management."""

from __future__ import annotations

import json
import logging
import os
from typing import Callable, Optional

from PyQt6.QtCore import QThread, pyqtSignal

from core.usb_control import UsbResponderClient

logger = logging.getLogger(__name__)


class UsbConnectWorker(QThread):
    """Create a UsbResponderClient and perform the devinfo handshake."""

    connect_succeeded = pyqtSignal(object, dict)  # usbRC, device_info dict
    connect_failed = pyqtSignal(object)  # exception

    def __init__(
        self,
        vid: int,
        pid: int,
        bus: int,
        address: int,
        interface: int = 0,
        timeout_ms: int = 3000,
        disconnect_callback: Optional[Callable] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._vid = vid
        self._pid = pid
        self._bus = bus
        self._address = address
        self._interface = interface
        self._timeout_ms = timeout_ms
        self._disconnect_callback = disconnect_callback

    def run(self):
        try:
            usbRC = UsbResponderClient(
                vid=self._vid,
                pid=self._pid,
                bus=self._bus,
                address=self._address,
                interface=self._interface,
                timeout_ms=self._timeout_ms,
                disconnect_callback=self._disconnect_callback,
            )
            kv = usbRC.devinfo()
            self.connect_succeeded.emit(usbRC, kv)
        except Exception as ex:
            logger.exception("USB connect failed")
            self.connect_failed.emit(ex)


class UsbListAssetsWorker(QThread):
    """List remote files and directories over USB."""

    list_completed = pyqtSignal(list, list)  # files, dirs
    list_failed = pyqtSignal(object)  # exception

    def __init__(self, usbRC: UsbResponderClient, path: str = ".", parent=None):
        super().__init__(parent)
        self._usbRC = usbRC
        self._path = path

    def run(self):
        try:
            files, dirs = self._usbRC.file_list(self._path)
            self.list_completed.emit(files, dirs)
        except Exception as ex:
            logger.exception("USB list assets failed")
            self.list_failed.emit(ex)


class UsbListOperatorsWorker(QThread):
    """Query operator info via epassctl and download preview icons over USB."""

    list_completed = pyqtSignal(list)  # list of operator data dicts
    list_failed = pyqtSignal(object)  # exception
    progress_updated = pyqtSignal(int, str)

    def __init__(
        self,
        usbRC: UsbResponderClient,
        temp_dir: str,
        parent=None,
    ):
        super().__init__(parent)
        self._usbRC = usbRC
        self._temp_dir = temp_dir

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        """从混合输出（C 错误前缀 + JSON）中提取并解析 JSON 对象。

        epassctl 失败时 stderr/stdout 可能混入 ANSI 转义序列和 C 源码路径，
        JSON 位于末尾，以 ``{`` 开始 ``}`` 结束。
        """
        if not text:
            return None
        # 找到最后一个 JSON 对象的起始位置
        start = text.rfind("{")
        if start == -1:
            return None
        end = text.rfind("}")
        if end == -1 or end <= start:
            return None
        json_str = text[start:end + 1]
        try:
            return json.loads(json_str)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def run(self):
        operators: list = []
        i = 0
        while True:
            try:
                result = self._usbRC.command_exec(
                    f"epassctl json prts get_operator_info {i}",
                    max_stdout=8192,
                )
            except Exception:
                # Exception means no more operators or connection lost
                break

            if not result.stdout:
                break

            raw = result.stdout.decode("utf-8", errors="replace")
            info = self._extract_json(raw)
            if info is None:
                break

            # 请求失败（如索引超出范围）→ 停止迭代
            if info.get("status") == "error":
                break

            operator_name = info.get("operator_name", f"Operator {i}")
            self.progress_updated.emit(0, f"加载: {operator_name}")

            # 解包ICONPATH
            icon_path_raw = info.get("icon_path", "")
            remote_icon = icon_path_raw
            if remote_icon and remote_icon.startswith("A:"):
                remote_icon = remote_icon[2:]  # strip "A:" prefix

            # 下载ICON
            local_icon = ""
            if remote_icon:
                local_icon = os.path.join(self._temp_dir, f"op_{i}_icon.png")
                try:
                    self._usbRC.file_get(remote_icon, local_icon)
                except Exception:
                    logger.warning("Failed to download icon: %s", remote_icon)
                    local_icon = ""  # use placeholder on failure

            # delete_path: the icon file path on device
            delete_path = remote_icon

            operators.append({
                "name": operator_name,
                "uuid": uuid,
                "description": info.get("description", ""),
                "path": remote_icon,        # for download button
                # for delete button (the icon file)
                "delete_path": delete_path,
                "local_icon": local_icon,    # local preview thumbnail
                "operator_index": i,
                "is_dir": False,
            })

            i += 1

        self.list_completed.emit(operators)


class UsbUploadAssetWorker(QThread):
    """Upload a local file or directory tree over USB."""

    progress_updated = pyqtSignal(int, str)
    upload_completed = pyqtSignal(str)
    upload_failed = pyqtSignal(object)

    def __init__(
        self,
        usbRC: UsbResponderClient,
        local_path: str,
        remote_path: str,
        chunk_size: int = 16 * 1024 - 4,
        parent=None,
    ):
        super().__init__(parent)
        self._usbRC = usbRC
        self._local_path = local_path
        self._remote_path = remote_path.rstrip("/")
        self._chunk_size = chunk_size

    def run(self):
        try:
            if os.path.isfile(self._local_path):
                self._upload_file(self._local_path, self._remote_path)
            else:
                self._upload_directory()
            self.upload_completed.emit(self._remote_path)
        except Exception as ex:
            logger.exception("USB upload failed")
            self.upload_failed.emit(ex)

    def _upload_file(self, local: str, remote: str):
        self._usbRC.file_put(local, remote, chunk_size=self._chunk_size)

    def _upload_directory(self):
        # Collect all files and directories first so we can report progress
        entries: list[tuple[str, str, bool]] = []  # (local, remote, is_dir)
        local_base = os.path.normpath(self._local_path)
        for dirpath, dirnames, filenames in os.walk(local_base):
            rel = os.path.relpath(dirpath, local_base)
            if rel == ".":
                remote_dir = self._remote_path
            else:
                remote_dir = self._remote_path + "/" + rel.replace("\\", "/")
            for name in dirnames:
                entries.append((
                    os.path.join(dirpath, name),
                    remote_dir + "/" + name,
                    True,
                ))
            for name in filenames:
                entries.append((
                    os.path.join(dirpath, name),
                    remote_dir + "/" + name,
                    False,
                ))

        total = len(entries)
        if total == 0:
            return

        for idx, (local, remote, is_dir) in enumerate(entries):
            pct = int((idx + 1) / total * 100)
            if is_dir:
                self.progress_updated.emit(pct, f"创建目录: {remote}")
                self._usbRC.dir_mkdir(remote, parents=True)
            else:
                self.progress_updated.emit(
                    pct, f"上传: {os.path.basename(local)}")
                self._usbRC.file_put(
                    local, remote, chunk_size=self._chunk_size)


class UsbDownloadAssetWorker(QThread):
    """Download a remote file over USB."""

    progress_updated = pyqtSignal(int, str)
    download_completed = pyqtSignal(str)
    download_failed = pyqtSignal(object)

    def __init__(
        self,
        usbRC: UsbResponderClient,
        remote_path: str,
        local_path: str,
        parent=None,
    ):
        super().__init__(parent)
        self._usbRC = usbRC
        self._remote_path = remote_path
        self._local_path = local_path

    def run(self):
        try:
            self._usbRC.file_get(self._remote_path, self._local_path)
            self.download_completed.emit(self._local_path)
        except Exception as ex:
            logger.exception("USB download failed")
            self.download_failed.emit(ex)


class UsbDeleteAssetWorker(QThread):
    """Delete a remote file or directory over USB."""

    delete_completed = pyqtSignal(str)
    delete_failed = pyqtSignal(object)

    def __init__(
        self,
        usbRC: UsbResponderClient,
        remote_path: str,
        parent=None,
    ):
        super().__init__(parent)
        self._usbRC = usbRC
        self._remote_path = remote_path

    def run(self):
        try:
            self._usbRC.file_delete(self._remote_path)
            self.delete_completed.emit(self._remote_path)
        except Exception as ex:
            logger.exception("USB delete failed")
            self.delete_failed.emit(ex)


class UsbRestartDrmWorker(QThread):
    """Restart DrmApp on the device over USB."""

    restart_succeeded = pyqtSignal()
    restart_failed = pyqtSignal(object)

    def __init__(
        self,
        usbRC: UsbResponderClient,
        command: str = "restart_drm",
        parent=None,
    ):
        super().__init__(parent)
        self._usbRC = usbRC
        self._command = command

    def run(self):
        try:
            self._usbRC.command_exec(self._command)
            self.restart_succeeded.emit()
        except Exception as ex:
            logger.exception("USB restart DRM failed")
            self.restart_failed.emit(ex)


class UsbReloadAssetsWorker(QThread):
    """Reload assets on the device after upload/delete over USB."""

    reload_succeeded = pyqtSignal()
    reload_failed = pyqtSignal(object)

    def __init__(self, usbRC: UsbResponderClient, parent=None):
        super().__init__(parent)
        self._usbRC = usbRC

    def run(self):
        try:
            self._usbRC.command_exec("epassctl prts reload_assets")
            self.reload_succeeded.emit()
        except Exception as ex:
            logger.exception("USB reload assets failed")
            self.reload_failed.emit(ex)
