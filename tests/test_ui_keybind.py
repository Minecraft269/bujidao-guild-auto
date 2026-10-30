# -*- coding: utf-8 -*-
"""§3.6 键位捕获：钩子层三个退出路径都必须关钩子。

真实钩子用 pynput（suppress=True 吞键），这里注入假 listener 测状态机：
开启→捕获→关闭 / 取消→关闭 / 超时→关闭 / 钩子装不上→显式报错。
"""

from __future__ import annotations

import pytest

from guild_auto.ui import keybind as K


class FakeKey:
    def __init__(self, char=None, name=None):
        self.char = char
        if name is not None:
            self.name = name


class FakeListener:
    instances: list[FakeListener] = []

    def __init__(self, on_press):
        self.on_press = on_press
        self.started = False
        self.stopped = False
        FakeListener.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


@pytest.fixture(autouse=True)
def _reset():
    FakeListener.instances.clear()
    yield
    FakeListener.instances.clear()


def _factory(on_press):
    return FakeListener(on_press)


def test_capture_key_closes_hook():
    session = K.KeyCapture(listener_factory=_factory, timeout=1.0)
    session.start()
    assert FakeListener.instances[0].started is True

    FakeListener.instances[0].on_press(FakeKey(name="f1"))
    result = session.wait()

    assert result == {"ok": True, "key": "key.keyboard.f1", "display": "F1"}
    assert FakeListener.instances[0].stopped is True


def test_escape_cancels_and_closes_hook_without_writing():
    session = K.KeyCapture(listener_factory=_factory, timeout=1.0)
    session.start()
    FakeListener.instances[0].on_press(FakeKey(name="esc"))
    result = session.wait()
    assert result == {"ok": False, "reason": "cancelled"}
    assert "key" not in result
    assert FakeListener.instances[0].stopped is True


def test_timeout_closes_hook():
    session = K.KeyCapture(listener_factory=_factory, timeout=0.05)
    session.start()
    result = session.wait()
    assert result == {"ok": False, "reason": "timeout"}
    assert FakeListener.instances[0].stopped is True


def test_hook_failure_raises_explicitly():
    def boom(_on_press):
        raise RuntimeError("没有桌面会话")

    session = K.KeyCapture(listener_factory=boom)
    with pytest.raises(K.KeyCaptureError) as ei:
        session.start()
    assert "没有桌面会话" in str(ei.value)


def test_character_key_is_lowercased_and_humanized():
    session = K.KeyCapture(listener_factory=_factory, timeout=1.0)
    session.start()
    FakeListener.instances[0].on_press(FakeKey(char="T"))
    assert session.wait() == {"ok": True, "key": "key.keyboard.t", "display": "T"}


def test_mouse_key_normalization():
    assert K.normalize_key("mouse.1") == "key.mouse.1"
    assert K.humanize("key.mouse.1") == "鼠标左键"
    assert K.humanize("key.mouse.3") == "鼠标右键"
    assert K.humanize("key.keyboard.space") == "Space"


def test_normalize_is_idempotent():
    once = K.normalize_key("f2")
    assert K.normalize_key(once) == once == "key.keyboard.f2"


def test_capture_helper_always_stops_listener():
    result = K.capture(listener_factory=_factory, timeout=0.05)
    assert result == {"ok": False, "reason": "timeout"}
    assert FakeListener.instances[0].stopped is True
