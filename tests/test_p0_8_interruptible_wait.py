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

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from guild_auto.application import runtime as runtime_mod
from guild_auto.infrastructure.log_watcher import LogWaitInterrupted, LogWatcher
from guild_auto import main as main_mod


def _missing_path():
    return os.path.join(tempfile.mkdtemp(prefix="p0_8_"), "latest.log")


class TestOpenIsInterruptible(unittest.TestCase):
    def setUp(self):
        self.addCleanup(runtime_mod.reset_state)

    def test_stop_interrupts_wait_within_one_second(self):
        path = _missing_path()
        threading.Timer(0.3, lambda: runtime_mod.set_force_stop(True)).start()
        t0 = time.monotonic()
        with self.assertRaises(LogWaitInterrupted):
            LogWatcher(path).open(wait_seconds=30)
        took = time.monotonic() - t0
        self.assertLess(took, 1.3, f"停止后 {took:.2f}s 才退出等待（要求 ≤1s 级）")
        self.assertGreaterEqual(took, 0.2, f"停止信号在 0.3s 才发出，却在 {took:.2f}s 退出")

    def test_pause_suspends_budget_and_is_not_stop(self):
        """暂停 1.5s 期间不消耗 1.0s 预算:总耗时 ≈ 2.5s,且结局是超时而非 user_stop。"""
        path = _missing_path()
        start = time.monotonic()
        paused_until = start + 1.5

        def paused():
            return time.monotonic() < paused_until

        t0 = time.monotonic()
        with self.assertRaises(FileNotFoundError) as ctx:
            LogWatcher(path).open(wait_seconds=1.0, stop_check=paused)
        took = time.monotonic() - t0
        self.assertIn("游戏日志不存在", str(ctx.exception))
        self.assertGreaterEqual(took, 2.4, f"{took:.2f}s 就超时了 —— 暂停消耗了预算")
        self.assertLess(took, 4.5, f"{took:.2f}s 远超 1.0s 预算 + 1.5s 暂停")

    def test_pause_alone_never_times_out(self):
        """一直暂停 = 一直等（不因预算耗尽退出），且不抛 user_stop。"""
        path = _missing_path()
        stop_at = time.monotonic() + 0.6

        def _stop_later():
            time.sleep(0.6)
            runtime_mod.set_force_stop(True)

        threading.Thread(target=_stop_later, daemon=True).start()
        t0 = time.monotonic()
        with self.assertRaises(LogWaitInterrupted):
            LogWatcher(path).open(wait_seconds=0.1, stop_check=lambda: time.monotonic() < stop_at)
        took = time.monotonic() - t0
        self.assertLess(took, 1.3, f"暂停中停止没生效，耗时 {took:.2f}s")

    def test_timeout_keeps_original_error(self):
        path = _missing_path()
        t0 = time.monotonic()
        with self.assertRaises(FileNotFoundError) as ctx:
            LogWatcher(path).open(wait_seconds=0.2)
        self.assertIn("game_log", str(ctx.exception))
        self.assertLess(time.monotonic() - t0, 2.0)


class TestExitReasonIsHonest(unittest.TestCase):
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
