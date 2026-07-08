'''USB管理器页'''
from __future__ import annotations
from datetime import datetime
import logging
import os
import tempfile
from urllib.parse import urlsplit
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
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
from gui.workers.rndis_http_workers import (
    HttpDeleteAssetWorker,
    HttpDownloadAssetWorker,
    HttpListAssetsWorker,
    HttpRestartDrmWorker,
    HttpUploadAssetWorker,
    RndisConnectWorker,
)
import usb.core
import usb.util
from core.usb_control import UsbResponderClient


class UsbControlPage(QWidget):
    setting_changed = pyqtSignal(str, object)

    def __init__(self, parent):
        super().__init__(parent)
        self._parent = parent
        self._settings: dict = {}
        self._is_busy = False
        self._is_connected = False
        self._init_ui()
        self._connect_signals()
        self._set_busy(False)
        self.VID = self._settings.get("usb_controler_vid")
        self.PID = self._settings.get("usb_controler_pid")
        self.enable_restart = self._settings.get(
            'usb_controler_auto_restart_program')
        # self._update_connection_ui("disconnected")

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
            "若要使用此功能，请确保：\n1、菜单 -> 设备 -> 版本号 为a2.7以上\n2、菜单 -> 设置 -> USB模式：管理器APP"
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
        self.btnConnect.clicked.connect(self._on_connect)
        self.btnRefreshList.clicked.connect(self._on_refresh_list)
        self.btnUploadLocal.clicked.connect(self._on_upload_local)
        self.btnRestartDrm.clicked.connect(self._on_restart_drm)

    def _on_connect(self):
        self.VID = int(self._settings.get("usb_controler_vid"), 16)
        self.PID = int(self._settings.get("usb_controler_pid"), 16)
        """连接click"""
        if (self._is_connected == True):
            self._on_disconnect()
            return
        self._set_busy(True)
        self._update_connection_ui("Connecting")

        # 枚举设备
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
                self._set_busy(False)
                return
            index = items.index(item)
            dev = usbDeviceList[index]

        temp_string_builder = ""
        try:
            self.usbRC = UsbResponderClient(
                vid=dev.idVendor,
                pid=dev.idProduct,
                bus=dev.bus,
                address=dev.address,
                interface=0,
                timeout_ms=3000
            )
            kv = self.usbRC.hello()
            for k in sorted(kv.keys()):
                temp_string_builder += f"{k}={kv[k]}\n"
        except Exception as ex:
            InfoBar.error(
                "连接失败",
                str(ex),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=6000,
            )
            self._update_connection_ui("Connected")
            return
        InfoBar.success(
            "连接成功",
            f"已连接到：Bus {dev.bus}  Address {dev.address}  VID {hex(dev.idVendor)}  PID {hex(dev.idProduct)}\n{temp_string_builder}",
            parent=self,
            position=InfoBarPosition.TOP,
            duration=6000,
        )
        self._update_connection_ui(
            "Connected", f"{dev.bus} / {dev.address} / {hex(dev.idVendor)} / {hex(dev.idProduct)}")

    def _on_disconnect(self):
        '''断开连接'''
        if (self.usbRC != None):
            try:
                self.usbRC.close()
            except Exception as ex:
                InfoBar.error(
                    "断开失败",
                    ex,
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=6000,
                )
                return
            self._update_connection_ui("Disconnected")

    def _on_restart_drm(self):
        '''重启DRM'''

    def _on_upload_local(self):
        '''本地上传'''

    def _on_refresh_list(self):
        '''刷新列表'''

    def load_settings(self, settings: dict):
        self._settings = settings.copy()

    def _update_connection_ui(self, state: str, success_str=None):
        if state == "Connected":
            self._is_connected = True
            self.connectionStatusLabel.setText(f"已连接设备：{success_str}")
            self.btnConnect.setText("断开连接")
            self._is_busy = False
        elif state == "Connecting":
            self.connectionStatusLabel.setText("正在检测 EPass RNDIS 网卡...")
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
