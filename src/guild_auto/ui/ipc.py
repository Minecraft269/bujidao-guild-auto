# -*- coding: utf-8 -*-
"""ipc.py —— 核侧的命名管道 JSON-RPC 服务端（GUI设计规范 §2）。

为什么用 ctypes 直接调 Win32：标准库没有"创建命名管道**服务端**"的 API；
`multiprocessing.connection.Listener` 能用管道，但它用 pickle 分帧，
而规范要求**单行 JSON、UTF-8、无 BOM**，且要能与 Rust 侧 `BufReader::lines()`
直接对接。所以这里用 CreateNamedPipeW 自己建管道，读写按行切分。

安全：管道名带一次性 token（8 字节 hex），固定管道名一律不用 ——
否则本机任意进程都能连上来说话。握手必须带对 token，否则回 ok=false 后断开。

进程关系：GUI 断开时核继续运行（只记 warning）；核退出时 GUI 提示"核心已退出"。
"""

from __future__ import annotations

import ctypes
import json
import os
import secrets
import threading
from ctypes import wintypes
from typing import Any, Callable

PIPE_PREFIX = "bujidao-"
BUFFER_SIZE = 64 * 1024

# ── Win32 常量 ────────────────────────────────────────────────────────────
_PIPE_ACCESS_DUPLEX = 0x00000003
_PIPE_TYPE_BYTE = 0x00000000
_PIPE_READMODE_BYTE = 0x00000000
_PIPE_WAIT = 0x00000000
_PIPE_UNLIMITED_INSTANCES = 255
_ERROR_PIPE_CONNECTED = 535
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

if os.name == "nt":  # pragma: no cover - 非 Windows 上只用于 import
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:  # pragma: no cover
    _kernel32 = None


class IpcError(RuntimeError):
    """管道创建/读写失败。"""


def _check(ok: Any, what: str) -> None:
    if not ok:
        raise IpcError(f"{what} 失败: WinError {ctypes.get_last_error()}")


class IpcServer:
    """单连接、按行 JSON 的命名管道服务端。

    handlers: {method: callable(params: dict) -> Any}，返回值由本类包进应答。
    事件推送走 send_event()（无 id），供核在运行中主动推 state/event。
    """

    def __init__(self, handlers: dict[str, Callable[[dict], Any]], token: str | None = None):
        self.token = token or secrets.token_hex(8)
        self.pipe_name = PIPE_PREFIX + self.token
        self.path = "\\\\.\\pipe\\" + self.pipe_name
        self._handlers = handlers
        self._handle: Any = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._connected = threading.Event()
        self.last_error: str | None = None

    # ── 生命周期 ──────────────────────────────────────────────────────────
    def create(self) -> None:
        """创建管道实例（不阻塞）。"""
        _check(_kernel32 is not None, "加载 kernel32")
        handle = _kernel32.CreateNamedPipeW(
            self.path,
            _PIPE_ACCESS_DUPLEX,
            _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT,
            _PIPE_UNLIMITED_INSTANCES,
            BUFFER_SIZE,
            BUFFER_SIZE,
            0,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            raise IpcError(f"创建命名管道失败: WinError {ctypes.get_last_error()}")
        self._handle = handle

    def accept(self) -> bool:
        """等待 GUI 连接；返回是否连上（stop() 后返回 False）。"""
        if self._handle is None:
            self.create()
        ok = _kernel32.ConnectNamedPipe(self._handle, None)
        if not ok and ctypes.get_last_error() == _ERROR_PIPE_CONNECTED:
            ok = True  # 客户端在 ConnectNamedPipe 之前就连上了
        if ok:
            self._connected.set()
        return bool(ok)

    def stop(self) -> None:
        self._stop.set()
        self._close_handle()

    def _close_handle(self) -> None:
        handle, self._handle = self._handle, None
        if handle:
            try:
                _kernel32.DisconnectNamedPipe(handle)
            except Exception:
                pass
            _kernel32.CloseHandle(handle)

    # ── 收发 ──────────────────────────────────────────────────────────────
    def _write_line(self, obj: dict) -> None:
        if self._handle is None:
            raise IpcError("管道未连接")
        payload = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        written = wintypes.DWORD(0)
        _check(
            _kernel32.WriteFile(self._handle, payload, len(payload), ctypes.byref(written), None),
            "写管道",
        )

    def send_event(self, method: str, params: dict) -> None:
        """推送事件（无 id）。GUI 断开后静默失败 —— 核必须继续运行。"""
        if not self._connected.is_set():
            return
        with self._lock:
            try:
                self._write_line({"method": method, "params": params})
            except IpcError as e:  # GUI 断开不该影响核
                self.last_error = str(e)

    def _read_lines(self, handle: Any):
        buf = b""
        chunk = ctypes.create_string_buffer(BUFFER_SIZE)
        read = wintypes.DWORD(0)
        while not self._stop.is_set():
            ok = _kernel32.ReadFile(handle, chunk, BUFFER_SIZE, ctypes.byref(read), None)
            if not ok or read.value == 0:
                return
            buf += chunk.raw[: read.value]
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    yield line.decode("utf-8", "replace")

    def serve_forever(self) -> None:
        """接受一个连接并循环处理请求，直到对端断开或 stop()。"""
        if not self.accept():
            return
        handle = self._handle
        try:
            for line in self._read_lines(handle):
                reply = self.handle_line(line)
                if reply is None:
                    continue
                with self._lock:
                    self._write_line(reply)
        finally:
            self._connected.clear()
            self._close_handle()

    # ── 协议 ──────────────────────────────────────────────────────────────
    def handle_line(self, line: str) -> dict | None:
        """处理一行请求，返回应答字典（含原 id）。token 不对 → ok=false 并断连。"""
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            return {"ok": False, "error": f"请求不是合法 JSON: {e}"}
        req_id = msg.get("id")
        method = str(msg.get("method") or "")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        # 带 token 的请求必须对得上；对不上直接拒绝并停止服务（一次性 token 语义）
        if msg.get("token") is not None and msg.get("token") != self.token:
            self._stop.set()
            return {"id": req_id, "ok": False, "error": "token 无效，连接被拒绝"}
        handler = self._handlers.get(method)
        if handler is None:
            return {"id": req_id, "ok": False, "error": f"未知方法: {method}"}
        try:
            result = handler(params)
        except Exception as e:  # 原样回传错误文本，不吞
            return {"id": req_id, "ok": False, "error": str(e)}
        return {"id": req_id, "ok": True, "result": result}
