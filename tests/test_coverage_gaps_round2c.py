# -*- coding: utf-8 -*-
"""补 application 层欠账（第三批）：异常/取消分支。

这些分支平时进不去（要等"终端被关""后台被取消""钩子自己炸了"），
但正是它们保证"异常时不静默继续"，所以值得锁住。
"""

from __future__ import annotations

import copy
import random

import pytest

from guild_auto.application import debug_data
from guild_auto.application import pause as pause_mod
from guild_auto.application import runtime as runtime_mod
from guild_auto.application.runtime import Runtime
from guild_auto.config.defaults import DEFAULT_CONFIG


@pytest.fixture(autouse=True)
def _neutral_runtime():
    def _reset():
        runtime_mod.reset_state()
        runtime_mod.configure_auto_pause(operation_mode="cli", enabled=False, grace_ms=0)
        runtime_mod.set_auto_pause_guard(False)

    _reset()
    yield
    _reset()


# ── pause：监听器内部异常必须被记日志、不能冒泡 ──────────────────────
def test_on_press_ctrl_c_falls_back_when_kill_fails(monkeypatch):
    monkeypatch.setattr(runtime_mod, "pause_key", lambda: "f12")
    import os as _os

    def _boom(*_a, **_k):
        raise OSError("no console")

    monkeypatch.setattr(_os, "kill", _boom)
    runtime_mod.set_paused(False)
    pause_mod._on_press(type("K", (), {"char": chr(3)})())
    # 兜底路径必须真的把脚本停下来，而不是当作没事发生
    assert runtime_mod.is_force_stop() is True or runtime_mod.is_paused() is True


def test_mouse_callbacks_swallow_exceptions(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("钩子内部故障")

    monkeypatch.setattr(pause_mod, "_maybe_auto_pause", _boom)
    pause_mod._on_mouse_move(1, 2)  # 不得抛
    pause_mod._on_mouse_click(1, 2, "left", True)  # 不得抛


# ── runtime：终端被关 / 后台被取消 ────────────────────────────────────
def test_ask_text_returns_empty_on_eof():
    def _eof(_prompt):
        raise EOFError

    rt = Runtime(cfg=copy.deepcopy(DEFAULT_CONFIG), ask_text_fn=_eof)
    assert rt.ask_text("确认?") == ""


def test_ask_text_returns_empty_on_keyboard_interrupt():
    def _interrupt(_prompt):
        raise KeyboardInterrupt

    rt = Runtime(cfg=copy.deepcopy(DEFAULT_CONFIG), ask_text_fn=_interrupt)
    assert rt.ask_text("确认?") == ""


def test_async_send_returns_false_when_backend_stopped():
    """后台已收工时不得再往游戏里发命令，且必须返回 False 让上层记失败。"""
    rt = Runtime(cfg=copy.deepcopy(DEFAULT_CONFIG))
    rt.async_system = type("S", (), {"is_stopped": lambda self: True})()
    rt.pause_gate = lambda: None
    sent = []
    rt.human = type(
        "H",
        (),
        {
            "send_chat_text": lambda *a, **k: sent.append(1),
            "wait_between_commands": lambda self: None,
        },
    )()
    fn = getattr(rt, "_async_send_fn", None)
    if fn is None:
        pytest.skip("该版本没有 _async_send_fn")
    assert fn("/guild member 示例玩家", "示例玩家", {}) is False
    assert sent == []


# ── debug_data：跳过判定时按随机动作类型分支 ──────────────────────────
@pytest.mark.parametrize("choice", ["promote", "demote", "kick", "none"])
def test_debug_records_skip_decide_covers_each_action(monkeypatch, choice):
    monkeypatch.setattr(random, "choice", lambda _seq: choice)
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["debug"]["simulation"]["players_per_rank"] = 1
    guild = debug_data._generate_debug_guild(cfg)
    rank = next(iter(guild["groups"]))
    pid = guild["groups"][rank][0]
    records = debug_data._generate_debug_records(cfg, guild, [(rank, pid)], decide_skipped=True)
    assert len(records) == 1
    acts = records[0]["actions"]
    if choice == "promote":
        assert acts and acts[0] == "promote"
    elif choice == "demote":
        assert acts == ["demote"]
    elif choice == "kick":
        assert acts == ["kick"]
    else:
        assert acts == []
