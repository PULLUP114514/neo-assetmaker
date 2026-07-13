'''USB管理器页'''
from __future__ import annotations
from datetime import datetime
import json
import logging
import os
import tempfile
from urllib.parse import urlsplit
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont, QPixmap
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidgetItem,
    QMessageBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
    QInputDialog
)
from qfluentwidgets import (
    CaptionLabel,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    ListWidget,
    PlainTextEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    SimpleCardWidget,
    StrongBodyLabel,
    SubtitleLabel,
    setCustomStyleSheet,
)

from core.rndis_device_service import DEFAULT_BASE_URL, DEFAULT_DEVICE_HOST
from core.remote_asset_manager import RemoteAssetManager
from gui.texts import remote_user_error
from gui.workers.usb_workers import (
    UsbConnectWorker,
    UsbDeleteAssetWorker,
    UsbDownloadAssetWorker,
    UsbListOperatorsWorker,
    UsbReloadAssetsWorker,
    UsbRestartDrmWorker,
    UsbUploadAssetWorker,
)
import usb.core
import usb.util
from core.usb_control import UsbResponderClient


class UsbAssetListItemWidget(QWidget):
    """USB remote asset list item with thumbnail and action buttons."""

    def __init__(self, asset_data: dict, parent_page=None):
        super().__init__()
        self.asset_data = asset_data
        self.parent_page = parent_page

        # Thumbnail — try to load the downloaded icon, fall back to placeholder
        self.thumbnail_label = QLabel()
        self.thumbnail_label.setFixedSize(64, 64)
        self.thumbnail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail_label.setScaledContents(True)
        local_icon = asset_data.get("local_icon", "")
        if local_icon and os.path.isfile(local_icon):
            pixmap = QPixmap(local_icon)
            if not pixmap.isNull():
                self.thumbnail_label.setPixmap(
                    pixmap.scaled(64, 64, Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation)
                )
            else:
                self.thumbnail_label.setText("素材")
        else:
            self.thumbnail_label.setText("素材")
        self.thumbnail_label.setStyleSheet(
            "border: 1px solid #ccc; border-radius: 4px; color: #777;"
        )

        self.name_label = CaptionLabel(asset_data.get("name", "Unnamed"))
        uuid = asset_data.get("uuid", "")
        desc = asset_data.get("description", "")
        path = asset_data.get("path", "")
        info_text = f"UUID: {uuid}" if uuid else ""
        if desc:
            info_text += f"\n{desc}" if info_text else desc
        if path:
            info_text += f"\nPath: {path}" if info_text else path
        self.info_label = CaptionLabel(info_text)
        self.info_label.setWordWrap(True)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(3)
        text_layout.addWidget(self.name_label)
        text_layout.addWidget(self.info_label)

        self.btn_delete = PushButton("删除")
        self.btn_delete.setIcon(FluentIcon.DELETE)
        self.btn_download = PushButton("下载")
        self.btn_download.setIcon(FluentIcon.DOWNLOAD)

        action_layout = QVBoxLayout()
        action_layout.setSpacing(4)
        action_layout.addWidget(self.btn_delete)
        action_layout.addWidget(self.btn_download)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)
        layout.addWidget(self.thumbnail_label,
                         alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(text_layout, stretch=1)
        layout.addLayout(action_layout)

        self.btn_delete.clicked.connect(
            lambda: self.parent_page._on_delete_for_asset(self.asset_data)
        )
        self.btn_download.clicked.connect(
            lambda: self.parent_page._on_download_for_asset(self.asset_data)
        )

    def set_buttons_enabled(self, enabled: bool):
        self.btn_delete.setEnabled(enabled)
        self.btn_download.setEnabled(enabled)


class UsbControlPage(QWidget):
    setting_changed = pyqtSignal(str, object)
    usbDisconnected = pyqtSignal(object)

    def __init__(self, parent):
        super().__init__(parent)
        self._parent = parent
        self._settings: dict = {}
        self._is_busy = False
        self._is_connected = False

        # Worker references
        self._connect_worker = None
        self._list_worker = None
        self._upload_worker = None
        self._download_worker = None
        self._delete_worker = None
        self._restart_worker = None
        self._reload_worker = None

        # Temp directory for operator preview icons
        self._temp_dir = tempfile.mkdtemp(prefix="usb_op_icons_")

        self._init_ui()
        self._connect_signals()
        self._set_busy(False)
        self.VID = self._settings.get("usb_controler_vid")
        self.PID = self._settings.get("usb_controler_pid")
        self.enable_restart = self._settings.get(
            'usb_controler_auto_restart_program')

    def _init_ui(self):
        '''初始化UI'''
        self.mainLayout = QVBoxLayout(self)
        self.mainLayout.setContentsMargins(0, 15, 0, 0)
        self.mainLayout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.titleLabel = SubtitleLabel("EPass USB管理器模式", self)
        self.titleLabel.setContentsMargins(30, 0, 0, 0)
        self.mainLayout.addWidget(self.titleLabel)
        self.mainLayout.addSpacing(10)

        # 三列
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setContentsMargins(10, 0, 10, 10)
        self._build_left_panel()
        self._build_middle_panel()
        # self._build_right_panel()
        self.splitter.addWidget(self.leftPanel)
        self.splitter.addWidget(self.middlePanel)
        # self.splitter.addWidget(self.rightPanel)
        self.splitter.setSizes([250, 560, 300])
        self.splitter.setStretchFactor(1, 3)
        self.mainLayout.addWidget(self.splitter, 1)

        # 底部
        self.connectionStatusLabel = CaptionLabel("未连接")
        self.progressBar = ProgressBar()
        self.progressBar.setVisible(False)
        self.progressLabel = CaptionLabel(" ")
        self.progressLabel.setWordWrap(True)
        wrapper = QVBoxLayout()
        wrapper.setContentsMargins(10, 0, 10, 0)
        wrapper.addWidget(self.connectionStatusLabel)
        wrapper.addWidget(self.progressBar)
        wrapper.addWidget(self.progressLabel)
        self.mainLayout.addLayout(wrapper)

    def _build_left_panel(self):
        '''左列构建'''
        self.leftPanel = SimpleCardWidget()
        self.leftPanel.setMinimumWidth(230)
        self.leftPanel.setMaximumWidth(300)

        layout = QVBoxLayout(self.leftPanel)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.btnConnect = PrimaryPushButton("连接设备")
        self.btnConnect.setIcon(FluentIcon.WIFI)
        layout.addWidget(self.btnConnect)

        self.btnRefreshList = PushButton("刷新素材")
        self.btnRefreshList.setIcon(FluentIcon.SYNC)
        layout.addWidget(self.btnRefreshList)

        self.btnUploadLocal = PushButton("上传素材")
        self.btnUploadLocal.setIcon(FluentIcon.SEND)
        layout.addWidget(self.btnUploadLocal)

        self.btnRestartDrm = PushButton("重启 DrmApp")
        self.btnRestartDrm.setIcon(FluentIcon.UPDATE)
        layout.addWidget(self.btnRestartDrm)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(line)

        self.networkHintLabel = CaptionLabel(
            "若要使用此功能，请确保：\n1、菜单 -> 设备 -> 版本号 a2.7及以上\n2、菜单 -> 设置 -> USB模式：管理器APP"
        )
        self.networkHintLabel.setWordWrap(True)
        layout.addWidget(self.networkHintLabel)

    def _build_middle_panel(self):
        '''中列构建'''
        self.middlePanel = SimpleCardWidget()
        layout = QVBoxLayout(self.middlePanel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.middleTitleLabel = CaptionLabel("远程素材")
        layout.addWidget(self.middleTitleLabel)

        self.assetDetailList = ListWidget()
        self.assetDetailList.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        setCustomStyleSheet(
            self.assetDetailList,
            "ListWidget { border: none; background: transparent; }",
            "ListWidget { border: none; background: transparent; }",
        )
        layout.addWidget(self.assetDetailList, stretch=1)

    def _connect_signals(self):
        # UI 对象信号
        self.btnConnect.clicked.connect(self._on_connect)
        self.btnRefreshList.clicked.connect(self._on_refresh_list)
        self.btnUploadLocal.clicked.connect(self._on_upload_local)
        self.btnRestartDrm.clicked.connect(self._on_restart_drm)

        # UI操作信号
        self.usbDisconnected.connect(self._on_disconnect)

    # ------------------------------------------------------------------
    # Connect / disconnect
    # ------------------------------------------------------------------

    def _on_connect(self):
        self.VID = int(self._settings.get("usb_controler_vid"), 16)
        self.PID = int(self._settings.get("usb_controler_pid"), 16)
        """连接click"""
        if (self._is_connected == True):
            self._on_manually_disconnect()
            return
        self._set_busy(True)
        self._update_connection_ui("Connecting")

        # 枚举设备 (UI线程，通常很快)
        usbDeviceList = []
        for dev in usb.core.find(find_all=True):
            if dev.idVendor == self.VID and dev.idProduct == self.PID:
                usbDeviceList.append(dev)

        usbNumberCounter = len(usbDeviceList)

        # 没有设备
        if usbNumberCounter == 0:
            InfoBar.error(
                "连接失败 无设备",
                f"找不到： VID {hex(self.VID)},PID {hex(self.PID)}",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=6000,
            )
            self._update_connection_ui("Disconnected")
            return

        if usbNumberCounter == 1:
            # 只有一个设备
            dev = usbDeviceList[0]
        else:
            # 多个设备，让用户选择
            items = [
                f"Bus {dev.bus}  Address {dev.address}  VID {hex(dev.idVendor)}  PID {hex(dev.idProduct)}"
                for dev in usbDeviceList
            ]

            item, ok = QInputDialog.getItem(
                self,
                "选择USB设备",
                "请选择需要连接的设备：",
                items,
                0,
                False
            )
            if not ok:
                self._update_connection_ui("Disconnected")
                return
            index = items.index(item)
            dev = usbDeviceList[index]

        # 在后台线程中创建 UsbResponderClient 并握手，避免阻塞 UI
        self._connect_worker = UsbConnectWorker(
            vid=dev.idVendor,
            pid=dev.idProduct,
            bus=dev.bus,
            address=dev.address,
            interface=0,
            timeout_ms=30000,
            disconnect_callback=self.usbDisconnected.emit,
            parent=self,
        )
        self._connect_worker.connect_succeeded.connect(
            self._on_connect_success)
        self._connect_worker.connect_failed.connect(self._on_connect_fail)
        self._connect_worker.start()

    def _on_connect_success(self, usbRC, kv: dict):
        """连接成功（回调在 UI 线程）"""
        self.usbRC = usbRC
        temp_string_builder = ""
        for k in sorted(kv.keys()):
            temp_string_builder += f"{k}={kv[k]}\n"

        # 从 usbRC 中获取设备的 bus/address 信息来显示
        InfoBar.success(
            "连接成功",
            f"已连接到设备\n{temp_string_builder}",
            parent=self,
            position=InfoBarPosition.TOP,
            duration=6000,
        )
        self._update_connection_ui("Connected")
        self._on_refresh_list()

    def _on_connect_fail(self, error):
        """连接失败（回调在 UI 线程）"""
        self._on_disconnect(error)

    def _on_disconnect(self, error=None):
        '''断开连接   （超级无敌霹雳大兜底()()()()）'''
        need_disconnect = False

        if isinstance(error, usb.core.USBError):
            title = "USB通信失败"
            message_builder = f"USB设备通信异常：{error}"
        elif isinstance(error, TimeoutError):
            title = "USB通信超时"
            message_builder = "USB设备响应超时，请稍后重试"
        elif isinstance(error, RuntimeError):
            title = "USB协议错误"
            message_builder = str(error) or "USB协议异常"
        elif isinstance(error, (FileNotFoundError, OSError)):
            title = "USB文件操作失败"
            message_builder = str(error) or "文件操作失败"
        else:
            title = "USB操作失败"
            message_builder = str(error) if error else "设备断开连接"
            need_disconnect = True
        if (need_disconnect):
            try:
                self.usbRC.close()
            except:
                '''ignore'''
            self.usbRC = None
            self._update_connection_ui("Disconnected")
        InfoBar.error(
            title,
            message_builder,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=6000,
        )

    def _on_manually_disconnect(self):
        '''手动断开连接'''
        if (self.usbRC != None):
            try:
                self.usbRC.close()
            except Exception as ex:
                InfoBar.error(
                    "断开失败",
                    str(ex),
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=6000,
                )
                return
            self.usbRC = None
            self._update_connection_ui("Disconnected")

    # ------------------------------------------------------------------
    # Refresh asset list
    # ------------------------------------------------------------------

    def _on_refresh_list(self):
        '''刷新列表 — 通过 epassctl 获取干员信息及预览图标'''
        if self._is_busy or not self._is_connected:
            return
        self._set_busy(True)

        self._list_worker = UsbListOperatorsWorker(
            self.usbRC, self._temp_dir, parent=self
        )
        self._list_worker.progress_updated.connect(self._on_task_progress)
        self._list_worker.list_completed.connect(self._on_list_loaded)
        self._list_worker.list_failed.connect(self._on_list_failed)
        self._list_worker.start()

    def _on_list_loaded(self, operators: list):
        """干员列表加载完成"""
        self.assetDetailList.clear()
        if not operators:
            placeholder = QListWidgetItem("设备端暂未返回干员信息。")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.assetDetailList.addItem(placeholder)
        else:
            for op in operators:
                widget = UsbAssetListItemWidget(op, parent_page=self)
                list_item = QListWidgetItem(self.assetDetailList)
                list_item.setSizeHint(widget.sizeHint())
                self.assetDetailList.addItem(list_item)
                self.assetDetailList.setItemWidget(list_item, widget)
        self._set_busy(False)

    def _on_list_failed(self, error):
        """素材列表加载失败"""
        self._set_busy(False)
        InfoBar.error(
            "刷新失败",
            str(error),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=5000,
        )

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    def _on_upload_local(self):
        '''本地上传 — 读取 epconfig.json 获取 uuid，上传到 /assets/{uuid}'''
        if self._is_busy or not self._is_connected:
            return
        path = QFileDialog.getExistingDirectory(self, "选择素材目录", "")
        if not path:
            return

        # 读取 epconfig.json 获取 uuid
        epconfig_path = os.path.join(path, "epconfig.json")
        try:
            with open(epconfig_path, "r", encoding="utf-8") as f:
                epconfig = json.load(f)
        except Exception as ex:
            InfoBar.error(
                "上传失败",
                f"无法读取 epconfig.json：{ex}",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )
            return

        uuid = (epconfig.get("uuid") or "").strip()
        if not uuid:
            InfoBar.error(
                "上传失败",
                "epconfig.json 中缺少 uuid 字段或为空",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )
            return

        self._set_busy(True)
        self.progressBar.setVisible(True)
        self.progressBar.setValue(0)
        self.progressLabel.setText("正在上传...")

        remote_path = f"/assets/{uuid}"
        self._upload_worker = UsbUploadAssetWorker(
            self.usbRC, path, remote_path, parent=self
        )
        self._upload_worker.progress_updated.connect(self._on_task_progress)
        self._upload_worker.upload_completed.connect(self._on_upload_done)
        self._upload_worker.upload_failed.connect(self._on_upload_failed)
        self._upload_worker.start()

    def _on_upload_done(self, remote_path: str):
        """上传完成 → reload_assets → 刷新列表"""
        self.progressBar.setVisible(False)
        self.progressLabel.setText("正在重载资产...")
        InfoBar.success(
            "上传完成",
            f"已上传至 {remote_path}",
            parent=self,
            position=InfoBarPosition.TOP,
            duration=4000,
        )
        self._reload_assets_and_refresh()

    def _on_upload_failed(self, error):
        """上传失败"""
        self.progressBar.setVisible(False)
        self.progressLabel.setText("")
        self._set_busy(False)
        InfoBar.error(
            "上传失败",
            str(error),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=5000,
        )

    def _on_task_progress(self, percent: int, message: str):
        """更新进度条"""
        self.progressBar.setVisible(True)
        self.progressBar.setValue(percent)
        self.progressLabel.setText(message)

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def _on_delete_for_asset(self, asset_data: dict):
        """删除远程素材"""
        if self._is_busy or not self._is_connected:
            return
        name = asset_data.get("name", "")
        path = asset_data.get("path")
        reply = QMessageBox.question(
            self,
            "确认删除",
            f'删除远程素材"{name}"？\n路径: {path}\n此操作不可撤销。',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._set_busy(True)
        self._delete_worker = UsbDeleteAssetWorker(
            self.usbRC, path, parent=self
        )
        self._delete_worker.delete_completed.connect(self._on_delete_done)
        self._delete_worker.delete_failed.connect(self._on_delete_failed)
        self._delete_worker.start()

    def _on_delete_done(self, path: str):
        """删除完成 → reload_assets → 刷新列表"""
        self.progressLabel.setText("正在重载资产...")
        InfoBar.success(
            "已删除",
            f"已删除 {path}",
            parent=self,
            position=InfoBarPosition.TOP,
            duration=3000,
        )
        self._reload_assets_and_refresh()

    def _on_delete_failed(self, error):
        """删除失败"""
        self._set_busy(False)
        InfoBar.error(
            "删除失败",
            str(error),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=5000,
        )

    # ------------------------------------------------------------------
    # Reload assets (shared by upload & delete)
    # ------------------------------------------------------------------

    def _reload_assets_and_refresh(self):
        """执行 reload_assets，完成后刷新列表。保持 busy 状态直到刷新结束。"""
        self._reload_worker = UsbReloadAssetsWorker(self.usbRC, parent=self)
        self._reload_worker.reload_succeeded.connect(self._on_reload_done)
        self._reload_worker.reload_failed.connect(self._on_reload_failed)
        self._reload_worker.start()

    def _on_reload_done(self):
        """reload 成功，释放 busy 并刷新列表"""
        self._set_busy(False)
        self._on_refresh_list()

    def _on_reload_failed(self, error):
        """reload 失败，仍然刷新列表（设备可能不支持 reload）"""
        self._set_busy(False)
        InfoBar.warning(
            "重载失败",
            str(error),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=5000,
        )
        self._on_refresh_list()

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def _on_download_for_asset(self, asset_data: dict):
        """下载远程素材"""
        if self._is_busy or not self._is_connected:
            return
        name = asset_data.get("name", "")
        path = asset_data.get("path", "")
        if not path:
            return
        save_dir = QFileDialog.getExistingDirectory(self, "选择保存目录")
        if not save_dir:
            return
        local_path = os.path.join(save_dir, name)
        self._set_busy(True)
        self.progressBar.setVisible(True)
        self.progressBar.setValue(0)
        self.progressLabel.setText(f"正在下载: {name}")
        self._download_worker = UsbDownloadAssetWorker(
            self.usbRC, path, local_path, parent=self
        )
        self._download_worker.download_completed.connect(
            self._on_download_done)
        self._download_worker.download_failed.connect(self._on_download_failed)
        self._download_worker.start()

    def _on_download_done(self, local_path: str):
        """下载完成"""
        self.progressBar.setVisible(False)
        self.progressLabel.setText("")
        self._set_busy(False)
        InfoBar.success(
            "下载完成",
            f"已保存到 {local_path}",
            parent=self,
            position=InfoBarPosition.TOP,
            duration=5000,
        )

    def _on_download_failed(self, error):
        """下载失败"""
        self.progressBar.setVisible(False)
        self.progressLabel.setText("")
        self._set_busy(False)
        InfoBar.error(
            "下载失败",
            str(error),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=5000,
        )

    # ------------------------------------------------------------------
    # Restart DRM
    # ------------------------------------------------------------------

    def _on_restart_drm(self):
        '''重启DRM'''
        if self._is_busy or not self._is_connected:
            return
        self._set_busy(True)

        self._restart_worker = UsbRestartDrmWorker(
            self.usbRC, parent=self
        )
        self._restart_worker.restart_succeeded.connect(self._on_restart_done)
        self._restart_worker.restart_failed.connect(self._on_restart_failed)
        self._restart_worker.start()

    def _on_restart_done(self):
        """重启DRM完成"""
        self._set_busy(False)
        InfoBar.success(
            "已请求重启",
            "已向设备发送 DrmApp 重启请求。",
            parent=self,
            position=InfoBarPosition.TOP,
            duration=3000,
        )

    def _on_restart_failed(self, error):
        """重启DRM失败"""
        self._set_busy(False)
        InfoBar.error(
            "重启失败",
            str(error),
            parent=self,
            position=InfoBarPosition.TOP,
            duration=5000,
        )

    # ------------------------------------------------------------------
    # Settings & UI helpers
    # ------------------------------------------------------------------

    def load_settings(self, settings: dict):
        self._settings = settings.copy()

    def _update_connection_ui(self, state: str, success_str=None):
        if state == "Connected":
            self._is_connected = True
            self.connectionStatusLabel.setText(
                f"已连接设备：{success_str}" if success_str else "已连接设备"
            )
            self.btnConnect.setText("断开连接")
            self._is_busy = False
        elif state == "Connecting":
            self.connectionStatusLabel.setText("正在连接 USB 设备...")
            self.btnConnect.setText("连接中...")
            self._is_busy = True
        elif state == "Disconnected":
            self._is_connected = False
            self.connectionStatusLabel.setText("连接失败")
            self.btnConnect.setText("连接设备")
            self._is_busy = False
        self._set_busy(self._is_busy)

    def _set_busy(self, busy: bool):
        self._is_busy = busy
        self.btnConnect.setEnabled(not (busy and self._is_connected))
        self.btnRefreshList.setEnabled(not busy and self._is_connected)
        self.btnUploadLocal.setEnabled(not busy and self._is_connected)
        self.btnRestartDrm.setEnabled(not busy and self._is_connected)
        for i in range(self.assetDetailList.count()):
            item = self.assetDetailList.item(i)
            widget = self.assetDetailList.itemWidget(item)
            if widget:
                widget.set_buttons_enabled(not busy)

    def shutdown(self):
        """等待所有后台工作线程结束"""
        for worker in [
            self._connect_worker,
            self._list_worker,
            self._upload_worker,
            self._download_worker,
            self._delete_worker,
            self._restart_worker,
            self._reload_worker,
        ]:
            if worker and worker.isRunning():
                worker.wait(3000)
        # Clean up temp icon directory
        if self._temp_dir and os.path.isdir(self._temp_dir):
            try:
                import shutil
                shutil.rmtree(self._temp_dir)
            except Exception:
                pass
