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
import time
from ctypes import wintypes
from typing import Any, Callable

from guild_auto.infrastructure import logger

PIPE_PREFIX = "bujidao-"
BUFFER_SIZE = 64 * 1024
# GUI 拉起后应在短时间内连上管道；超过这个上限就放弃等待并记 WARNING（§6.2 禁止无超时阻塞）。
CONNECT_TIMEOUT = 60.0

# ── Win32 常量 ────────────────────────────────────────────────────────────
_PIPE_ACCESS_DUPLEX = 0x00000003
_PIPE_TYPE_BYTE = 0x00000000
_PIPE_READMODE_BYTE = 0x00000000
_PIPE_WAIT = 0x00000000
_PIPE_UNLIMITED_INSTANCES = 255
_ERROR_PIPE_CONNECTED = 535
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_DUPLICATE_SAME_ACCESS = 0x00000002

# WinDLL 是动态属性代理，注解成 WinDLL 后 mypy 会把各方法当真实签名逐处校验，
# 反而逼出一串 union-attr 噪音；这里显式标 Any，只保留"非 Windows 上为 None"这一事实。
_kernel32: Any = None
if os.name == "nt":  # pragma: no cover - 非 Windows 上只用于 import
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # 显式原型：GetCurrentThread/GetCurrentProcess 返回伪句柄 (-1/-2)，
    # 默认按 c_int 传会把 64 位 HANDLE 截断，DuplicateHandle 会报 ERROR_INVALID_HANDLE。
    # 超时取消 ConnectNamedPipe 依赖 CancelSynchronousIo，必须拿到真实线程句柄。
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.GetCurrentThread.restype = wintypes.HANDLE
    _kernel32.DuplicateHandle.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    _kernel32.DuplicateHandle.restype = wintypes.BOOL
    _kernel32.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
    _kernel32.CancelSynchronousIo.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


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
        self._ever_connected = False
        self.last_error: str | None = None
        # 事件推送失败统计：同一方法只记一条 warning，避免 GUI 断开后刷屏（§四 不得刷屏）
        self._event_fail_count = 0
        self._event_fail_methods: set[str] = set()

    @property
    def masked_path(self) -> str:
        """管道名（一次性 token 脱敏），供日志使用（§6.1 S6）。"""
        return self.path.replace(self.token, "***") if self.token else self.path

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def ever_connected(self) -> bool:
        """是否曾经连上过：用来区分「GUI 从未连上」与「连上后断开」。"""
        return self._ever_connected

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

    def accept(self, timeout: float | None = CONNECT_TIMEOUT) -> bool:
        """等待 GUI 连接；返回是否连上（stop()/超时后返回 False）。

        ConnectNamedPipe 本身是无限阻塞的，这里放到工作线程里等，
        超时后关闭管道中止等待，保证服务端线程不会永远停在「等管道连接」（§6.2）。
        """
        if self._handle is None:
            self.create()
        if self._stop.is_set():
            logger.log_info(f"[阶段] S6 ipc.accept 已停止，不再等待连接 管道={self.masked_path}")
            return False
        timeout = CONNECT_TIMEOUT if timeout is None else float(timeout)
        started = time.monotonic()
        logger.log_info(
            f"[阶段][ENTER] S6 ipc.accept 等待 GUI 连接 管道={self.masked_path} 超时={timeout:.0f}s"
        )
        result: dict[str, Any] = {"ok": False, "err": None, "thread": None}
        done = threading.Event()

        def _connect() -> None:
            try:
                # 复制出真实线程句柄：超时时用它 CancelSynchronousIo 中止 ConnectNamedPipe
                thread_handle = wintypes.HANDLE()
                if _kernel32.DuplicateHandle(
                    _kernel32.GetCurrentProcess(),
                    _kernel32.GetCurrentThread(),
                    _kernel32.GetCurrentProcess(),
                    ctypes.byref(thread_handle),
                    0,
                    False,
                    _DUPLICATE_SAME_ACCESS,
                ):
                    result["thread"] = thread_handle
                result["ok"] = bool(_kernel32.ConnectNamedPipe(self._handle, None))
                result["err"] = ctypes.get_last_error()
            except Exception as e:  # ctypes 调用异常也要唤醒等待方并留痕
                result["err"] = e
            finally:
                done.set()

        threading.Thread(target=_connect, name="ipc-accept", daemon=True).start()
        finished = done.wait(timeout)
        elapsed = time.monotonic() - started
        thread_handle = result.get("thread")
        if not finished:
            logger.log_warning(
                f"[阶段] S6 ipc.accept 超时: 已等待={elapsed:.1f}s(上限 {timeout:.0f}s) "
                f"管道={self.masked_path}，中止等待"
            )
            self._stop.set()
            if thread_handle is not None:
                _kernel32.CancelSynchronousIo(thread_handle)
            finished = done.wait(2.0)  # 给被阻塞的 ConnectNamedPipe 一个有限时间返回
            if not finished:
                # 取消未生效：关闭句柄可能阻塞，交给后台线程，绝不再阻塞主流程
                logger.log_warning(
                    f"[阶段] S6 ipc.accept 取消等待未生效，改由后台关闭管道 管道={self.masked_path}"
                )
                threading.Thread(target=self._close_handle, daemon=True).start()
            else:
                self._close_handle()
            if thread_handle is not None:
                _kernel32.CloseHandle(thread_handle)
            logger.log_info(
                f"[阶段][EXIT ] S6 ipc.accept 耗时={time.monotonic() - started:.2f}s "
                f"结果=超时未连接"
            )
            return False
        if thread_handle is not None:
            _kernel32.CloseHandle(thread_handle)

        ok = bool(result["ok"])
        err = result["err"]
        if not ok and err == _ERROR_PIPE_CONNECTED:
            ok = True  # 客户端在 ConnectNamedPipe 之前就连上了
            logger.log_info(
                f"[阶段] S6 ipc.accept 客户端先连上(ERROR_PIPE_CONNECTED) 管道={self.masked_path}"
            )
        if ok:
            self._connected.set()
            self._ever_connected = True
            logger.log_info(
                f"[阶段][EXIT ] S6 ipc.accept 耗时={elapsed:.2f}s 结果=已连接 "
                f"管道={self.masked_path}"
            )
        else:
            logger.log_warning(
                f"[阶段] S6 ipc.accept 失败: WinError {err} 已等待={elapsed:.2f}s "
                f"管道={self.masked_path}"
            )
        return bool(ok)

    def stop(self) -> None:
        self._stop.set()
        self._close_handle()

    def _close_handle(self) -> None:
        handle, self._handle = self._handle, None
        if handle:
            try:
                _kernel32.DisconnectNamedPipe(handle)
            except Exception as e:  # 断开失败仍要继续关闭句柄，但不能静默
                logger.log_warning(f"[阶段] S6 断开命名管道失败(继续关闭): {e!r}")
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
                self._event_fail_count += 1
                # 同一事件方法只记第一次（GUI 断开后每次推送都会失败，不能刷屏）
                if method not in self._event_fail_methods:
                    self._event_fail_methods.add(method)
                    logger.log_warning(
                        f"[阶段] S6 IPC 事件推送失败 method={method} "
                        f"累计失败={self._event_fail_count} 原因={e}"
                    )

    def _read_lines(self, handle: Any):
        buf = b""
        chunk = ctypes.create_string_buffer(BUFFER_SIZE)
        read = wintypes.DWORD(0)
        total = 0
        while not self._stop.is_set():
            ok = _kernel32.ReadFile(handle, chunk, BUFFER_SIZE, ctypes.byref(read), None)
            if not ok or read.value == 0:
                # 读侧必须留痕：否则「GUI 说发了、核说没收到」时无从判断
                logger.log_warning(
                    f"[阶段] S6 ipc 读循环结束 ok={bool(ok)} 本次字节={read.value} "
                    f"累计字节={total} 管道={self.masked_path}"
                )
                return
            total += read.value
            if total == read.value:
                # 只记首包：首包到了说明连接真的通了（后续按行记录会刷屏）
                logger.log_info(
                    f"[阶段] S6 ipc 首包已读 字节={read.value} 管道={self.masked_path}"
                )
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
