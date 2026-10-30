# -*- coding: utf-8 -*-
"""启动期 UI 决策（§2.1 / §6.1 的 S4）：双击就该出界面，缺 Gui.exe 则明确回落。

不做端到端：只锁"决策与拉起"这一段，流程主体另有测试。
"""

from __future__ import annotations

import copy
from unittest import mock

import pytest

from guild_auto import main as main_mod
from guild_auto.config.defaults import DEFAULT_CONFIG


class _FakeBridge:
    instances: list["_FakeBridge"] = []

    def __init__(self, *a, **k):
        self.args = a
        self.kwargs = k
        self.launched = False
        self.events = []
        _FakeBridge.instances.append(self)

    def launch(self, **k):
        self.launched = True
        return False  # 模拟"用户关窗"：main 应立刻 return 0

    def event(self, *a, **k):
        self.events.append((a, k))


@pytest.fixture()
def cfg():
    c = copy.deepcopy(DEFAULT_CONFIG)
    c["ui"]["enabled"] = True
    return c


def _args(tmp_path):
    a = mock.Mock()
    a.ui = False
    a.config = str(tmp_path / "config.json")
    a.debug = False
    a.debug_skip = None
    a.dry_run = False
    return a


def test_main_launches_gui_and_returns_when_window_closed(tmp_path, cfg, monkeypatch):
    _FakeBridge.instances.clear()
    args = _args(tmp_path)
    monkeypatch.setattr(main_mod, "parse_args", lambda: args)
    monkeypatch.setattr(main_mod, "prepare_config", lambda a: copy.deepcopy(cfg))
    monkeypatch.setattr(main_mod, "console", lambda *a, **k: None)
    from guild_auto.ui import binding as ui_binding

    monkeypatch.setattr(ui_binding, "gui_executable", lambda: tmp_path / "Gui.exe")
    monkeypatch.setattr(ui_binding, "GuiBridge", _FakeBridge)
    monkeypatch.setattr(ui_binding, "set_active", lambda b: None)
    monkeypatch.setattr(ui_binding, "RuntimeController", lambda: object())
    monkeypatch.setattr(main_mod.config_mod, "load_config", lambda p: copy.deepcopy(cfg))

    assert main_mod.main() == 0
    assert len(_FakeBridge.instances) == 1
    assert _FakeBridge.instances[0].launched is True
    # 首次运行标记必须透传给桥（S4 决策需要它）
    assert "first_run" in _FakeBridge.instances[0].kwargs


def test_main_falls_back_to_console_when_gui_missing(tmp_path, cfg, monkeypatch):
    """找不到 Gui.exe：告警 + 回落控制台（不 SystemExit）。"""
    _FakeBridge.instances.clear()
    args = _args(tmp_path)
    monkeypatch.setattr(main_mod, "parse_args", lambda: args)
    monkeypatch.setattr(main_mod, "prepare_config", lambda a: copy.deepcopy(cfg))
    logged = []
    monkeypatch.setattr(main_mod, "console", lambda m, *a, **k: logged.append(str(m)))
    from guild_auto.ui import binding as ui_binding

    monkeypatch.setattr(ui_binding, "gui_executable", lambda: None)
    monkeypatch.setattr(ui_binding, "GuiBridge", _FakeBridge)

    # 回落后会继续走主流程；用 runtime 构造失败把它截停，只验证决策分支
    monkeypatch.setattr(main_mod.runtime_mod, "build_runtime", mock.Mock(side_effect=SystemExit(0)))
    with pytest.raises(SystemExit):
        main_mod.main()
    assert _FakeBridge.instances == []
    assert any("Gui.exe" in m for m in logged)
