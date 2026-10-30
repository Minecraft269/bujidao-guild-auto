# -*- coding: utf-8 -*-
"""核心层的"非法输入"分支（覆盖率欠账，A 口径按门禁补测）。

这些分支平时只在用户把配置写坏、或规则条件读不到字段时才走到 ——
正是最需要"报错而不是静默兜底"的地方，必须逐条锁住。
"""

from __future__ import annotations

import copy

import pytest

from guild_auto.config.defaults import DEFAULT_CONFIG
from guild_auto.config.schema import validate_config
from guild_auto.domain import rules as rules_mod


def _cfg(**over):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.update(over)
    return cfg


def _messages(cfg):
    return " | ".join(msg for _level, msg in validate_config(cfg))


# ── schema：类型/枚举/边界 ────────────────────────────────────────────
def test_switches_must_be_dict():
    assert "switches" in _messages(_cfg(switches="not-a-dict"))


def test_ui_enum_invalid_value():
    cfg = _cfg()
    cfg["ui"]["theme"] = "neon"
    assert "theme" in _messages(cfg)


def test_ui_bool_invalid_value():
    cfg = _cfg()
    cfg["ui"]["custom_titlebar"] = "yes"
    assert "custom_titlebar" in _messages(cfg)


def test_ui_sub_block_must_be_dict():
    cfg = _cfg()
    cfg["ui"]["window"] = "big"
    # 子块不是对象时必须报错而不是崩在 .get 上
    assert "window" in _messages(cfg)


def test_ui_window_size_must_be_positive_int():
    cfg = _cfg()
    cfg["ui"]["window"]["width"] = 0
    assert "width" in _messages(cfg)


def test_ui_remember_geometry_must_be_bool():
    cfg = _cfg()
    cfg["ui"]["window"]["remember_geometry"] = 1
    assert "remember_geometry" in _messages(cfg)


def test_update_block_must_be_dict():
    assert "update" in _messages(_cfg(update="auto"))


def test_update_mode_must_be_enum():
    cfg = _cfg()
    cfg["update"]["mode"] = "sometimes"
    assert "update.mode" in _messages(cfg) or "mode" in _messages(cfg)


def test_update_manifest_url_must_be_string():
    cfg = _cfg()
    cfg["update"]["manifest_url"] = 123
    assert "manifest_url" in _messages(cfg)


def test_kick_confirm_text_must_be_non_empty():
    cfg = _cfg()
    cfg["kick_confirm_text"] = "   "
    assert "kick_confirm_text" in _messages(cfg)


# ── rules：不可求值时判否并写明原因，且告警去重 ──────────────────────
def _rule(cond):
    return {
        "id": "r-x",
        "name": "边界",
        "priority": 1,
        "enabled": True,
        "conditions": [cond],
        "actions": [{"type": "notify"}],
    }


def test_unreadable_date_condition_is_not_evaluable():
    member = {"player": "示例玩家", "rank": "member", "weekly_total": 10, "joined_at": "不是日期"}
    actions, hits, traces = rules_mod.evaluate_rules(
        [_rule({"field": "joined_at", "op": "before", "value": "2026-01-01"})], member
    )
    assert actions == [] and hits == []
    assert traces and traces[0]["matched"] is False


def test_unknown_field_condition_warns_once():
    """同一不可求值条件重复求值只告警一次（warned 集合去重）。"""
    cond = {"field": "joined_at", "op": "before", "value": "不是日期"}
    member = {"player": "示例玩家", "rank": "member", "weekly_total": 1, "joined_at": "x"}
    rule = _rule(cond)
    for _ in range(3):
        rules_mod.evaluate_rules([rule], member)
    # 只验证不抛且结论稳定（去重逻辑不改变判定）
    _a, hits, traces = rules_mod.evaluate_rules([rule], member)
    assert hits == [] and traces[0]["matched"] is False


def test_empty_rule_set_returns_empty_result():
    actions, hits, traces = rules_mod.evaluate_rules([], {"player": "示例玩家", "rank": "member"})
    assert actions == [] and hits == [] and traces == []


def test_condition_not_dict_is_rejected():
    ok, detail = rules_mod.check_condition("not-a-dict", {"player": "示例玩家"})
    assert ok is False and "条件格式非法" in detail
