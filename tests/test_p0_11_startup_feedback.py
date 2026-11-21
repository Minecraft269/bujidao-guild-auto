# -*- coding: utf-8 -*-
"""
test_p0_11_startup_feedback.py —— 启动期致命错误的可见反馈（GOALS P0-11）
=====================================================================
验收口径：
  * GUI 变体（无控制台）在交互桌面下，配置校验失败要弹 MessageBoxW；
  * 纯 CLI（有控制台）不弹窗，控制台输出不变；
  * 非交互（无窗口站/服务）不弹窗、不死等；
  * exit code 仍为 1，日志先写、弹窗最后。
"""

import os
import tempfile
import unittest
from unittest import mock

from guild_auto import main as main_mod
from guild_auto.config import ensure_default_config


def _args(config_path):
    return mock.Mock(
        config=config_path,
        admin=None,
        log=None,
        options=None,
        dry_run=False,
        debug=False,
        debug_skip=None,
        ui=None,
    )


class TestStartupFailureText(unittest.TestCase):
    def test_contains_summary_key_log_and_hint(self):
        text = main_mod._startup_failure_text(
            "配置校验未通过",
            ["配置项 game_log 仍是模板占位符。请改成真实路径(键名: game_log)。"],
            r"D:\app\logs\latest.log",
        )
        self.assertIn("启动失败：配置校验未通过", text)
        self.assertIn("错误摘要", text)
        self.assertIn("涉及键名：game_log", text)
        self.assertIn(r"日志：D:\app\logs\latest.log", text)
        self.assertIn("没有控制台窗口是正常现象", text)
        self.assertIn("查看日志", text)

    def test_more_than_three_errors_are_counted(self):
        text = main_mod._startup_failure_text("x", ["e1", "e2", "e3", "e4", "e5"], "log")
        self.assertIn("另有 2 条", text)
        self.assertNotIn("e4", text)

    def test_no_key_when_message_has_none(self):
        text = main_mod._startup_failure_text("x", ["普通错误"], "log")
        self.assertNotIn("涉及键名", text)


class TestDialogNeeded(unittest.TestCase):
    def test_console_build_never_pops(self):
        """纯 CLI：stdout 存在 → 不弹窗（控制台输出不变）。"""
        with (
            mock.patch.object(main_mod.sys, "stdout", object()),
            mock.patch.object(main_mod.sys, "stderr", object()),
            mock.patch.object(main_mod, "_std_handles_absent", return_value=True),
            mock.patch.object(main_mod, "_interactive_desktop", return_value=True),
        ):
            self.assertFalse(main_mod._startup_dialog_needed())

    def test_noconsole_non_interactive_desktop_skips(self):
        with (
            mock.patch.object(main_mod.sys, "stdout", None),
            mock.patch.object(main_mod.sys, "stderr", None),
            mock.patch.object(main_mod, "_std_handles_absent", return_value=True),
            mock.patch.object(main_mod, "_interactive_desktop", return_value=False),
        ):
            self.assertFalse(main_mod._startup_dialog_needed())

    def test_noconsole_interactive_desktop_pops(self):
        with (
            mock.patch.object(main_mod.sys, "stdout", None),
            mock.patch.object(main_mod.sys, "stderr", None),
            mock.patch.object(main_mod, "_std_handles_absent", return_value=True),
            mock.patch.object(main_mod, "_interactive_desktop", return_value=True),
        ):
            self.assertTrue(main_mod._startup_dialog_needed())

    def test_redirected_handles_never_pops(self):
        """被脚本/管道启动（标准句柄存在）≠ 双击：不得挂在弹窗上。"""
        with (
            mock.patch.object(main_mod.sys, "stdout", None),
            mock.patch.object(main_mod.sys, "stderr", None),
            mock.patch.object(main_mod, "_std_handles_absent", return_value=False),
            mock.patch.object(main_mod, "_interactive_desktop", return_value=True),
        ):
            self.assertFalse(main_mod._startup_dialog_needed())


class TestNotifyStartupFailure(unittest.TestCase):
    def test_skips_dialog_when_not_needed(self):
        with mock.patch.object(main_mod, "_startup_dialog_needed", return_value=False):
            with mock.patch("ctypes.windll.user32.MessageBoxW") as box:
                main_mod._notify_startup_failure("s", ["e"], "log")
        box.assert_not_called()

    def test_shows_dialog_when_needed(self):
        with mock.patch.object(main_mod, "_startup_dialog_needed", return_value=True):
            with mock.patch("ctypes.windll.user32.MessageBoxW", return_value=1) as box:
                main_mod._notify_startup_failure("s", ["e"], "log")
        box.assert_called_once()

    def test_dialog_failure_never_raises(self):
        with mock.patch.object(main_mod, "_startup_dialog_needed", return_value=True):
            with mock.patch("ctypes.windll.user32.MessageBoxW", side_effect=OSError("boom")):
                main_mod._notify_startup_failure("s", ["e"], "log")  # 不抛异常即通过


class TestPrepareConfigNotifies(unittest.TestCase):
    def test_config_error_pops_then_exits_1(self):
        """非首启 + 占位符：日志/控制台照旧，弹窗被调用，exit code 仍为 1。"""
        d = tempfile.mkdtemp(prefix="p0_11_")
        path = os.path.join(d, "config.json")
        ensure_default_config(path)  # 已存在 → created=False → 占位符是 error
        printed, notified = [], []

        def _exit(code=0):
            raise SystemExit(code)

        with mock.patch.object(
            main_mod, "_notify_startup_failure", side_effect=lambda *a: notified.append(a)
        ):
            with self.assertRaises(SystemExit) as ctx:
                main_mod.prepare_config(
                    _args(path),
                    console_fn=lambda *a, **k: printed.append(str(a[0])),
                    exit_fn=_exit,
                )
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(len(notified), 1)
        summary, errors, log_path = notified[0]
        self.assertIn("配置校验未通过", summary)
        self.assertTrue(any("game_log" in e for e in errors))
        self.assertTrue(log_path.endswith("latest.log"))
        joined = "\n".join(printed)
        self.assertIn("[错误]", joined)  # 控制台输出没有被弹窗取代


class TestInteractiveDesktopSafety(unittest.TestCase):
    def test_returns_bool_and_never_raises(self):
        self.assertIsInstance(main_mod._interactive_desktop(), bool)


if __name__ == "__main__":
    unittest.main()
