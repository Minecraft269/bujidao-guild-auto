# -*- coding: utf-8 -*-
"""IPC/桥的推送与异常分支（总体覆盖率欠账）。

这些路径是"GUI 显示得对不对"的直接来源：状态、玩家表、待办、失败、报告、
事件都要能推出去；管道未连接/请求非法时必须安全失败，不能把核带崩。
"""

from __future__ import annotations

import json

import pytest

from guild_auto.config import load_config, save_config
from guild_auto.config.defaults import DEFAULT_CONFIG
from guild_auto.ui import binding as B
from guild_auto.ui.ipc import IpcError, IpcServer


@pytest.fixture()
def bridge(tmp_path):
    path = tmp_path / "config.json"
    save_config(path, json.loads(json.dumps(DEFAULT_CONFIG)))
    br = B.GuiBridge(load_config(path), path, report_dir=tmp_path, log_dir=tmp_path)
    br.sent = []
    br.server.send_event = lambda method, params: br.sent.append((method, params))
    return br


def test_push_state_includes_elapsed_and_payload(bridge):
    bridge.push_state(stage="查询成员", done=3, total=10)
    method, params = bridge.sent[-1]
    assert method == "state"
    assert params["stage"] == "查询成员" and params["done"] == 3


def test_push_players_append_and_replace(bridge):
    bridge.push_players([{"name": "示例玩家"}])
    bridge.push_players([{"name": "张三"}], replace=False)
    assert len(bridge._players) == 2
    bridge.push_players([{"name": "李四"}], replace=True)
    assert [p["name"] for p in bridge._players] == ["李四"]


def test_push_pending_failures_reports(bridge):
    bridge.push_pending([{"player": "示例玩家"}])
    bridge.push_failures([{"player": "张三", "kind": "查询失败"}])
    bridge.push_reports({"basic": "a.md", "full": "b.md"})
    kinds = [m for m, _ in bridge.sent]
    assert kinds[-3:] == ["pending", "failures", "reports"]


def test_event_carries_level_category_and_player(bridge):
    bridge.event("发送 /guild member 示例玩家", level=4, category="send", player="示例玩家")
    method, payload = bridge.sent[-1]
    assert method == "event"
    assert payload["level"] == 4 and payload["category"] == "send"
    assert payload["player"] == "示例玩家"


def test_event_without_player_has_no_player_key(bridge):
    bridge.event("启动完成", category="启动")
    assert "player" not in bridge.sent[-1][1]


def test_push_action_merges_fields(bridge):
    bridge.push_action(cmd="/guild prom 示例玩家", retry=2)
    bridge.push_action(last="成功")
    assert bridge._action["cmd"] == "/guild prom 示例玩家"
    assert bridge._action["last"] == "成功" and bridge._action["retry"] == 2


def test_ipc_send_event_silent_when_not_connected():
    srv = IpcServer({})
    srv.send_event("state", {"a": 1})  # 未连接：静默，不抛
    assert srv.last_error in (None, "")


def test_ipc_write_line_raises_without_connection():
    srv = IpcServer({})
    with pytest.raises(IpcError):
        srv._write_line({"ok": True})


def test_ipc_handle_line_rejects_non_json():
    srv = IpcServer({})
    out = srv.handle_line("not json")
    assert out["ok"] is False and "JSON" in out["error"]


def test_ipc_handle_line_reports_handler_exception_text():
    def boom(_params):
        raise RuntimeError("处理器炸了")

    srv = IpcServer({"boom": boom})
    out = srv.handle_line('{"id": 7, "method": "boom", "params": {}}')
    assert out["ok"] is False
    assert "处理器炸了" in out["error"]
    assert out["id"] == 7


def test_ipc_token_mismatch_stops_server():
    srv = IpcServer({}, token="right")
    out = srv.handle_line('{"id": 1, "method": "x", "token": "wrong"}')
    assert out["ok"] is False and "token" in out["error"]
    assert srv._stop.is_set() is True
