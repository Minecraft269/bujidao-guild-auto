# -*- coding: utf-8 -*-
"""keybind.py —— 键位捕获（GUI设计规范 §3.6）。

为什么要单独一层：全局键盘钩子不该被界面直接调用 —— 它要能注入、能超时、
必须在**任何退出路径**上关掉（否则钩子残留会吞掉用户所有按键）。
所以这里把「钩子」抽成可注入的 listener，界面只看到 start()/wait()。

键名统一两种：`key.keyboard.<name>` 与 `key.mouse.<n>`，
与界面显示的「人类可读形式」分开（humanize）。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

DEFAULT_TIMEOUT = 10.0
CANCEL_KEY = "esc"


class KeyCaptureError(RuntimeError):
    """钩子不可用 / 启用失败。调用方必须保持原值并显式报错。"""


def normalize_key(name: str) -> str:
    """把 pynput 的键名规整成 `key.keyboard.<name>` / `key.mouse.<n>`。"""
    raw = str(name or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("key."):
        return raw
    if raw.startswith("mouse."):
        return f"key.{raw}"
    return f"key.keyboard.{raw}"


_MOUSE_LABELS = {"1": "鼠标左键", "2": "鼠标中键", "3": "鼠标右键"}


def humanize(key: str) -> str:
    """`key.keyboard.f1` → `F1`；`key.mouse.1` → 鼠标左键。"""
    if not key:
        return ""
    body = key[len("key.") :] if key.startswith("key.") else key
    if body.startswith("mouse."):
        n = body.split(".", 1)[1]
        return _MOUSE_LABELS.get(n, f"鼠标 {n}")
    name = body.split(".", 1)[1] if "." in body else body
    if len(name) == 1:
        return name.upper()
    return name.capitalize()


class KeyCapture:
    """一次捕获会话：start() 起钩子，wait() 拿结果，任何路径都会关钩子。

    listener_factory 是注入点：真实用 pynput（suppress=True 吞键），
    测试注入假对象，于是「开启→捕获→关闭 / 取消→关闭 / 超时→关闭」都可测。
    """

    def __init__(
        self,
        listener_factory: Callable[[Callable[[Any], bool]], Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._factory = listener_factory or _default_listener_factory
        self.timeout = timeout
        self._clock = clock
        self._listener: Any | None = None
        self._done = threading.Event()
        self._result: dict[str, Any] = {}
        self._deadline = 0.0

    # ── 生命周期 ────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._listener is not None:
            return
        try:
            self._listener = self._factory(self._on_press)
            start = getattr(self._listener, "start", None)
            if callable(start):
                start()
        except Exception as e:  # 钩子装不上必须显式报错，不能静默降级
            self._listener = None
            raise KeyCaptureError(f"全局键盘钩子启用失败: {e}") from e
        self._deadline = self._clock() + self.timeout

    def wait(self) -> dict[str, Any]:
        """等结果；超时自动关闭钩子并返回 timeout。"""
        self._done.wait(self.timeout)
        if not self._result:
            self._finish({"ok": False, "reason": "timeout"})
        return dict(self._result)

    def cancel(self) -> None:
        self._finish({"ok": False, "reason": "cancelled"})

    def _finish(self, result: dict[str, Any]) -> None:
        if not self._result:
            self._result = result
        self._stop_listener()
        self._done.set()

    def _stop_listener(self) -> None:
        listener, self._listener = self._listener, None
        if listener is None:
            return
        stop = getattr(listener, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                # 关钩子失败不该覆盖真正的结果（例如已经捕获到键）
                pass

    # ── 回调 ────────────────────────────────────────────────────────────
    def _on_press(self, key: Any) -> bool:
        """返回 False 表示吞掉这次按键（pynput suppress 语义）。"""
        name = _key_name(key)
        if name == CANCEL_KEY:
            self._finish({"ok": False, "reason": "cancelled"})
            return False
        if not name:
            return False
        if self._clock() > self._deadline:
            self._finish({"ok": False, "reason": "timeout"})
            return False
        canonical = normalize_key(name)
        self._finish({"ok": True, "key": canonical, "display": humanize(canonical)})
        return False


def _key_name(key: Any) -> str:
    """从 pynput 的 Key/KeyCode（或测试假对象）里取规范名。"""
    if key is None:
        return ""
    char = getattr(key, "char", None)
    if char is not None:
        return str(char).lower()
    name = getattr(key, "name", None)
    if name:
        return str(name).lower()
    return str(key).lower()


def _default_listener_factory(on_press: Callable[[Any], bool]) -> Any:
    """真实钩子：pynput 键盘监听 + suppress 吞键（捕获期间不透传给游戏）。"""
    try:
        from pynput import keyboard  # noqa: PLC0415
    except Exception as e:  # 缺依赖 / 无桌面会话
        raise KeyCaptureError(f"pynput 不可用: {e}") from e
    return keyboard.Listener(on_press=on_press, suppress=True)


def capture(listener_factory: Callable[[Any], Any] | None = None, timeout: float = DEFAULT_TIMEOUT):
    """便捷入口：起钩子 → 等结果 → 保证关闭。返回 {ok, key?, display?, reason?}。"""
    session = KeyCapture(listener_factory=listener_factory, timeout=timeout)
    session.start()
    try:
        return session.wait()
    finally:
        session._stop_listener()
