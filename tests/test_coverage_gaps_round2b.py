# -*- coding: utf-8 -*-
"""补 application 层欠账（第二批）：通知阶段、异步查询后处理、鼠标回调。

这三处都是"线上会走到、但现有测试没走过"的分支，属于 docs/覆盖率报告.md 第七节。
全部用假 Runtime / 假 executor，不碰真硬件。
"""

from __future__ import annotations

import copy

import pytest

from guild_auto.application import pause as pause_mod
from guild_auto.application import runtime as runtime_mod
from guild_auto.application import stages
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


class _Human:
    def __init__(self):
        self.waits = 0

    def wait_between_commands(self):
        self.waits += 1


class _Executor:
    """只实现 gc_broadcast_with_retry：按预设序列返回 (ok, err)。"""

    def __init__(self, results):
        self.results = list(results)
        self.sent = []

    def gc_broadcast_with_retry(self, text, retries=3, timeout=4.0):
        self.sent.append(text)
        return self.results.pop(0) if self.results else (True, None)


class _Rt:
    def __init__(self, results, async_enabled=False):
        self.human = _Human()
        self.executor = _Executor(results)
        self.async_enabled = async_enabled
        self.gates = 0
        self.watcher = None
        self.detector = None

    def pause_gate(self):
        self.gates += 1

    def is_paused(self):
        return False

    def reset_window_state_after_pause(self, *a, **k):
        """通知发完后核会重置窗口状态（签名是 (hwnd, detector)）；假 Runtime 只记调用。"""
        return None

    def __getattr__(self, name):
        """兜底：通知路径会摸到 hwnd/detector 等硬件属性。

        测试只关心控制流与返回值，摸到未定义的属性统一当成"无害的空操作"，
        避免每加一个依赖就补一个假字段。真实现的问题由集成测试兜底。
        """
        return lambda *a, **k: None


def _cfg(**switch_over):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["switches"].update(switch_over)
    cfg["switches"]["notify_enabled"] = True
    return cfg


def _records():
    return [
        {"player": "示例玩家", "rank": "member", "weekly_total": 100, "actions": ["promote"]},
        {"player": "张三", "rank": "member", "weekly_total": 0, "actions": []},
    ]


def test_stage_notify_per_player_success_and_failure():
    """per_player 模式逐条发；失败的进 failures，成功的只计数（覆盖 §3.3 的跳过分组来源）。"""
    rt = _Rt(results=[(True, None), (False, "广播回显超时")])
    failures = stages.stage_notify(rt, _records(), _cfg(notify_mode="per_player"))
    assert len(rt.executor.sent) == 2
    assert rt.human.waits == 2
    assert len(failures) == 1
    assert failures[0]["scope"] == "张三"
    assert "回显超时" in failures[0]["error"]


def test_stage_notify_summary_mode_reports_failure():
    rt = _Rt(results=[(False, "网络中断")])
    failures = stages.stage_notify(rt, _records(), _cfg(notify_mode="summary"))
    assert failures == [{"scope": "summary", "error": "网络中断"}]


def test_stage_notify_skipped_paths_are_silent():
    rt = _Rt(results=[])
    assert stages.stage_notify(rt, _records(), _cfg(), debug_notify_skipped=True) == []
    assert stages.stage_notify(rt, _records(), _cfg(notify_enabled=False)) == []
    assert stages.stage_notify(rt, [], _cfg()) == []


def test_stage_query_async_post_processes_snapshots(monkeypatch):
    """异步查询的后处理：成功快照回填 rank、失败快照写原因、屏蔽词单独归类。"""
    from guild_auto.concurrency import async_system

    monkeypatch.setattr(
        async_system,
        "run_async_round",
        lambda system, commands, send_only, max_retries: (
            [
                {
                    "player": "示例玩家",
                    "data": {
                        "player": "示例玩家",
                        "weekly_total": 10,
                        "joined_at": "2026-01-01",
                        "last_online": "2026-10-01",
                    },
                },
                {"player": "李四", "data": None},  # 非 dict：不得进 records
            ],
            [
                {"player": "王五", "error": "等待响应超时"},
                {"player": "***", "error": None},
            ],
        ),
    )
    rt = _Rt(results=[])
    cfg = _cfg()
    cfg["commands"]["guild_member"] = "/guild member {player}"
    records, failures = stages.stage_query_async(rt, [("member", "示例玩家")], cfg)
    assert [r["player"] for r in records] == ["示例玩家"]
    assert records[0]["rank"] == "member"
    reasons = {f["player"]: f["reason"] for f in failures}
    assert reasons["王五"] == "等待响应超时"
    assert "屏蔽" in reasons["***"]


def test_mouse_callbacks_trigger_auto_pause():
    runtime_mod.configure_auto_pause(operation_mode="ui", enabled=True, grace_ms=0)
    runtime_mod.set_auto_pause_guard(True)
    runtime_mod.set_paused(False)
    pause_mod._on_mouse_move(1, 2)
    assert runtime_mod.is_paused() is True
    runtime_mod.set_paused(False)
    pause_mod._on_mouse_click(1, 2, "left", False)  # 抬起不触发
    assert runtime_mod.is_paused() is False
    pause_mod._on_mouse_click(1, 2, "left", True)  # 按下触发
    assert runtime_mod.is_paused() is True


def test_debug_records_else_branch_marks_no_action(monkeypatch):
    """未跳过判定时动作留空、trigger 为 None：不得凭空造动作。"""
    import random as _random

    from guild_auto.application import debug_data

    real_choice = _random.choice
    monkeypatch.setattr(
        _random, "choice", lambda seq: None if seq and seq[-1] is None else real_choice(seq)
    )
    cfg = _cfg()
    cfg["debug"]["simulation"]["players_per_rank"] = 1
    guild = debug_data._generate_debug_guild(cfg)
    rank = next(iter(guild["groups"]))
    # decide_skipped=False 才是「等着阶段 3 判定」的路径：动作必须留空
    recs = debug_data._generate_debug_records(
        cfg, guild, [(rank, guild["groups"][rank][0])], decide_skipped=False
    )
    assert recs[0]["actions"] == []
    assert recs[0]["trigger"] is None


def test_query_async_inner_closures_cover_send_and_parse_error(monkeypatch):
    """让 run_async_round 真正调用 send_only / verify：覆盖「发命令打印」与「解析失败」。"""
    from guild_auto.concurrency import async_system

    sent = []

    class FakeWatcher:
        def wait_for_chat_block(self, timeout=0, stop_check=None):
            return ["完全无法解析的一行"]

    class FakeHuman:
        def send_chat_text(self, cmd, detector=None, stop_check=None):
            sent.append(cmd)
            return True

        def wait_between_commands(self):
            pass

    rt = _Rt(results=[])
    rt.watcher = FakeWatcher()
    rt.human = FakeHuman()

    def fake_round(system, commands, send_only, max_retries):
        for cmd, player in commands:
            assert send_only(cmd, player, {}) is True
            ok, reason, _info = system._verify_fn(cmd, player, {})
            assert ok is False and "解析失败" in reason
        return [], []

    monkeypatch.setattr(async_system, "run_async_round", fake_round)
    cfg = _cfg()
    cfg["commands"]["guild_member"] = "/guild member {player}"
    records, failures = stages.stage_query_async(rt, [("member", "示例玩家")], cfg)
    assert sent == ["/guild member 示例玩家"]
    assert records == [] and failures == []
