from __future__ import annotations
from typing import Callable, Optional
import argparse
import os
import struct
import sys
from typing import Dict, List, Optional, Tuple

import usb.core
import usb.util

import core.protocol as P
from PyQt6.QtCore import (QObject, pyqtSignal)

MAX_PAYLOAD = 8 * 1024 * 1024
USB_REQUEST_CHUNK = 16 * 1024
DEFAULT_CHUNK = USB_REQUEST_CHUNK - 4
VID = 0x1d6b
PID = 0x0203


def _write_exact(ep, data: bytes, timeout: int) -> None:
    sent = 0
    while sent < len(data):
        n = ep.write(data[sent: sent + USB_REQUEST_CHUNK], timeout=timeout)
        if n <= 0:
            raise IOError("USB write failed")
        sent += n


def struct_pack_u32(v: int) -> bytes:
    return v.to_bytes(4, "little")


def struct_unpack_header(hdr: bytes) -> Tuple[int, int, int, int, int, int, int]:
    return struct.unpack("<IHHIIII", hdr)


class UsbResponderClient:
    def __init__(
            self,
            vid: int,
            pid: int,
            *,
            bus: Optional[int] = None,
            address: Optional[int] = None,
            interface: int = 0,
            timeout_ms: int = 300_000,
            disconnect_callback=None,
    ) -> None:
        self._disconnect_callback = disconnect_callback
        self._dev = usb.core.find(
            idVendor=vid,
            idProduct=pid,
            custom_match=lambda d:
            (bus is None or d.bus == bus) and
                (address is None or d.address == address)
        )
        if self._dev is None:
            raise RuntimeError(
                f"未找到设备 vid=0x{vid:04x} pid=0x{pid:04x}"
            )
        self._timeout = timeout_ms
        self._iface = interface
        try:
            if self._dev.is_kernel_driver_active(interface):
                self._dev.detach_kernel_driver(interface)
        except (NotImplementedError, ValueError, usb.core.USBError):
            pass
        self._dev.set_configuration()
        cfg = self._dev.get_active_configuration()
        intf = usb.util.find_descriptor(cfg, bInterfaceNumber=interface)
        if intf is None:
            raise RuntimeError(f"无接口 {interface}")
        self._ep_in = usb.util.find_descriptor(
            intf,
            custom_match=lambda e: usb.util.endpoint_direction(
                e.bEndpointAddress) == usb.util.ENDPOINT_IN,
        )
        self._ep_out = usb.util.find_descriptor(
            intf,
            custom_match=lambda e: usb.util.endpoint_direction(
                e.bEndpointAddress) == usb.util.ENDPOINT_OUT,
        )
        if self._ep_in is None or self._ep_out is None:
            raise RuntimeError("未找到 bulk IN/OUT 端点（期望 0x81 / 0x02）")
        self._req = 0
        self._rxbuf = bytearray()
        self._rx_read_size = USB_REQUEST_CHUNK

    def close(self) -> None:
        usb.util.dispose_resources(self._dev)

    def _next_id(self) -> int:
        self._req = (self._req + 1) & 0xFFFFFFFF
        if self._req == 0:
            self._req = 1
        return self._req

    def _send_frame(self, typ: int, payload: bytes = b"", req_id: Optional[int] = None) -> int:
        rid = self._next_id() if req_id is None else req_id
        raw = P.Frame(type=typ, request_id=rid, payload=payload).encode()
        _write_exact(self._ep_out, raw[: P.HEADER_SIZE], self._timeout)
        if len(raw) > P.HEADER_SIZE:
            _write_exact(self._ep_out, raw[P.HEADER_SIZE:], self._timeout)
        return rid

    def _recv_frame(self) -> P.Frame:
        while len(self._rxbuf) < P.HEADER_SIZE:
            self._read_some(self._rx_read_size)
        hdr = bytes(self._rxbuf[: P.HEADER_SIZE])
        _magic, _ver, _typ, _fl, _rid, plen, _crc = struct_unpack_header(hdr)
        if plen > MAX_PAYLOAD:
            raise ValueError("payload too large")
        frame_size = P.HEADER_SIZE + plen
        while len(self._rxbuf) < frame_size:
            self._read_some(frame_size - len(self._rxbuf))
        raw = bytes(self._rxbuf[:frame_size])
        del self._rxbuf[:frame_size]
        return P.decode_frame(raw)

    def _read_some(self, size: int) -> None:
        # 0 长度读是 bulk IN 的 ZLP 收尾包，不是错误：直接忽略，调用方循环会再读。
        # 真正的超时由 pyusb 抛 USBTimeoutError，不会走到这里返回空。
        chunk = self._ep_in.read(size, timeout=self._timeout)
        if chunk is None or len(chunk) == 0:
            return
        self._rxbuf.extend(bytes(chunk))

    def _expect_kv(self, req_id: int) -> Dict[str, str]:
        fr = self._recv_frame()
        if fr.request_id != req_id:
            raise RuntimeError(
                f"request_id 不匹配: 期望 {req_id} 收到 {fr.request_id}")
        if fr.type == P.MSG_ERROR:
            kv = P.decode_kv(fr.payload)
            raise RuntimeError(kv.get("message", "ERROR"))
        if fr.type != P.MSG_STATUS:
            raise RuntimeError(f"意外消息类型 {fr.type}，期望 STATUS")
        return P.decode_kv(fr.payload)

    def hello(self) -> Dict[str, str]:
        try:
            rid = self._send_frame(P.MSG_HELLO)
            return self._expect_kv(rid)
        except Exception as e:
            self._notify_disconnect(e)
            raise

    def file_put(
            self,
            local_path: str,
            remote_path: str,
            chunk_size: int = DEFAULT_CHUNK,
            desire_storage: Optional[str] = None,
            perm: Optional[str] = None,
    ) -> None:
        try:
            rid = self._next_id()
            items: List[Tuple[str, str]] = [("path", remote_path)]
            if desire_storage:
                items.append(("desire_storage", desire_storage))
            if perm:
                items.append(("perm", perm))
            begin = P.encode_kv(items)
            self._send_frame(P.MSG_FILE_PUT_BEGIN, begin, req_id=rid)
            self._expect_kv(rid)

            with open(local_path, "rb") as f:
                while True:
                    piece = f.read(chunk_size)
                    if not piece:
                        break
                    chunk_payload = struct_pack_u32(rid) + piece
                    self._send_frame(P.MSG_FILE_PUT_CHUNK,
                                     chunk_payload, req_id=rid)
                    self._expect_kv(rid)

            self._send_frame(P.MSG_FILE_PUT_END,
                             struct_pack_u32(rid), req_id=rid)
            self._expect_kv(rid)
        except Exception as e:
            self._notify_disconnect(e)
            raise

    def file_get(self, remote_path: str, local_path: str) -> None:
        try:
            rid = self._send_frame(
                P.MSG_FILE_GET, P.encode_kv([("path", remote_path)]))
            fr = self._recv_frame()
            if fr.request_id != rid:
                raise RuntimeError(
                    f"request_id 不匹配: 期望 {rid} 收到 {fr.request_id}")
            if fr.type == P.MSG_ERROR:
                kv = P.decode_kv(fr.payload)
                raise RuntimeError(kv.get("message", "ERROR"))
            if fr.type != P.MSG_FILE_GET:
                raise RuntimeError(f"意外类型 {fr.type}，期望 FILE_GET")
            with open(local_path, "wb") as out:
                out.write(fr.payload)
        except Exception as e:
            self._notify_disconnect(e)
            raise

    def file_list(self, path: str = ".") -> Tuple[List[str], List[str]]:
        try:
            rid = self._send_frame(
                P.MSG_FILE_LIST, P.encode_kv([("path", path)]))
            kv = self._expect_kv(rid)
            files_raw = kv.get("files", "")
            dirs_raw = kv.get("dirs", "")
            files = [line for line in files_raw.splitlines() if line]
            dirs = [line for line in dirs_raw.splitlines() if line]
            return files, dirs
        except Exception as e:
            self._notify_disconnect(e)
            raise

    def file_stat(self, path: str) -> Dict[str, str]:
        try:
            rid = self._send_frame(
                P.MSG_FILE_STAT, P.encode_kv([("path", path)]))
            return self._expect_kv(rid)
        except Exception as e:
            self._notify_disconnect(e)
            raise

    def file_delete(self, remote_path: str, desire_storage: Optional[str] = None) -> None:
        try:
            items: List[Tuple[str, str]] = [("path", remote_path)]
            if desire_storage:
                items.append(("desire_storage", desire_storage))
            rid = self._send_frame(P.MSG_FILE_DELETE, P.encode_kv(items))
            self._expect_kv(rid)
        except Exception as e:
            self._notify_disconnect(e)
            raise

    def file_rename(self, src: str, dst: str, desire_storage: Optional[str] = None) -> None:
        try:
            items: List[Tuple[str, str]] = [("from", src), ("to", dst)]
            if desire_storage:
                items.append(("desire_storage", desire_storage))
            rid = self._send_frame(P.MSG_FILE_RENAME, P.encode_kv(items))
            self._expect_kv(rid)
        except Exception as e:
            self._notify_disconnect(e)
            raise

    def dir_mkdir(self, path: str, parents: bool = False, desire_storage: Optional[str] = None) -> None:
        try:
            items: List[Tuple[str, str]] = [("path", path)]
            if parents:
                items.append(("parents", "1"))
            if desire_storage:
                items.append(("desire_storage", desire_storage))
            rid = self._send_frame(P.MSG_FILE_MKDIR, P.encode_kv(items))
            self._expect_kv(rid)
        except Exception as e:
            self._notify_disconnect(e)
            raise

    def devinfo(self) -> Dict[str, str]:
        try:
            rid = self._send_frame(P.MSG_DEVINFO)
            fr = self._recv_frame()
            if fr.request_id != rid:
                raise RuntimeError(
                    f"request_id 不匹配: 期望 {rid} 收到 {fr.request_id}")
            if fr.type == P.MSG_ERROR:
                kv = P.decode_kv(fr.payload)
                raise RuntimeError(kv.get("message", "ERROR"))
            if fr.type != P.MSG_DEVINFO:
                raise RuntimeError(f"意外类型 {fr.type}，期望 DEVINFO")
            return P.decode_kv(fr.payload)
        except Exception as e:
            self._notify_disconnect(e)
            raise

    def command_exec(
            self,
            command: str,
            *,
            timeout_ms: int = 0,
            max_stdout: int = 0,
            max_stderr: int = 0,
    ) -> P.CommandResult:
        try:
            pl = P.encode_command_exec(
                command, timeout_ms=timeout_ms, max_stdout=max_stdout, max_stderr=max_stderr
            )
            rid = self._send_frame(P.MSG_COMMAND_EXEC, pl)
            fr = self._recv_frame()
            if fr.request_id != rid:
                raise RuntimeError(
                    f"request_id 不匹配: 期望 {rid} 收到 {fr.request_id}")
            if fr.type == P.MSG_ERROR:
                kv = P.decode_kv(fr.payload)
                raise RuntimeError(kv.get("message", "ERROR"))
            if fr.type != P.MSG_COMMAND_RESULT:
                raise RuntimeError(f"意外类型 {fr.type}，期望 COMMAND_RESULT")
            return P.decode_command_result(fr.payload)
        except Exception as e:
            self._notify_disconnect(e)
            raise

    def _notify_disconnect(self, error: Optional[BaseException] = None) -> None:
        if self._disconnect_callback:
            self._disconnect_callback(error)

# cl = UsbResponderClient(
#     vid=VID, pid=PID, bus=1, address=27, interface=0, timeout_ms=300
# )
# # kv = cl.hello()
# # for k in sorted(kv.keys()):
# # print(f"{k}={kv[k]}")
# files, dirs = cl.file_list("/assets")
# for name in dirs:
#     print(f"{name}/")
# for name in files:
#     print(name)

# # for dev in usb.core.find(find_all=True):
# # if (dev.idVendor == VID and dev.idProduct == PID):
#     # print(
#     # hex(dev.idVendor),
#     # hex(dev.idProduct),
#     # dev.bus,
#     # dev.address,
#     # dev.serial_number
# # )
# # print(dev)
