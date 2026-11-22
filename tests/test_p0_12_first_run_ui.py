# -*- coding: utf-8 -*-
"""
test_p0_12_first_run_ui.py —— 首启 UI 判定顺序（GOALS P0-12）
=============================================================
规则：首启只有「--ui 或 ui.enabled」+「变体具备 gui 能力」+「Gui.exe 可定位」三者齐备
才进配置页；任一不满足 → 控制台首启停机（交互等回车、非交互直接退出 0），绝不再冲进 S8。
无 gui 能力的变体不查 Gui.exe、不打印「即将打开配置页」；--ui 显式指定仍报错退出 1。
控制台 S8 等待文案必须写「按 Ctrl+C 取消」，GUI 路径保持「点「停止」」。
"""

import os
import tempfile
import unittest
from unittest import mock

from guild_auto import main as main_mod
from guild_auto.ui import binding as ui_binding


def _args(path, ui=False):
    a = mock.Mock()
    a.config = path
    a.admin = None
    a.log = None
    a.options = None
    a.dry_run = False
    a.debug = False
    a.debug_skip = None
    a.ui = ui
    return a


def _run_prepare(tmp, *, ui=False, gui_capable=True, gui_exe=None, isatty=False):
    """跑一次首启 prepare_config，返回 (exit_code|None, 控制台行列表, gui_executable mock, input mock)。"""
    path = os.path.join(tmp, "config.json")
    printed = []

    def _exit(code=0):
        raise SystemExit(code)

    has = mock.Mock(side_effect=lambda cap: (cap != "gui") or gui_capable)
    gui = mock.Mock(return_value=gui_exe)
    with (
        mock.patch.object(main_mod.capabilities_mod, "has", has),
        mock.patch.object(ui_binding, "gui_executable", gui),
        mock.patch.object(main_mod.sys.stdin, "isatty", return_value=isatty),
        mock.patch("builtins.input", side_effect=lambda *_a: "") as fake_input,
    ):
        try:
            main_mod.prepare_config(
                _args(path, ui=ui),
                console_fn=lambda *a, **k: printed.append(str(a[0])),
                exit_fn=_exit,
            )
            code = None
        except SystemExit as e:
            code = e.code
    return code, printed, gui, fake_input


class TestFirstRunDecision(unittest.TestCase):
    def test_no_gui_capability_goes_console_and_stops(self):
        """Lite（无 gui 能力）+ ui.enabled=true：不进配置页、不查 Gui.exe、停机退出 0。"""
        with tempfile.TemporaryDirectory() as tmp:
            code, printed, gui, fake_input = _run_prepare(tmp, gui_capable=False, isatty=True)
        joined = "\n".join(printed)
        self.assertNotIn("即将打开配置页", joined)
        self.assertIn("该构建不含界面能力", joined)
        self.assertIn("按回车退出", joined)
        self.assertEqual(code, 0)
        gui.assert_not_called()
        fake_input.assert_called_once()

    def test_gui_capable_but_exe_missing_stops(self):
        """有 gui 能力但目录里没有 Gui.exe：同样控制台停机，不冲 S8。"""
        with tempfile.TemporaryDirectory() as tmp:
            code, printed, gui, _ = _run_prepare(tmp, gui_capable=True, gui_exe=None, isatty=True)
        joined = "\n".join(printed)
        self.assertNotIn("即将打开配置页", joined)
        self.assertIn("未找到 Gui.exe", joined)
        self.assertIn("按回车退出", joined)
        self.assertEqual(code, 0)
        gui.assert_called_once()

    def test_gui_ready_enters_config_page(self):
        """三者齐备（ui.enabled + gui 能力 + Gui.exe）：进配置页，不退出（P0-9b 不回退）。"""
        with tempfile.TemporaryDirectory() as tmp:
            code, printed, gui, _ = _run_prepare(
                tmp, gui_capable=True, gui_exe="Gui.exe", isatty=True
            )
        joined = "\n".join(printed)
        self.assertIn("即将打开配置页", joined)
        self.assertIsNone(code)
        gui.assert_called_once()

    def test_explicit_ui_without_capability_errors_exit_1(self):
        """--ui 传给无 gui 能力的变体：能力门禁报错，exit 1（不按控制台首启放行）。"""
        with tempfile.TemporaryDirectory() as tmp:
            code, printed, gui, _ = _run_prepare(tmp, ui=True, gui_capable=False, isatty=True)
        joined = "\n".join(printed)
        self.assertEqual(code, 1)
        self.assertIn("[错误]", joined)
        self.assertIn("gui", joined)

    def test_non_interactive_first_run_exits_without_waiting(self):
        """非交互终端：不调用 input、不挂起，退出码 0。"""
        with tempfile.TemporaryDirectory() as tmp:
            code, _printed, _gui, fake_input = _run_prepare(tmp, gui_capable=False, isatty=False)
        self.assertEqual(code, 0)
        fake_input.assert_not_called()


class TestS8WaitWording(unittest.TestCase):
    def _waiting(self, bridge):
        wd = main_mod._StartupWatchdog("unused.log")
        logged = []
        with (
            mock.patch.object(main_mod.logger, "log_info", side_effect=logged.append),
            mock.patch.object(ui_binding, "active", return_value=bridge),
        ):
            wd._report_waiting({"path": "game.log", "expected_s": 60})
        return "\n".join(str(x) for x in logged)

    def test_console_wait_says_ctrl_c(self):
        text = self._waiting(None)
        self.assertIn("按 Ctrl+C 取消", text)
        self.assertNotIn("点「停止」", text)

    def test_gui_wait_keeps_stop_wording(self):
        bridge = mock.Mock()
        bridge._state = {"stage": ""}
        bridge.push_state = mock.Mock()
        text = self._waiting(bridge)
        self.assertIn("点「停止」", text)
        self.assertNotIn("按 Ctrl+C", text)


class TestS4NoGuiCapability(unittest.TestCase):
    def test_no_gui_variant_never_searches_gui(self):
        """ui.enabled=true 但构建无 gui 能力：S4 不查 Gui.exe，提示按控制台运行。"""
        cfg = {
            "ui": {"enabled": True},
            "switches": {},
            "report_dir": "reports",
            "log": {"dir": "logs"},
            "operation_mode": "command",
            "async": {"enabled": False},
            "rules_enabled": False,
            "rules": [],
        }
        logged = []
        args = _args("X.json")
        gui = mock.Mock(return_value="Gui.exe")
        with (
            mock.patch.object(main_mod, "parse_args", lambda: args),
            mock.patch.object(main_mod, "prepare_config", lambda a: cfg),
            mock.patch.object(
                main_mod.capabilities_mod, "has", side_effect=lambda cap: cap != "gui"
            ),
            mock.patch.object(ui_binding, "gui_executable", gui),
            mock.patch.object(
                main_mod, "console", side_effect=lambda m, *a, **k: logged.append(str(m))
            ),
            mock.patch.object(main_mod.runtime_mod, "build_runtime", side_effect=SystemExit(0)),
        ):
            with self.assertRaises(SystemExit):
                main_mod.main()
        gui.assert_not_called()
        self.assertTrue(any("不含界面能力" in x for x in logged), logged)


if __name__ == "__main__":
    unittest.main()
