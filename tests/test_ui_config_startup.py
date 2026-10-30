# -*- coding: utf-8 -*-
"""§3.5 配置页启动行为：首次运行门禁与热重载分级。"""

from __future__ import annotations

import json

import pytest

from guild_auto.config import load_config, save_config
from guild_auto.config.defaults import DEFAULT_CONFIG
from guild_auto.ui import binding as B


@pytest.fixture()
def bridge(tmp_path):
    path = tmp_path / "config.json"
    save_config(path, json.loads(json.dumps(DEFAULT_CONFIG)))
    return B.GuiBridge(load_config(path), path, report_dir=tmp_path, log_dir=tmp_path)


def test_hello_reports_first_run(tmp_path):
    path = tmp_path / "config.json"
    save_config(path, json.loads(json.dumps(DEFAULT_CONFIG)))
    first = B.GuiBridge(load_config(path), path, first_run=True)
    again = B.GuiBridge(load_config(path), path)
    assert first._hello()["first_run"] is True
    assert again._hello()["first_run"] is False


def test_config_payload_marks_restart_required_paths(bridge):
    fields = bridge._config_payload()["fields"]
    marked = sorted(f["path"] for f in fields if f["needs_restart"])
    assert marked == sorted(B.RESTART_REQUIRED_PATHS)
    # 阈值这类保存即生效，不得打需重启标记
    low = next(f for f in fields if f["path"] == "thresholds.low_demote")
    assert low["needs_restart"] is False


def test_config_payload_never_empty_and_covers_defaults(bridge):
    """配置页不得为空：即使没有任何用户改动，也要渲染全部默认键。"""
    fields = bridge._config_payload()["fields"]
    assert len(fields) > 100
    assert any(f["path"] == "thresholds.high_demote" for f in fields)


def test_save_config_reports_restart_required_only_for_restart_paths(bridge):
    out = bridge.server.handle_line(
        json.dumps(
            {
                "id": 1,
                "method": "save_config",
                "params": {"values": {"thresholds.low_demote": 4000}},
            }
        )
    )
    assert out["ok"] is True
    assert out["result"]["restart_required"] == []

    out2 = bridge.server.handle_line(
        json.dumps({"id": 2, "method": "save_config", "params": {"values": {"log.dir": "logs2"}}})
    )
    assert out2["ok"] is True
    assert out2["result"]["restart_required"] == ["log.dir"]


def test_restart_app_is_registered_and_does_not_block(bridge):
    """立即重启：回包必须先到，替换进程在后台线程里做。"""
    out = bridge.server.handle_line('{"id":3,"method":"restart_app","params":{}}')
    assert out["ok"] is True
    assert out["result"] == {}
