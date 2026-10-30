# -*- coding: utf-8 -*-
"""§3.4 模态接线与待办推送的协议级测试。

确认点必须走 GUI 模态（不再读控制台）：这里验证 ask_stage / ask_kick_text 的
语义与 stages 里的判定完全一致 —— 尤其 kick 的「逐字匹配」不能被布尔化。
"""

from __future__ import annotations

import json
import threading

import pytest

from guild_auto.config import load_config, save_config
from guild_auto.config.defaults import DEFAULT_CONFIG
from guild_auto.ui import binding as B


@pytest.fixture()
def bridge(tmp_path):
    path = tmp_path / "config.json"
    save_config(path, json.loads(json.dumps(DEFAULT_CONFIG)))
    return B.GuiBridge(load_config(path), path, report_dir=tmp_path, log_dir=tmp_path)


def _answer_next(bridge, choice):
    """等桥登记了待确认 token 后回一个选项（模拟用户点了按钮）。"""
    for _ in range(400):
        if bridge._confirm:
            break
        threading.Event().wait(0.005)
    token = next(iter(bridge._confirm))
    bridge.server.handle_line(
        json.dumps({"id": 1, "method": "confirm_reply", "params": {"id": token, "choice": choice}})
    )


def test_ask_stage_resume_returns_true(bridge):
    out = {}

    def run():
        out["ok"] = bridge.ask_stage(
            "是否执行?", True, {"stage": "执行确认", "target": 5, "actions": 7}
        )

    t = threading.Thread(target=run)
    t.start()
    _answer_next(bridge, "resume")
    t.join(timeout=2)
    assert out["ok"] is True


def test_ask_stage_stop_returns_false(bridge):
    out = {}

    def run():
        out["ok"] = bridge.ask_stage("是否执行?", True, {"stage": "执行确认"})

    t = threading.Thread(target=run)
    t.start()
    _answer_next(bridge, "stop")
    t.join(timeout=2)
    assert out["ok"] is False


def test_ask_kick_text_returns_typed_text_for_stage_comparison(bridge):
    """stages 用 `ans != expected` 判定，所以模态确认后必须返回确认词原文。"""
    out = {}

    def run():
        out["ans"] = bridge.ask_kick_text(
            "确认?",
            {
                "players": [{"player": "示例玩家", "level": "member", "weekly_total": 100}],
                "confirm_text": "kick-confirm",
            },
        )

    t = threading.Thread(target=run)
    t.start()
    _answer_next(bridge, "kick")
    t.join(timeout=2)
    assert out["ans"] == "kick-confirm"


def test_ask_kick_text_cancel_returns_empty(bridge):
    out = {}

    def run():
        out["ans"] = bridge.ask_kick_text("确认?", {"confirm_text": "kick-confirm"})

    t = threading.Thread(target=run)
    t.start()
    _answer_next(bridge, "cancel")
    t.join(timeout=2)
    assert out["ans"] == ""


def test_confirm_payload_carries_stage_context(bridge):
    """模态需要阶段名/目标人数/预计动作数，不能只有一句 prompt。"""
    seen = {}
    orig = bridge.server.send_event

    def spy(method, params):
        if method == "confirm":
            seen.update(params)
        return orig(method, params)

    bridge.server.send_event = spy
    t = threading.Thread(
        target=lambda: bridge.ask_stage(
            "p", True, {"stage": "执行确认", "target": 9, "actions": 11}
        )
    )
    t.start()
    _answer_next(bridge, "resume")
    t.join(timeout=2)
    assert seen["kind"] == "stage"
    assert seen["stage"] == "执行确认"
    assert seen["target"] == 9
    assert seen["actions"] == 11


def test_pending_rows_maps_items_for_the_queue():
    items = [
        {
            "player": "示例玩家",
            "actions": ["promote", "notify"],
            "reason": "上次未完成",
            "enqueued_at": "2026-10-30T10:00:00",
            "retries": 2,
        },
        {"player": "张三", "actions": ["kick"], "error": "执行失败"},
    ]
    rows = B.pending_rows(items)
    assert rows[0] == {
        "player": "示例玩家",
        "action": "promote,notify",
        "reason": "上次未完成",
        "enqueued_at": "2026-10-30T10:00:00",
        "retries": 2,
    }
    assert rows[1]["reason"] == "执行失败"
    assert rows[1]["retries"] == 0
    assert B.pending_rows(None) == []
