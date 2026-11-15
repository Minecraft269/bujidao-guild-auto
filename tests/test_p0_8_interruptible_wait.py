# -*- coding: utf-8 -*-
"""P0-8 启动等待不可中断 —— 回归测试

实机现象（2026-11-15 11:17–11:19）：game_log 指向不存在的文件 → S8 硬等 60s，
期间 pause/resume/stop/start 四个 RPC 全 ok=True 但程序毫无反应，等满 60.1s 才
FileNotFoundError 崩溃，且 ipc-closed 的 reason 竟然是 normal。

这里锁定四件事：
  1. 等待可被「停止」打断（≤1s 级，不再是等满剩余预算）
  2. 暂停挂起等待、**不消耗超时预算**、且不得当停止处理
  3. 超时仍是原来的 FileNotFoundError（配置错误），口径不变
  4. 退出原因分类：user_stop / config_error / crash，崩溃路径绝不再落 normal，
     也不再把配置错误打成「脚本意外关闭(非用户手动)」
"""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

from guild_auto.application import runtime as runtime_mod
from guild_auto.infrastructure.log_watcher import LogWaitInterrupted, LogWatcher
from guild_auto import main as main_mod


def _missing_path():
    return os.path.join(tempfile.mkdtemp(prefix="p0_8_"), "latest.log")


class TestOpenIsInterruptible(unittest.TestCase):
    """三条时序用例都由 open() 的 stop_check 回调**自己**驱动,不用定时器线程。

    教训（PR #47 的 py3.10 CI 实红）:用 `threading.Timer(0.3, set_force_stop)` 时,
    慢机上线程晚到会把 force_stop 漏进下一例 —— 本课第二例当场抛 LogWaitInterrupted、
    第一例却先超时成 FileNotFoundError。改成"第 N 次检查时发信号"后完全确定。
    """

    def setUp(self):
        runtime_mod.reset_state()  # 先清:上一例可能留下 force_stop
        self.addCleanup(runtime_mod.reset_state)

    def test_stop_interrupts_wait_within_one_second(self):
        path = _missing_path()
        seen = {}

        def stop_at_second_check():
            seen["n"] = seen.get("n", 0) + 1
            if seen["n"] >= 2:  # 第一次检查后 0.1s 发「停止」
                runtime_mod.set_force_stop(True)
            return False

        t0 = time.monotonic()
        with self.assertRaises(LogWaitInterrupted):
            LogWatcher(path).open(wait_seconds=30, stop_check=stop_at_second_check)
        took = time.monotonic() - t0
        self.assertLess(took, 1.0, f"停止后 {took:.2f}s 才退出等待（要求 ≤1s）")
        self.assertGreaterEqual(took, 0.05, f"停止信号在第二次检查才发，却 {took:.2f}s 就退出")

    def test_pause_suspends_budget_and_is_not_stop(self):
        """先暂停 15 次检查(1.5s)，再放开 1.0s 预算：暂停期间绝不能超时消耗预算。"""
        path = _missing_path()
        seen = {}

        def paused_for_first_15():
            seen["n"] = seen.get("n", 0) + 1
            return seen["n"] <= 15

        t0 = time.monotonic()
        with self.assertRaises(FileNotFoundError) as ctx:
            LogWatcher(path).open(wait_seconds=1.0, stop_check=paused_for_first_15)
        took = time.monotonic() - t0
        self.assertIn("游戏日志不存在", str(ctx.exception))
        self.assertGreaterEqual(
            seen["n"], 25, f"只检查了 {seen['n']} 次 —— 暂停期间预算被消耗/提前超时"
        )
        self.assertGreaterEqual(took, 2.0, f"{took:.2f}s 就超时了 —— 暂停消耗了预算")

    def test_pause_alone_never_times_out(self):
        """预算 0.1s + 一直暂停：只要还有暂停，就永远不超时；停止仍能打断。"""
        path = _missing_path()
        seen = {}

        def always_paused_stop_after_10():
            seen["n"] = seen.get("n", 0) + 1
            if seen["n"] >= 10:  # 预算(0.1s)早该耗尽,但一直暂停 → 不许超时
                runtime_mod.set_force_stop(True)
            return True

        with self.assertRaises(LogWaitInterrupted):
            LogWatcher(path).open(wait_seconds=0.1, stop_check=always_paused_stop_after_10)
        self.assertGreaterEqual(seen["n"], 10, "暂停期间因预算耗尽提前退出（暂停被当成了超时）")

    def test_timeout_keeps_original_error(self):
        path = _missing_path()
        t0 = time.monotonic()
        with self.assertRaises(FileNotFoundError) as ctx:
            LogWatcher(path).open(wait_seconds=0.2)
        self.assertIn("game_log", str(ctx.exception))
        self.assertLess(time.monotonic() - t0, 2.0)


class TestExitReasonIsHonest(unittest.TestCase):
    def setUp(self):
        # 模块级 _session_bridge / _pinned_exit 会被别的用例(驱动 main() 的 UI 路径)
        # 留下残余，这里先清空，避免本类断言打到别人那座桥/那个原因上。
        main_mod._reset_exit_state()
        self.addCleanup(main_mod._reset_exit_state)

    def test_classification(self):
        cls = main_mod._classify_exit
        self.assertEqual(cls(LogWaitInterrupted("/x", 1.0))[0], "user_stop")
        self.assertEqual(cls(KeyboardInterrupt())[0], "user_stop")
        self.assertEqual(cls(FileNotFoundError("游戏日志不存在: /x"))[0], "config_error")
        self.assertEqual(cls(SystemExit(2))[0], "config_error")
        self.assertEqual(cls(SystemExit(0))[0], "normal")
        self.assertEqual(cls(SystemExit(None))[0], "normal")
        self.assertEqual(cls(RuntimeError("boom"))[0], "crash")
        self.assertEqual(cls(FileNotFoundError("别的文件"))[0], "crash")

    def test_other_config_env_errors_are_config_error(self):
        """手改坏的 config.json / 日志被占用 / 路径写成目录 —— 都是配置问题,不是崩溃。

        独立审查必修 2:旧口径只认「游戏日志不存在」一条文案,这三类会继续被
        打成「脚本意外关闭(非用户手动)」。
        """
        cls = main_mod._classify_exit
        broken_json = json.JSONDecodeError("Expecting ',' delimiter", '{"a": 1 "b": 2}', 7)
        self.assertEqual(cls(broken_json)[0], "config_error")
        self.assertEqual(
            cls(PermissionError(13, "拒绝访问", "<synthetic>/latest.log"))[0], "config_error"
        )
        self.assertEqual(
            cls(IsADirectoryError(21, "是目录", "<synthetic>/logs"))[0], "config_error"
        )

    def test_launch_phase_crash_reaches_session_bridge(self):
        """launch() 阶段崩溃:binding.active() 还没登记,也必须归因到本轮那座桥。"""
        bridge = mock.Mock()
        bridge.exit_reason = "normal"
        main_mod._set_session_bridge(bridge)
        self.addCleanup(main_mod._set_session_bridge, None)
        with (
            mock.patch.object(main_mod, "_gui_bridge", return_value=None),
            mock.patch.object(main_mod, "main", side_effect=RuntimeError("launch 内炸了")),
            mock.patch.object(main_mod.logger, "log_exception"),
            mock.patch.object(main_mod, "console"),
        ):
            with self.assertRaises(SystemExit) as ctx:
                main_mod.run_cli()
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(
            bridge.exit_reason,
            "crash",
            "ipc-closed 会带着默认 normal —— launch 阶段的崩溃仍被说成正常退出",
        )

    def test_config_error_never_logged_as_unexpected_close(self):
        """崩溃路径(日志缺失):日志必须是「启动失败(配置错误)」,不是「脚本意外关闭」。"""
        exc = FileNotFoundError("游戏日志不存在: X")
        with (
            mock.patch.object(main_mod, "main", side_effect=exc),
            mock.patch.object(main_mod.logger, "log_exception") as log_exc,
            mock.patch.object(main_mod, "console"),
        ):
            with self.assertRaises(SystemExit) as ctx:
                main_mod.run_cli()
        self.assertEqual(ctx.exception.code, 1)
        msg = log_exc.call_args[0][0]
        self.assertIn("配置错误", msg)
        self.assertNotIn("意外关闭", msg)

    def test_user_stop_exits_zero_without_crash_log(self):
        exc = LogWaitInterrupted("/x", 2.0)
        with (
            mock.patch.object(main_mod, "main", side_effect=exc),
            mock.patch.object(main_mod.logger, "log_exception") as log_exc,
            mock.patch.object(main_mod, "console"),
        ):
            with self.assertRaises(SystemExit) as ctx:
                main_mod.run_cli()
        self.assertEqual(ctx.exception.code, 0, "用户停止不是崩溃，必须退出码 0")
        log_exc.assert_not_called()

    def test_crash_still_logged_as_unexpected_close(self):
        with (
            mock.patch.object(main_mod, "main", side_effect=RuntimeError("boom")),
            mock.patch.object(main_mod.logger, "log_exception") as log_exc,
            mock.patch.object(main_mod, "console"),
        ):
            with self.assertRaises(SystemExit) as ctx:
                main_mod.run_cli()
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("意外关闭", log_exc.call_args[0][0])

    def test_gui_stop_skips_console_ctrlc_menu(self):
        """GUI 会话里的「停止」= wait_if_paused 抛 KeyboardInterrupt：不许进 5 秒控制台菜单。

        独立复查：旧实现会调 _ctrlc_pause_menu()（msvcrt.kbhit 轮询满 5s），
        停止→退出要等 5 秒；控制台有残留按键时还可能被误判「c 继续」整套重启。
        """
        bridge = mock.Mock()
        bridge.exit_reason = "normal"
        main_mod._set_session_bridge(bridge)
        with (
            mock.patch.object(main_mod, "main", side_effect=KeyboardInterrupt()),
            mock.patch.object(main_mod, "_ctrlc_pause_menu") as menu,
            mock.patch.object(main_mod.logger, "log_exception") as log_exc,
            mock.patch.object(main_mod, "console"),
        ):
            main_mod.run_cli()
        menu.assert_not_called()
        log_exc.assert_not_called()
        self.assertEqual(bridge.exit_reason, "user_stop")

    def test_console_ctrlc_still_shows_menu(self):
        """控制台模式（没有桥）保持原行为：仍要进「回车退出 / c 继续」菜单。"""
        with (
            mock.patch.object(main_mod, "main", side_effect=KeyboardInterrupt()),
            mock.patch.object(main_mod, "_gui_bridge", return_value=None),
            mock.patch.object(main_mod, "_ctrlc_pause_menu", return_value=False) as menu,
            mock.patch.object(main_mod, "console"),
        ):
            main_mod.run_cli()
        menu.assert_called_once()

    def test_pinned_reason_survives_zero_system_exit(self):
        """S8 未找到窗口、用户选 q：退出码 0，但 reason 必须是钉住的 config_error。"""
        bridge = mock.Mock()
        bridge.exit_reason = "normal"
        main_mod._set_session_bridge(bridge)
        main_mod._pin_exit_reason("config_error", "未找到游戏窗口，用户选择退出（配置/环境问题）")
        with (
            mock.patch.object(main_mod, "main", side_effect=SystemExit(0)),
            mock.patch.object(main_mod, "console"),
        ):
            with self.assertRaises(SystemExit) as ctx:
                main_mod.run_cli()
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(bridge.exit_reason, "config_error")
        self.assertEqual(main_mod._classify_exit(SystemExit(0))[0], "normal", "钉住只生效一次")

    def test_nonzero_system_exit_is_config_error_not_normal(self):
        """聊天键不可用等 SystemExit(2) 路径:原样抛出,但 reason 必须是 config_error。"""
        bridge = mock.Mock()
        bridge.exit_reason = "normal"
        with (
            mock.patch.object(main_mod, "_gui_bridge", return_value=bridge),
            mock.patch.object(main_mod, "main", side_effect=SystemExit(2)),
        ):
            with self.assertRaises(SystemExit) as ctx:
                main_mod.run_cli()
        self.assertEqual(ctx.exception.code, 2, "SystemExit 必须原样抛给上层")
        self.assertEqual(bridge.exit_reason, "config_error")

    def test_zero_system_exit_stays_normal(self):
        bridge = mock.Mock()
        bridge.exit_reason = "crash"  # 上一轮残留:normal 必须覆盖它
        with (
            mock.patch.object(main_mod, "_gui_bridge", return_value=bridge),
            mock.patch.object(main_mod, "main", side_effect=SystemExit(0)),
        ):
            with self.assertRaises(SystemExit):
                main_mod.run_cli()
        self.assertEqual(bridge.exit_reason, "normal")

    def test_stamp_reaches_ipc_closed_payload(self):
        """run_cli 归因后,atexit 那条 ipc-closed 必须带 config_error,不是 normal。"""
        bridge = mock.Mock()
        bridge.exit_reason = "normal"
        server = mock.Mock()
        server.connected = True
        with (
            mock.patch.object(main_mod, "_gui_bridge", return_value=bridge),
            mock.patch.object(main_mod, "main", side_effect=FileNotFoundError("游戏日志不存在: X")),
            mock.patch.object(main_mod.logger, "log_exception"),
            mock.patch.object(main_mod, "console"),
        ):
            with self.assertRaises(SystemExit):
                main_mod.run_cli()
        self.assertEqual(bridge.exit_reason, "config_error")
        self.assertEqual(
            bridge.push_state.call_args.kwargs.get("exit_reason"),
            "config_error",
            "界面拿不到真实原因,只能显示固定的「核心已退出」",
        )
        main_mod._gui_notify_core_exit(server, bridge)
        sent = server.send_event.call_args
        self.assertEqual(sent[0][0], "ipc-closed")
        self.assertEqual(sent[0][1]["reason"], "config_error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
