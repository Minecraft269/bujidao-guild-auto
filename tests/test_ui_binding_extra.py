# -*- coding: utf-8 -*-
"""补齐 ui/binding.py 与 ui/keybind.py 的边角：让总体覆盖率回到门禁线以上。

只测**不依赖真实硬件**的路径（钩子/管道都用注入或 monkeypatch），
真实进程启停由端到端 GUI 冒烟覆盖。
"""

from __future__ import annotations

import json
import types

import pytest

from guild_auto.config import load_config, save_config
from guild_auto.config.defaults import DEFAULT_CONFIG
from guild_auto.ui import binding as B
from guild_auto.ui import keybind as K


@pytest.fixture()
def bridge(tmp_path):
    path = tmp_path / "config.json"
    save_config(path, json.loads(json.dumps(DEFAULT_CONFIG)))
    return B.GuiBridge(load_config(path), path, report_dir=tmp_path, log_dir=tmp_path)


# ── binding：发布流程相关 ────────────────────────────────────────────────
def test_gui_executable_prefers_env_override(monkeypatch, tmp_path):
    fake = tmp_path / "Gui.exe"
    fake.write_bytes(b"MZ")
    monkeypatch.setenv("BUJIDAO_GUI", str(fake))
    assert B.gui_executable() == fake


def test_launch_without_gui_exe_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(B, "gui_executable", lambda: None)
    br = B.GuiBridge(DEFAULT_CONFIG, tmp_path / "c.json")
    with pytest.raises(FileNotFoundError) as ei:
        br.launch(timeout=0.01)
    assert "cargo build" in str(ei.value)


def test_open_dir_success_uses_startfile(bridge, monkeypatch):
    calls = []
    monkeypatch.setattr(B.os, "startfile", lambda p: calls.append(p), raising=False)
    out = bridge.server.handle_line('{"id":1,"method":"open_reports","params":{}}')
    assert out["ok"] is True
    assert calls and calls[0] == str(bridge.report_dir)


def test_check_update_rejects_missing_url(bridge):
    bridge.cfg["update"]["manifest_url"] = ""
    with pytest.raises(ValueError):
        bridge._check_update()


def test_check_update_rejects_unsigned_manifest(bridge, monkeypatch):
    payload = json.dumps({"version": "9.9.9", "variants": {"x": {"url": "a"}}}).encode()

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return payload

    monkeypatch.setattr(B.urllib.request, "urlopen", lambda *a, **k: Resp())
    bridge.cfg["update"]["manifest_url"] = "https://example.invalid/latest.json"
    with pytest.raises(ValueError) as ei:
        bridge._check_update()
    assert "签名" in str(ei.value)


def test_check_update_accepts_signed_manifest(bridge, monkeypatch):
    from guild_auto import capabilities as caps

    variant = caps.variant_meta()["build_variant"]
    payload = json.dumps(
        {"version": "9.9.9", "variants": {variant: {"url": "a", "signature": "sig"}}}
    ).encode()

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return payload

    monkeypatch.setattr(B.urllib.request, "urlopen", lambda *a, **k: Resp())
    bridge.cfg["update"]["manifest_url"] = "https://example.invalid/latest.json"
    result = bridge._check_update()
    assert result["version"] == "9.9.9"
    assert result["entry"]["signature"] == "sig"


def test_console_safe_tolerates_missing_stdout(monkeypatch):
    monkeypatch.setattr(B.sys, "stdout", None)
    B.console_safe("no crash")


def test_find_key_conflict_reports_other_path(bridge):
    fields = bridge._config_payload()["fields"]
    key_paths = [f["path"] for f in fields if "key" in f["path"] and f["kind"] != "bool"]
    assert key_paths, "配置里应有键位字段"
    first = key_paths[0]
    value = str(next(f for f in fields if f["path"] == first)["value"])
    if len(key_paths) > 1:
        conflict = bridge._find_key_conflict(value, key_paths[1])
        assert conflict == "" or conflict.startswith("与「")
    assert bridge._find_key_conflict("key.keyboard.f13", first) == ""


def test_send_event_after_stop_is_silent(bridge):
    bridge.server.stop()
    bridge.push_state(stage="停止后推送不应抛异常")


def test_controller_none_is_safe(bridge):
    assert bridge.controller is None
    assert bridge._on_pause() == {}
    assert bridge._on_resume() == {}
    assert bridge._on_stop() == {}


# ── keybind：取消与超时边界 ──────────────────────────────────────────────
class _L:
    def __init__(self):
        self.stopped = False

    def start(self):
        pass

    def stop(self):
        self.stopped = True


def test_cancel_stops_listener_and_marks_cancelled():
    listener = _L()
    session = K.KeyCapture(listener_factory=lambda _cb: listener, timeout=1.0)
    session.start()
    session.cancel()
    assert session.wait() == {"ok": False, "reason": "cancelled"}
    assert listener.stopped is True


def test_start_is_idempotent():
    made = []

    def factory(_cb):
        made.append(1)
        return _L()

    session = K.KeyCapture(listener_factory=factory, timeout=0.01)
    session.start()
    session.start()
    assert len(made) == 1
    session.cancel()


def test_empty_key_name_is_ignored():
    session = K.KeyCapture(listener_factory=lambda _cb: _L(), timeout=0.01)
    session.start()
    assert session._on_press(None) is False
    assert session.wait() == {"ok": False, "reason": "timeout"}


def test_press_after_deadline_reports_timeout():
    now = {"t": 0.0}
    session = K.KeyCapture(listener_factory=lambda _cb: _L(), timeout=1.0, clock=lambda: now["t"])
    session.start()
    now["t"] = 5.0
    session._on_press(types.SimpleNamespace(name="f3", char=None))
    assert session.wait() == {"ok": False, "reason": "timeout"}


def test_humanize_empty_and_unknown_mouse():
    assert K.humanize("") == ""
    assert K.humanize("key.mouse.9") == "鼠标 9"
