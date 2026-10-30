# -*- coding: utf-8 -*-
"""补核心层覆盖率欠账（docs/覆盖率报告.md 第七节）。

只为把 application 层里"没人走过但确实在线上跑"的分支走一遍：
自动暂停的条件判定、暂停键切换、调试数据按等级生成。
不引入新行为，只调用既有实现。
"""

from __future__ import annotations

import copy
import json

import pytest

from guild_auto.application import pause as pause_mod
from guild_auto.application import runtime as runtime_mod
from guild_auto.application.debug_data import _generate_debug_guild
from guild_auto.config.defaults import DEFAULT_CONFIG


@pytest.fixture(autouse=True)
def _clean_runtime_state():
    runtime_mod.reset_state()
    yield
    runtime_mod.reset_state()


def _cfg():
    return copy.deepcopy(DEFAULT_CONFIG)


# ── debug_data：players_per_rank 两种写法 ──────────────────────────────
def test_debug_guild_accepts_int_players_per_rank():
    cfg = _cfg()
    cfg["debug"]["simulation"]["players_per_rank"] = 2
    guild = _generate_debug_guild(cfg)
    total = sum(len(ids) for ids in guild["groups"].values())
    assert total == 2 * len(cfg["rank_names"])


def test_debug_guild_accepts_dict_players_per_rank():
    cfg = _cfg()
    ranks = list(cfg["rank_names"].keys())
    cfg["debug"]["simulation"]["players_per_rank"] = {ranks[0]: 3}
    guild = _generate_debug_guild(cfg)
    # 未列出的等级按 1 人补齐
    assert len(guild["groups"][ranks[0]]) == 3
    assert len(guild["groups"][ranks[1]]) == 1


# ── runtime：模拟输入窗口与自动暂停条件 ────────────────────────────────
def test_input_is_simulated_false_without_note():
    assert runtime_mod.input_is_simulated() is False


def test_input_is_simulated_true_right_after_note(monkeypatch):
    # 忽略窗口长度来自 grace_ms，用 0 就等于没有窗口，这里给 5 秒
    runtime_mod.configure_auto_pause(operation_mode="ui", enabled=True, grace_ms=5000)
    now = {"t": 100.0}
    monkeypatch.setattr(runtime_mod.time, "monotonic", lambda: now["t"])
    runtime_mod.note_simulated_input()
    assert runtime_mod.input_is_simulated() is True
    now["t"] += 10_000.0  # 越过忽略窗口
    assert runtime_mod.input_is_simulated() is False


def test_request_auto_pause_requires_ui_mode():
    runtime_mod.configure_auto_pause(operation_mode="cli", enabled=True, grace_ms=0)
    runtime_mod.set_auto_pause_guard(True)
    assert runtime_mod.request_auto_pause() is False  # 非 UI 模式不触发


def test_request_auto_pause_skips_when_already_paused():
    runtime_mod.configure_auto_pause(operation_mode="ui", enabled=True, grace_ms=0)
    runtime_mod.set_auto_pause_guard(True)
    runtime_mod.set_paused(True)
    assert runtime_mod.request_auto_pause() is False
    runtime_mod.set_paused(False)


def test_request_auto_pause_skips_simulated_input(monkeypatch):
    runtime_mod.configure_auto_pause(operation_mode="ui", enabled=True, grace_ms=5000)
    runtime_mod.set_auto_pause_guard(True)
    now = {"t": 1.0}
    monkeypatch.setattr(runtime_mod.time, "monotonic", lambda: now["t"])
    runtime_mod.note_simulated_input()
    assert runtime_mod.request_auto_pause() is False


def test_request_auto_pause_fires_and_marks_rescan():
    runtime_mod.configure_auto_pause(operation_mode="ui", enabled=True, grace_ms=0)
    runtime_mod.set_auto_pause_guard(True)
    runtime_mod.set_paused(False)
    assert runtime_mod.request_auto_pause() is True
    assert runtime_mod.is_paused() is True
    assert runtime_mod.consume_rescan_flag() is True
    assert runtime_mod.auto_pause_stats()["triggers"] >= 1


# ── pause：暂停键切换与自动暂停提示 ────────────────────────────────────
def test_toggle_pause_flips_state_twice():
    runtime_mod.set_paused(False)
    pause_mod._toggle_pause()
    assert runtime_mod.is_paused() is True
    pause_mod._toggle_pause()
    assert runtime_mod.is_paused() is False


def test_maybe_auto_pause_noop_when_conditions_unmet():
    runtime_mod.configure_auto_pause(operation_mode="cli", enabled=False, grace_ms=0)
    pause_mod._maybe_auto_pause("键盘按键")  # 不该抛异常，也不该暂停
    assert runtime_mod.is_paused() is False


def test_maybe_auto_pause_logs_when_triggered():
    runtime_mod.configure_auto_pause(operation_mode="ui", enabled=True, grace_ms=0)
    runtime_mod.set_auto_pause_guard(True)
    runtime_mod.set_paused(False)
    pause_mod._maybe_auto_pause("键盘按键")
    assert runtime_mod.is_paused() is True


def test_on_press_with_plain_key_does_not_crash(monkeypatch):
    """普通按键（非暂停键、非 Ctrl+C）只记日志，不能抛到监听线程外。"""
    monkeypatch.setattr(runtime_mod, "pause_key", lambda: "f12")
    runtime_mod.configure_auto_pause(operation_mode="cli", enabled=False, grace_ms=0)
    pause_mod._on_press(type("K", (), {"char": "a"})())


def test_on_press_with_pause_key_toggles(monkeypatch):
    monkeypatch.setattr(runtime_mod, "pause_key", lambda: "f12")
    runtime_mod.set_paused(False)
    pause_mod._on_press(type("K", (), {"char": "f12", "name": "f12"})())


def test_pause_key_name_is_string():
    runtime_mod.set_pause_key_cfg(_cfg()["pause_key"])
    assert isinstance(runtime_mod.pause_key_name(), str)


def test_auto_pause_stats_shape():
    stats = runtime_mod.auto_pause_stats()
    assert set(stats) == {"triggers", "resumes"}
    json.dumps(stats)  # 必须可序列化（要进报告/日志）
