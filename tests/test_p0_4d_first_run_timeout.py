# -*- coding: utf-8 -*-
"""P0-4d：首启锁配置页 + S5 超时把界面一起带走。

守三条不变量：
  1. `first_run=True` 时 S5 **不启动计时**（配置写多小都不计时）；
  2. 非首启一律按 `config.ui.start_timeout` 计时，且退出原因是机器码
     `start_timeout`（界面据此保留，不自关）；
  3. `ui.start_timeout` 有 schema 校验（10–3600 整数）。

这里全部用注入替身，不跑真 Gui.exe；真进程的 180s 静置由
`scripts/repro_p0_4d.py` 覆盖。
"""

from __future__ import annotations

import copy
import threading
import time

import pytest

from guild_auto.config.defaults import DEFAULT_CONFIG
from guild_auto.config.schema import validate_config
from guild_auto.main import _gui_notify_core_exit
from guild_auto.ui import binding as B


class _FakeProc:
    """一直活着的假 Gui 进程：只有显式 terminate 才会退出。"""

    pid = 4321
    returncode = None

    def __init__(self):
        self.terminated = False

    def poll(self):
        return -15 if self.terminated else None

    def terminate(self):
        self.terminated = True


def _fake_env(monkeypatch, tmp_path, *, first_run: bool, start_timeout: float):
    """装好 launch() 需要的全部替身，返回 (bridge, proc)。"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["ui"]["start_timeout"] = start_timeout
    exe = tmp_path / "Gui.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(B, "gui_executable", lambda: exe)
    br = B.GuiBridge(
        cfg,
        tmp_path / "config.json",
        report_dir=tmp_path,
        log_dir=tmp_path,
        first_run=first_run,
    )
    monkeypatch.setattr(br.server, "create", lambda: None)
    monkeypatch.setattr(br.server, "serve_forever", lambda: None)
    proc = _FakeProc()
    monkeypatch.setattr(B.subprocess, "Popen", lambda _cmd: proc)
    return br, proc


def test_first_run_never_times_out_even_with_tiny_config(monkeypatch, tmp_path):
    """首启不启动计时：配置写 0.05s，等 0.5s 仍必须判成「用户点了开始」。"""
    br, _ = _fake_env(monkeypatch, tmp_path, first_run=True, start_timeout=0.05)
    threading.Timer(0.5, br._started.set).start()
    started = time.monotonic()
    assert br.launch() is True
    waited = time.monotonic() - started
    assert waited >= 0.45, f"首启不该在 {waited:.2f}s 就结束等待（配置 0.05s 生效了）"
    assert br.outcome == "started"
    assert br.exit_reason == "normal"


def test_non_first_run_uses_config_timeout(monkeypatch, tmp_path):
    """非首启按 config 的 ui.start_timeout 计时，退出原因是 start_timeout。"""
    br, proc = _fake_env(monkeypatch, tmp_path, first_run=False, start_timeout=0.05)
    started = time.monotonic()
    assert br.launch() is False
    waited = time.monotonic() - started
    assert waited < 0.5, f"应当按配置 0.05s 超时，实际等了 {waited:.2f}s"
    assert br.outcome == "超时"
    assert br.exit_reason == "start_timeout", "超时必须可被界面识别，不能自关"
    assert not proc.terminated, "超时属用户没操作：绝不能再自己去杀界面进程（P0-4d 根因）"


def test_non_first_run_timeout_scales_with_config(monkeypatch, tmp_path):
    """配置值真的被用上：0.5s 的等待明显长于 0.05s。"""
    br, _ = _fake_env(monkeypatch, tmp_path, first_run=False, start_timeout=0.5)
    started = time.monotonic()
    assert br.launch() is False
    waited = time.monotonic() - started
    assert waited >= 0.45, f"配置 0.5s 却只等了 {waited:.2f}s"


def test_gui_disconnect_is_not_reported_as_start_timeout(monkeypatch, tmp_path):
    """用户关界面 ≠ 超时：原因必须是 gui_gone，界面本来也没了。"""
    br, _ = _fake_env(monkeypatch, tmp_path, first_run=False, start_timeout=30)
    br.server._ever_connected = True
    br.server._connected.clear()
    assert br.launch() is False
    assert br.exit_reason == "gui_gone"


def test_core_exit_notice_carries_reason():
    """ipc-closed 报文必须带 reason：界面靠它决定「自退」还是「留着提示」。"""

    class _Srv:
        connected = True

        def __init__(self):
            self.events = []

        def send_event(self, method, params):
            self.events.append((method, params))

    srv = _Srv()

    class _Bridge:
        exit_reason = "start_timeout"

    _gui_notify_core_exit(srv, _Bridge())
    assert srv.events and srv.events[0][0] == "ipc-closed"
    assert srv.events[0][1]["reason"] == "start_timeout"

    # 没有 bridge（老路径/控制台）时退化成 normal，不能让界面莫名留窗
    srv2 = _Srv()
    _gui_notify_core_exit(srv2)
    assert srv2.events[0][1]["reason"] == "normal"


# ── ui.start_timeout 的 schema 校验 ────────────────────────────────────
def _timeout_errors(value):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["ui"]["start_timeout"] = value
    return [msg for level, msg in validate_config(cfg) if "ui.start_timeout" in msg]


@pytest.mark.parametrize("bad", [0, 5, 9, 3601, 99999, "120", 12.5, True])
def test_invalid_start_timeout_is_rejected(bad):
    assert _timeout_errors(bad), f"ui.start_timeout={bad!r} 应当报错"


@pytest.mark.parametrize("good", [10, 120, 3600])
def test_valid_start_timeout_passes(good):
    assert _timeout_errors(good) == []
