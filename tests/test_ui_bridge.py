# -*- coding: utf-8 -*-
"""GUI 桥的协议级测试：方法表返回什么、写回是否拒绝只读、确认是否真能解除阻塞。

不启动 GUI 进程，也不建真实管道（IpcServer 只有 create() 才建管道），
所以这些用例是纯逻辑级的：同一条路径既被 GUI 走，也被单测走。
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
    cfg = load_config(path)
    return B.GuiBridge(cfg, path, report_dir=tmp_path, log_dir=tmp_path)


def test_hello_carries_variant_and_capabilities(bridge):
    out = bridge.server.handle_line('{"id":1,"method":"hello","params":{}}')
    assert out["ok"] is True
    result = out["result"]
    assert result["variant"]
    assert isinstance(result["capabilities"], list)
    assert result["config_schema_version"] >= 1


def test_config_payload_covers_every_field_and_marks_build_variant_readonly(bridge):
    out = bridge.server.handle_line('{"id":2,"method":"config","params":{}}')
    fields = out["result"]["fields"]
    paths = {f["path"] for f in fields}
    assert "thresholds.high_demote" in paths  # 与 config_panel 一一对应
    assert "ui.custom_titlebar" in paths
    readonly = [f for f in fields if f["readonly"]]
    assert [f["path"] for f in readonly] == ["build_variant"]
    assert all(f.get("comment") for f in fields)  # 每项都有说明


def test_save_config_rejects_readonly_field(bridge):
    out = bridge.server.handle_line(
        json.dumps(
            {"id": 3, "method": "save_config", "params": {"values": {"build_variant": "Lite"}}},
            ensure_ascii=False,
        )
    )
    assert out["ok"] is False
    assert "只读" in out["error"]


def test_save_config_writes_and_reloads(bridge):
    out = bridge.server.handle_line(
        json.dumps(
            {"id": 4, "method": "save_config", "params": {"values": {"thresholds.low_demote": 4321}}}
        )
    )
    assert out["ok"] is True, out
    reloaded = load_config(bridge.config_path)
    assert reloaded["thresholds"]["low_demote"] == 4321


def test_rules_payload_includes_builtin_readonly(bridge):
    out = bridge.server.handle_line('{"id":5,"method":"rules","params":{}}')
    rules = out["result"]["rules"]
    assert any(r.get("builtin") for r in rules)


def test_save_rules_rejects_rule_without_conditions(bridge):
    bad = [{"id": "x", "priority": 1, "conditions": [], "actions": [{"type": "kick"}]}]
    out = bridge.server.handle_line(
        json.dumps({"id": 6, "method": "save_rules", "params": {"rules": bad}})
    )
    assert out["ok"] is False
    assert "conditions" in out["error"]


def test_save_rules_persists_custom_rules_only(bridge):
    good = [
        {
            "id": "r1",
            "name": "测试",
            "priority": 10,
            "enabled": True,
            "conditions": [{"field": "weekly_total", "op": "<", "value": 10}],
            "actions": [{"type": "notify"}],
        }
    ]
    out = bridge.server.handle_line(
        json.dumps({"id": 7, "method": "save_rules", "params": {"rules": good}})
    )
    assert out["ok"] is True, out
    reloaded = load_config(bridge.config_path)
    assert [r["id"] for r in reloaded["rules"]] == ["r1"]


def test_preview_rule_uses_production_evaluator(bridge):
    rules = [
        {
            "id": "kick-low",
            "priority": 1,
            "enabled": True,
            "conditions": [{"field": "weekly_total", "op": "<", "value": 1000}],
            "actions": [{"type": "kick"}],
        }
    ]
    sample = {"player": "示例玩家", "rank": "member", "weekly_total": 5}
    out = bridge.server.handle_line(
        json.dumps(
            {"id": 8, "method": "preview_rule", "params": {"rules": rules, "sample": sample}}
        )
    )
    assert out["ok"] is True
    assert out["result"]["actions"] == ["kick"]
    assert out["result"]["hit_rule_ids"] == ["kick-low"]


class _FakeController:
    def __init__(self):
        self.calls = []

    def pause(self, reason=""):
        self.calls.append(("pause", reason))

    def resume(self):
        self.calls.append(("resume",))

    def stop(self):
        self.calls.append(("stop",))


def test_control_methods_delegate_to_controller(tmp_path):
    path = tmp_path / "config.json"
    save_config(path, json.loads(json.dumps(DEFAULT_CONFIG)))
    ctrl = _FakeController()
    br = B.GuiBridge(load_config(path), path, controller=ctrl)
    for method in ("pause", "resume", "stop"):
        out = br.server.handle_line(json.dumps({"id": 9, "method": method, "params": {}}))
        assert out["ok"] is True
    assert ctrl.calls == [("pause", "手动"), ("resume",), ("stop",)]


def test_start_sets_started_event(bridge):
    assert not bridge._started.is_set()
    bridge.server.handle_line('{"id":10,"method":"start","params":{}}')
    assert bridge._started.is_set()


def test_ask_confirm_unblocks_on_reply(bridge):
    """GUI 回 confirm_reply 后，核侧的 ask_confirm 必须真的返回。"""
    got = {}

    def wait_reply():
        got["choice"] = bridge.ask_confirm("stage", stage="查询", target=10, actions=5)

    t = threading.Thread(target=wait_reply)
    t.start()
    for _ in range(400):
        if bridge._confirm:
            break
        threading.Event().wait(0.005)
    token = next(iter(bridge._confirm))
    bridge.server.handle_line(
        json.dumps(
            {"id": 11, "method": "confirm_reply", "params": {"id": token, "choice": "resume"}}
        )
    )
    t.join(timeout=2)
    assert got["choice"] == "resume"


def test_confirm_reply_for_unknown_token_is_rejected(bridge):
    out = bridge.server.handle_line(
        '{"id":12,"method":"confirm_reply","params":{"id":"nope","choice":"resume"}}'
    )
    assert out["ok"] is False


def test_unknown_method_reports_error(bridge):
    out = bridge.server.handle_line('{"id":13,"method":"nope","params":{}}')
    assert out["ok"] is False
    assert "未知方法" in out["error"]


def test_rows_from_records_marks_masked_and_failures():
    records = [
        {
            "player": "张三",
            "rank": "member",
            "weekly_total": 100,
            "daily": [10, 20, 0, 0, 0, 0, 0],
            "last_online": "2026-10-01",
        },
        {"player": "***", "rank": "member", "weekly_total": 0, "daily": [], "last_online": ""},
    ]
    failures = [{"player": "李四", "rank": "member", "reason": "等待响应超时"}]
    rows = B.rows_from_records(records, failures)
    assert [r["index"] for r in rows] == [1, 2, 3]
    assert rows[1]["masked"] is True
    assert "屏蔽" in rows[1]["note"]
    assert rows[2]["exec_state"] == "failed"
    assert rows[2]["note"] == "等待响应超时"


def test_gui_executable_respects_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "Gui.exe"
    fake.write_bytes(b"MZ")
    monkeypatch.setenv("BUJIDAO_GUI", str(fake))
    assert B.gui_executable() == fake


def test_open_dir_rejects_missing_path(tmp_path):
    br = B.GuiBridge(DEFAULT_CONFIG, tmp_path / "c.json", report_dir=tmp_path / "missing")
    out = br.server.handle_line('{"id":14,"method":"open_reports","params":{}}')
    assert out["ok"] is False
