# -*- coding: utf-8 -*-
"""P0-9 启动期配置预检 + 看门狗阈值与长等待冲突 —— 回归测试

实机同一份日志里两件事：
  1. `game_log` 是模板占位符，`validate_config` 却返回 errors=0 → 放行到 S8 干等 60 秒
  2. 等待期间看门狗固定 30s 阈值 < 60s 等待 → 误报「启动卡在 S7」并转储线程栈

这里锁住三件事：
  * schema 三态：占位符 / 父目录不存在 / 文件暂缺但父目录存在（后两者口径必须不同）
    —— 三态断言在 tests/test_config_validate.py::TestValidateGameLogSafety
  * `log_watcher.wait_state()` 在等待期间上报、结束后清除
  * 看门狗在「阶段声明的预期等待」期间不转储、不报 Error；等待结束后按正常阈值
"""

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from guild_auto import main as main_mod
from guild_auto.infrastructure import log_watcher as lw
from guild_auto.infrastructure.log_watcher import LogWatcher


def _missing_log():
    """父目录必然存在、文件必然不存在的路径(不与别的用例抢同一个名字)。"""
    return os.path.join(tempfile.gettempdir(), f"p0_9_missing_{os.getpid()}_{id(object())}.log")


class TestWaitState(unittest.TestCase):
    def test_none_when_not_waiting(self):
        self.assertIsNone(lw.wait_state())

    def test_reports_during_wait_and_clears_after(self):
        with self.assertRaises(FileNotFoundError):
            LogWatcher(_missing_log()).open(wait_seconds=0.3)
        self.assertIsNone(lw.wait_state(), "等待结束后 wait_state 必须清空")

    def test_visible_while_waiting(self):
        seen = {}

        def _probe():
            time.sleep(0.1)
            seen["state"] = lw.wait_state()

        t = threading.Thread(target=_probe)
        t.start()
        try:
            with self.assertRaises(FileNotFoundError):
                LogWatcher(_missing_log()).open(wait_seconds=0.4)
        finally:
            t.join(5)
        st = seen.get("state")
        self.assertIsNotNone(st, "等待期间没有上报 wait_state —— 看门狗仍只能靠固定阈值猜")
        self.assertGreater(st["expected_s"], 0)
        self.assertGreaterEqual(st["elapsed_s"], 0)


class TestWatchdogDeclaredWait(unittest.TestCase):
    """看门狗:阶段声明了预期等待时,不得判卡住、不得转储线程栈、不得报 Error。"""

    @staticmethod
    def _run_ticks(wd, seconds=0.4):
        wd.start()
        time.sleep(seconds)
        wd.stop()

    def test_declared_wait_suppresses_stall_and_error(self):
        wait = {"path": "C:/x/latest.log", "expected_s": 60.0, "elapsed_s": 31.0}
        with (
            mock.patch.object(lw, "wait_state", return_value=wait),
            mock.patch.object(main_mod.logger, "log_warning") as warn,
            mock.patch.object(main_mod.logger, "log_error") as err,
            mock.patch.object(main_mod.logger, "log_info"),
            mock.patch.object(main_mod._StartupWatchdog, "_dump_thread_stack") as dump,
        ):
            wd = main_mod._StartupWatchdog(
                "x.log", interval=0.05, stall_seconds=0.05, start_seconds=0.05
            )
            self._run_ticks(wd)
        self.assertFalse(dump.called, "声明等待期间仍转储了线程栈 —— 实机 42 行 Trace 就是这么来的")
        self.assertFalse(err.called, "声明等待期间报了 Error")
        self.assertFalse(
            [c for c in warn.call_args_list if "无进展" in str(c)],
            f"声明等待期间仍报「无进展」: {warn.call_args_list}",
        )

    def test_wait_elapsed_is_deducted_from_start_budget(self):
        """等待时长必须从「启动 30s 未进 S9」的总预算里扣掉，否则等待一结束就误报。"""
        wd = main_mod._StartupWatchdog(
            "x.log", interval=0.05, stall_seconds=10.0, start_seconds=0.05
        )
        wd._in_declared_wait = True
        wd._last_wait_elapsed = 47.0  # 刚结束的是一段 47s 的声明等待
        with (
            mock.patch.object(lw, "wait_state", return_value=None),
            mock.patch.object(main_mod.logger, "log_error") as err,
            mock.patch.object(main_mod.logger, "log_warning"),
        ):
            self._run_ticks(wd, 0.25)
        self.assertAlmostEqual(wd._waited_total, 47.0, places=3, msg="等待时长没计入抵扣")
        self.assertFalse(err.called, "等待时长没从启动预算里扣掉 —— 等待刚结束就报「启动卡在…」")

    def test_paused_wait_past_declared_window_never_falls_back(self):
        """暂停会让墙钟 elapsed 越过「声明值+余量」—— 仍不得落回卡住判定(复审)。

        P0-8 的暂停不消耗 open() 的预算,但 wait_state 的 elapsed 是墙钟;
        拿 elapsed 越线就回退,会把「已持续 Ns 无进展」+ 线程栈转储原样搬回来。
        """
        wait = {"path": "C:/x/latest.log", "expected_s": 60.0, "elapsed_s": 600.0}
        with (
            mock.patch.object(lw, "wait_state", return_value=wait),
            mock.patch.object(main_mod._StartupWatchdog, "_dump_thread_stack") as dump,
            mock.patch.object(main_mod.logger, "log_error") as err,
            mock.patch.object(main_mod.logger, "log_warning") as warn,
            mock.patch.object(main_mod.logger, "log_info"),
        ):
            wd = main_mod._StartupWatchdog(
                "x.log", interval=0.05, stall_seconds=0.05, start_seconds=0.05
            )
            self._run_ticks(wd, 0.4)
        self.assertFalse(dump.called, "暂停中的等待仍被判卡住并转储了线程栈")
        self.assertFalse(err.called, "暂停中的等待仍报了 Error")
        self.assertFalse([c for c in warn.call_args_list if "无进展" in str(c)])

    def test_wait_end_clears_waiting_text(self):
        """等待结束后要把状态栏那句「等待游戏日志…」换回真实阶段(复审)。"""
        bridge = mock.Mock()
        bridge._state = {"stage": "等待游戏日志: C:/x/latest.log（最长 60s…）"}
        with (
            mock.patch("guild_auto.ui.binding.active", return_value=bridge),
            mock.patch.object(lw, "wait_state", return_value=None),
            mock.patch.object(main_mod.logger, "log_error"),
            mock.patch.object(main_mod.logger, "log_warning"),
            mock.patch.object(main_mod.logger, "log_debug"),
        ):
            wd = main_mod._StartupWatchdog(
                "x.log", interval=0.05, stall_seconds=10, start_seconds=10
            )
            wd._in_declared_wait = True
            wd._since = time.monotonic()
            self._run_ticks(wd, 0.12)
        bridge.push_state.assert_called_with(stage=wd._stage)

    def test_report_waiting_does_not_overwrite_stopped(self):
        """用户刚点「停止」时,等待文案不得盖掉「已停止」。"""
        bridge = mock.Mock()
        bridge._state = {"stage": "已停止"}
        with mock.patch("guild_auto.ui.binding.active", return_value=bridge):
            wd = main_mod._StartupWatchdog("x.log")
            wd._report_waiting({"path": "C:/x/latest.log", "expected_s": 60.0, "elapsed_s": 1.0})
        bridge.push_state.assert_not_called()

    def test_control_without_declared_wait_still_reports(self):
        """对照:没有声明等待时,看门狗必须照旧报卡住(证明上面的断言不是假绿)。"""
        with (
            mock.patch.object(lw, "wait_state", return_value=None),
            mock.patch.object(main_mod._StartupWatchdog, "_dump_thread_stack") as dump,
            mock.patch.object(main_mod.logger, "log_error"),
            mock.patch.object(main_mod.logger, "log_warning"),
        ):
            wd = main_mod._StartupWatchdog(
                "x.log", interval=0.05, stall_seconds=0.05, start_seconds=10
            )
            self._run_ticks(wd, 0.3)
        self.assertTrue(dump.called, "没有声明等待时也不报卡住 —— 看门狗被改哑了")

    def test_waiting_text_pushed_once_to_gui(self):
        bridge = mock.Mock()
        with (
            mock.patch("guild_auto.ui.binding.active", return_value=bridge),
            mock.patch.object(main_mod.logger, "log_info") as info,
        ):
            wd = main_mod._StartupWatchdog("x.log")
            wait = {"path": "C:/x/latest.log", "expected_s": 60.0, "elapsed_s": 1.0}
            wd._report_waiting(wait)
            wd._report_waiting(wait)  # 同一段等待只推一次
        self.assertEqual(bridge.push_state.call_count, 1)
        text = bridge.push_state.call_args.kwargs["stage"]
        self.assertIn("等待游戏日志", text)
        self.assertIn("60", text)
        self.assertIn("停止", text)
        # 验收口径:等待期间 grep '看门狗' 必须为空 —— 提示行不能自带这个前缀
        logged = " ".join(str(c.args[0]) for c in info.call_args_list)
        self.assertNotIn("看门狗", logged, f"等待提示污染了「看门狗」grep: {logged}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
