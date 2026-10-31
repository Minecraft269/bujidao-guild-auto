# -*- coding: utf-8 -*-
"""reporter 的三段渲染分支（覆盖率缺口）。

这些分支只有「名册里真的出现黑名单/白名单外/保护名单，或启用了自定义规则，
或出现未知段位/unprocessed 状态」时才会走到 —— 用合成名直调渲染函数最省事。
"""

from __future__ import annotations

from guild_auto.domain.reporter import _render_config, _render_roster, _render_skips


def test_skips_lists_blacklist_whitelist_and_protected():
    lines: list[str] = []
    cfg = {"whitelist": ["p1"], "rank_names": {}}
    run_meta = {
        "roster": [
            {"player": "p1", "status": "blacklist"},
            {"player": "p2", "status": "not_in_whitelist"},
            {"player": "p3", "status": "protected"},
        ]
    }
    _render_skips(lines, [], cfg, run_meta)
    text = "\n".join(lines)
    assert "黑名单跳过 1 人" in text
    assert "白名单外跳过 1 人" in text
    assert "保护名单 1 人" in text


def test_skips_without_whitelist_says_disabled():
    lines: list[str] = []
    cfg = {"rank_names": {}}
    run_meta = {"roster": [{"player": "p2", "status": "not_in_whitelist"}]}
    _render_skips(lines, [], cfg, run_meta)
    assert "白名单未启用" in "\n".join(lines)


def test_config_section_lists_enabled_custom_rules():
    lines: list[str] = []
    cfg = {
        "rules_enabled": True,
        "rules": [
            {"id": "r1", "priority": 3, "name": "合成规则", "enabled": True},
            {"id": "r2", "priority": 1, "name": "已禁用", "enabled": False},
        ],
    }
    _render_config(lines, cfg, {})
    text = "\n".join(lines)
    assert "自定义规则: 1 条启用" in text
    assert "r1" in text


def test_roster_handles_unknown_rank_and_unprocessed():
    lines: list[str] = []
    cfg = {"rank_names": {}}
    run_meta = {
        "roster": [
            {"player": "p1", "status": "ok", "rank": "未知段位"},
            {"player": "p2", "status": "unprocessed", "rank": "未知段位"},
        ],
        "counts": {"member_total": 2},
    }
    _render_roster(lines, [], cfg, run_meta)
    assert "未知段位" in "\n".join(lines)
