# -*- coding: utf-8 -*-
"""Ctrl+C 中断菜单与信号处理（覆盖率欠账里最大的一块）。

不许只测 happy path：这个菜单是"用户按了 Ctrl+C 之后唯一能自救的入口"，
退出、继续、再按一次 Ctrl+C、超时、非 Windows 回退，五条路径都要锁住。
"""

from __future__ import annotations

import builtins
import sys
import types

import pytest

from guild_auto.application import pause as pause_mod
from guild_auto.application import runtime as runtime_mod


class _FakeTime:
    """可控时钟：每次 time() 前进 0.1s，sleep() 不真睡。"""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        self.now += 0.1
        return self.now

    def sleep(self, _s):
        return None


def _fake_msvcrt(chars):
    mod = types.ModuleType("msvcrt")
    queue = list(chars)

    def kbhit():
        return bool(queue)

    def getwch():
        return queue.pop(0) if queue else "\r"

    mod.kbhit = kbhit
    mod.getwch = getwch
    return mod


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    runtime_mod.reset_state()
    monkeypatch.setattr(pause_mod, "time", _FakeTime())
    yield
    runtime_mod.reset_state()


def _install(monkeypatch, chars):
    monkeypatch.setitem(sys.modules, "msvcrt", _fake_msvcrt(chars))


def test_menu_enter_means_exit(monkeypatch):
    _install(monkeypatch, ["\r"])
    assert pause_mod._ctrlc_pause_menu() is False


def test_menu_c_then_enter_means_continue(monkeypatch):
    _install(monkeypatch, ["c", "\r"])
    assert pause_mod._ctrlc_pause_menu() is True


def test_menu_backspace_is_ignored_then_c_continues(monkeypatch):
    _install(monkeypatch, ["\x08", "c", "\r"])
    assert pause_mod._ctrlc_pause_menu() is True


def test_menu_second_ctrl_c_exits_immediately(monkeypatch):
    _install(monkeypatch, ["\x03"])
    assert pause_mod._ctrlc_pause_menu() is False


def test_menu_timeout_exits(monkeypatch):
    _install(monkeypatch, [])  # 永不按键 → 5s 超时
    assert pause_mod._ctrlc_pause_menu() is False


def test_menu_falls_back_when_msvcrt_missing(monkeypatch):
    monkeypatch.setattr(pause_mod, "_ctrlc_pause_menu_fallback", lambda: True)
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "msvcrt":
            raise ImportError("no msvcrt")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert pause_mod._ctrlc_pause_menu() is True


def test_fallback_menu_c_continues(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda _p="": "c")
    assert pause_mod._ctrlc_pause_menu_fallback() is True


def test_fallback_menu_empty_means_exit(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda _p="": "")
    assert pause_mod._ctrlc_pause_menu_fallback() is False


def test_signal_handler_pauses_and_raises():
    with pytest.raises(KeyboardInterrupt):
        pause_mod._signal_handler(2, None)
    assert runtime_mod.is_paused() is True
    assert runtime_mod.is_force_stop() is True


def test_install_signal_handlers_returns_previous():
    prev = pause_mod.install_signal_handlers()
    assert isinstance(prev, dict)
    assert "SIGINT" in prev
