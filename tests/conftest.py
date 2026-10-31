# -*- coding: utf-8 -*-
"""全局测试隔离。

**绝不让测试拉起真实的 Gui.exe**：`ui.enabled` 默认改成 true 之后，凡是跑 `main()`
的用例都会真的把仓库里已构建的 Gui.exe 拉起来，然后等用户点「开始」（900s 超时），
整套测试因此从 ~2 分钟变成挂死（实测挂在 test_main_flow::test_listener_started）。

统一把 `gui_executable()` 打成 None，让代码走「未找到 → 明确告警并回落控制台」这条
已经实现好的分支。需要真实路径的用例自行 monkeypatch（例如 test_ui_bridge 里那几条）。
"""

from __future__ import annotations

import pytest

from guild_auto.ui import binding as ui_binding


@pytest.fixture(autouse=True)
def _no_real_gui(request, monkeypatch):
    # 需要验证"真实定位逻辑"的用例打 @pytest.mark.real_gui_executable 豁免
    if request.node.get_closest_marker("real_gui_executable"):
        return
    monkeypatch.setattr(ui_binding, "gui_executable", lambda: None)
