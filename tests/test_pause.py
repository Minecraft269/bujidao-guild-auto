# -*- coding: utf-8 -*-
"""
test_pause.py —— 暂停门与 Ctrl+C 中断菜单
========================================
本文件锁定的不是"代码跑过",而是三条**静默失效**的高危行为:

  1. 状态唯一所有者。拆分后 main.py 曾 `from runtime import _paused` 再
     `global _paused` 赋值 —— 写的是 main 的副本,runtime 那份永远是 False,
     于是暂停门根本拦不住命令、且没有任何报错。本文件用身份比较 +
     真实 _toggle_pause 调用把这条钉死。
  2. Ctrl+C 必须"置标志 + 抛 KeyboardInterrupt",不能只 return。
     只 return 会让上层 while 把中断当普通 interrupted 分支 continue,
     实测在 latest.log 里死循环重发 ~30 次 /guild list。
  3. 恢复后必须置 _reset_needed(执行阶段据此重置窗口),否则恢复后
     直接发命令时窗口状态未知 → 命令可能发错目标。

零真实硬件:pyautogui / activate_window / prepare_chat_state / console
全部 patch 掉。
"""

import os
import sys
import threading
import time
import unittest
from unittest import mock


from guild_auto.infrastructure import logger  # noqa: E402
from guild_auto.application import pause  # noqa: E402
from guild_auto.application import runtime  # noqa: E402

for _n in (
    "log_debug",
    "log_info",
    "log_warning",
    "log_error",
    "log_critical",
    "log_trace",
    "log_exception",
    "trace_code_location",
):
    if not hasattr(logger, _n):
        setattr(logger, _n, lambda *a, **k: None)


class _PauseCase(unittest.TestCase):
    """每个用例前后都把 runtime 的暂停状态复位,避免用例间互相污染。"""

    def setUp(self):
        runtime.reset_state()
        self.addCleanup(runtime.reset_state)
        self.console_lines = []
        for p in (
            mock.patch.object(pause, "console", lambda m, **k: self.console_lines.append(str(m))),
            mock.patch.object(pause.pyautogui, "press", lambda *a, **k: None),
            mock.patch.object(pause, "activate_window", lambda *a, **k: None),
            mock.patch.object(pause, "prepare_chat_state", lambda *a, **k: None),
        ):
            p.start()
            self.addCleanup(p.stop)


class TestStateHasSingleOwner(_PauseCase):
    """暂停状态只能有一份。任何"值副本"写法都会让暂停门静默失效。"""

    def test_no_module_level_copy_of_state_in_pause(self):
        """pause.py 不该自己定义 _paused / _force_stop / _reset_needed。"""
        for name in ("_paused", "_force_stop", "_reset_needed"):
            self.assertFalse(
                hasattr(pause, name),
                f"pause.py 里出现了 {name} 模块级定义 —— "
                f"它就是导致暂停门失效的值副本形态,状态必须只由 runtime 持有",
            )

    def test_set_paused_is_visible_through_runtime_reader(self):
        """经 set_paused 写入后,runtime.is_paused() 必须立即可见。"""
        runtime.set_paused(True)
        self.assertTrue(runtime.is_paused())
        runtime.set_paused(False)
        self.assertFalse(runtime.is_paused())

    def test_set_force_stop_is_visible_through_runtime_reader(self):
        runtime.set_force_stop(True)
        self.assertTrue(runtime.is_force_stop())
        runtime.set_force_stop(False)
        self.assertFalse(runtime.is_force_stop())

    def test_toggle_pause_writes_through_to_owner(self):
        """真实 _toggle_pause 走完一轮,runtime 侧必须看到状态翻转。"""
        runtime.set_paused(False)
        pause._toggle_pause()
        self.assertTrue(runtime.is_paused(), "_toggle_pause 没写进唯一所有者")
        pause._toggle_pause()
        self.assertFalse(runtime.is_paused(), "_toggle_pause 没能恢复")


class TestTogglePauseSideEffects(_PauseCase):
    """_toggle_pause 除了翻状态,还要正确维护事件与重置标志。"""

    def test_pause_sets_event_and_resume_clears_it(self):
        runtime.set_paused(False)
        runtime._pause_event.clear()
        runtime.consume_reset_flag()  # 清残留

        pause._toggle_pause()  # → 暂停
        self.assertTrue(
            runtime._pause_event.is_set(), "暂停必须置位 _pause_event,否则阻塞中的 wait 收不到通知"
        )
        self.assertFalse(runtime.consume_reset_flag(), "暂停动作本身不需要重置窗口")

        pause._toggle_pause()  # → 恢复
        self.assertFalse(
            runtime._pause_event.is_set(), "恢复必须清掉 _pause_event,否则下次 wait 直接穿透"
        )
        self.assertTrue(
            runtime.consume_reset_flag(),
            "恢复后必须置 _reset_needed —— 执行阶段据此重置窗口,否则恢复后直接发命令时窗口状态未知",
        )

    def test_pause_message_includes_current_pause_key(self):
        """提示语要带当前暂停键名,否则用户不知道按哪个键恢复。"""
        runtime.set_pause_key_cfg("u")
        pause._toggle_pause()
        self.assertTrue(
            any("暂停" in m for m in self.console_lines), f"未打印暂停提示: {self.console_lines}"
        )
        self.assertTrue(
            any("u" in m for m in self.console_lines), f"提示语未带暂停键名: {self.console_lines}"
        )


class TestWaitIfPaused(_PauseCase):
    """暂停门:暂停时阻塞,恢复时放行,收到 Ctrl+C 时抛 KeyboardInterrupt。"""

    def test_not_paused_returns_immediately(self):
        runtime.reset_state()
        t0 = time.time()
        pause.wait_if_paused()
        self.assertLess(time.time() - t0, 0.5, "未暂停时不该有任何等待")

    def test_force_stop_raises_keyboard_interrupt(self):
        """核心:必须抛异常而非 return。

        只 return 的话,query_guild_list 的 while 会把中断当普通 interrupted
        分支 continue —— 那条路径实测死循环重发过 ~30 次 /guild list。
        """
        runtime.set_force_stop(True)
        with self.assertRaises(KeyboardInterrupt):
            pause.wait_if_paused()

    def test_force_stop_takes_priority_over_paused(self):
        """同时暂停 + Ctrl+C:必须先抛异常,不能被"暂停等待"卡住。"""
        runtime.set_paused(True)
        runtime.set_force_stop(True)
        with self.assertRaises(KeyboardInterrupt):
            pause.wait_if_paused()

    def test_blocks_until_resumed(self):
        """暂停中阻塞,后台线程恢复后必须放行(且确实等了)。"""
        runtime.set_paused(True)
        runtime._pause_event.set()

        def _resume():
            time.sleep(0.15)
            runtime.set_paused(False)
            runtime._pause_event.clear()

        t = threading.Thread(target=_resume, daemon=True)
        t.start()
        t0 = time.time()
        pause.wait_if_paused()
        elapsed = time.time() - t0
        self.assertGreaterEqual(elapsed, 0.1, f"没等就放行了: {elapsed:.3f}s")
        self.assertLess(elapsed, 3.0, f"等待过久: {elapsed:.3f}s")


class TestResetWindowStateAfterPause(_PauseCase):
    """恢复后重置:只在有残留标志时动作,且动作内容完整。"""

    def test_no_flag_no_action(self):
        """没有 _reset_needed 时不许动窗口 —— 否则每次发送前乱按 Esc。"""
        runtime.reset_state()
        with mock.patch.object(pause.pyautogui, "press") as press:
            self.assertFalse(pause.reset_window_state_after_pause(123, None))
            press.assert_not_called()

    def test_with_flag_activates_and_escapes(self):
        runtime.set_reset_needed(True)
        with (
            mock.patch.object(pause, "activate_window") as act,
            mock.patch.object(pause.pyautogui, "press") as press,
        ):
            self.assertTrue(pause.reset_window_state_after_pause(999, None))
            act.assert_called_once_with(999)
            press.assert_called_once_with("esc")

    def test_with_flag_runs_ocr_when_detector_present(self):
        runtime.set_reset_needed(True)
        detector = object()
        with mock.patch.object(pause, "prepare_chat_state") as prep:
            pause.reset_window_state_after_pause(None, detector)
            prep.assert_called_once()
            self.assertIs(prep.call_args[0][0], detector)

    def test_reset_failure_does_not_break_the_batch(self):
        """重置是尽力而为:按 Esc / OCR 失败不能让整个执行批次崩掉。

        恢复后紧接着要发下一条命令,异常冒泡会直接终结批次 ——
        这正是"宁可记日志不抛"的理由。
        """
        runtime.set_reset_needed(True)
        with mock.patch.object(
            pause.pyautogui, "press", side_effect=RuntimeError("pyautogui 挂了")
        ):
            self.assertTrue(pause.reset_window_state_after_pause(1, None))

    def test_works_without_hwnd_or_detector(self):
        """无窗口句柄、无 OCR 检测器时也必须完成重置(两者都是可选依赖)。"""
        runtime.set_reset_needed(True)
        with (
            mock.patch.object(pause, "activate_window") as act,
            mock.patch.object(pause, "prepare_chat_state") as prep,
        ):
            self.assertTrue(pause.reset_window_state_after_pause(None, None))
            act.assert_not_called()
            prep.assert_not_called()

    def test_flag_is_consumed_only_once(self):
        """标志必须被消费 —— 重复消费会让后续每次发送都白按一次 Esc。"""
        runtime.set_reset_needed(True)
        self.assertTrue(pause.reset_window_state_after_pause(None, None))
        self.assertFalse(
            pause.reset_window_state_after_pause(None, None), "第二次调用不该再动作(标志已被消费)"
        )

    def test_consume_reset_flag_helper_matches_runtime(self):
        """_consume_reset_flag 必须是 runtime 的同一行为(供 executor 注入)。"""
        runtime.set_reset_needed(True)
        self.assertTrue(pause._consume_reset_flag())
        self.assertFalse(pause._consume_reset_flag())


class TestStartPauseListener(_PauseCase):
    """监听启动:按配置解析暂停键,并真的把 listener 跑起来。"""

    def test_single_char_key(self):
        with mock.patch.object(pause.keyboard, "Listener") as L:
            inst = L.return_value
            pause.start_pause_listener("u")
            L.assert_called_once()
            self.assertTrue(inst.daemon, "监听线程必须 daemon,否则主流程退出时挂住")
            inst.start.assert_called_once()
        self.assertEqual(runtime.pause_key_name(), "u")

    def test_named_key_resolves_to_pynput_key(self):
        from pynput import keyboard as kb

        with mock.patch.object(pause.keyboard, "Listener"):
            pause.start_pause_listener("esc")
        self.assertEqual(runtime.pause_key(), kb.Key.esc)

    def test_unknown_key_name_falls_back_to_u(self):
        """配置写了个不认识的名字时不能崩,回退到 u(与拆分前行为一致)。"""
        from pynput import keyboard as kb

        runtime.set_pause_key_cfg("not_a_real_key")
        self.assertEqual(runtime.pause_key_name(), "not_a_real_key")
        self.assertEqual(runtime.pause_key(), kb.KeyCode.from_char("u"))


class TestOnPress(_PauseCase):
    """pynput 回调:暂停键翻状态;Ctrl+C 走 os.kill;其它键只记日志。"""

    def test_pause_key_toggles(self):
        from pynput import keyboard as kb

        runtime.set_pause_key_cfg("u")
        runtime.set_paused(False)
        pause._on_press(kb.KeyCode.from_char("u"))
        self.assertTrue(runtime.is_paused())
        pause._on_press(kb.KeyCode.from_char("u"))
        self.assertFalse(runtime.is_paused())

    def test_other_key_does_not_touch_state(self):
        from pynput import keyboard as kb

        runtime.set_pause_key_cfg("u")
        runtime.reset_state()
        pause._on_press(kb.KeyCode.from_char("z"))
        self.assertFalse(runtime.is_paused())
        self.assertFalse(runtime.is_force_stop())

    def test_ctrl_c_sends_ctrl_c_event(self):
        """Ctrl+C 必须主动 os.kill(CTRL_C_EVENT) 转 SIGINT 进菜单。"""
        import signal as _sig
        from pynput import keyboard as kb

        with mock.patch("os.kill") as kill:
            pause._on_press(kb.KeyCode.from_char(chr(3)))
        self.assertGreaterEqual(kill.call_count, 1, "未主动发 CTRL_C_EVENT")
        self.assertEqual(kill.call_args[0][1], _sig.CTRL_C_EVENT)

    def test_ctrl_c_falls_back_when_kill_fails(self):
        """os.kill 不可用时必须回退置 _force_stop + 置位事件,不能静默失效。"""
        from pynput import keyboard as kb

        runtime.reset_state()
        with mock.patch("os.kill", side_effect=OSError("no console")):
            pause._on_press(kb.KeyCode.from_char(chr(3)))
        self.assertTrue(runtime.is_force_stop(), "回退路径未置 _force_stop")
        self.assertTrue(runtime.is_paused(), "回退路径未置暂停")
        self.assertTrue(runtime._pause_event.is_set(), "回退路径未唤醒阻塞调用")

    def test_exception_inside_callback_is_swallowed_and_logged(self):
        """回调里出异常不能把 pynput 监听线程带崩(原实现会静默挂掉)。"""

        class Boom:
            @property
            def char(self):
                raise RuntimeError("boom")

        pause._on_press(Boom())  # 不抛异常即通过


class TestSignalHandler(_PauseCase):
    """signal handler:置暂停 + 置 force_stop + 抛 KeyboardInterrupt。"""

    def test_sets_state_and_raises(self):
        runtime.reset_state()
        with self.assertRaises(KeyboardInterrupt):
            pause._signal_handler(None, None)
        self.assertTrue(runtime.is_paused(), "Ctrl+C 必须置暂停,后台链路立即阻断")
        self.assertTrue(runtime.is_force_stop(), "Ctrl+C 必须置 force_stop")
        self.assertTrue(runtime.consume_reset_flag(), "Ctrl+C 后恢复需重置界面,标志必须置位")

    def test_install_registers_both_signals(self):
        import signal as _sig

        prev = pause.install_signal_handlers()
        try:
            self.assertIs(_sig.getsignal(_sig.SIGINT), pause._signal_handler)
            if hasattr(_sig, "SIGTERM"):
                self.assertIs(_sig.getsignal(_sig.SIGTERM), pause._signal_handler)
        finally:
            # 还原,避免影响同进程其它用例
            for name, handler in prev.items():
                signum = getattr(_sig, name, None)
                if signum is not None and handler is not None:
                    _sig.signal(signum, handler)


class TestCtrlCMenuInput(_PauseCase):
    """Ctrl+C 菜单:msvcrt 非阻塞轮询决定"退出"还是"继续"。"""

    def _run_menu(self, keys):
        """用假的 msvcrt 喂按键序列,并让时钟在无键可读时立即越过 deadline。"""
        seq = list(keys)

        class FakeMsvcrt:
            @staticmethod
            def kbhit():
                return bool(seq)

            @staticmethod
            def getwch():
                return seq.pop(0)

        clock = {"t": 0.0}

        def fake_time():
            return clock["t"]

        def fake_sleep(s):
            clock["t"] += s
            if not seq:
                clock["t"] += 10.0  # 没键可读了 → 直接跨过 5s deadline

        with (
            mock.patch.dict(sys.modules, {"msvcrt": FakeMsvcrt}),
            mock.patch.object(pause.time, "time", fake_time),
            mock.patch.object(pause.time, "sleep", fake_sleep),
        ):
            return pause._ctrlc_pause_menu()

    def test_enter_means_exit(self):
        self.assertFalse(self._run_menu(["\r"]))

    def test_c_then_enter_means_continue(self):
        self.assertTrue(self._run_menu(["c", "\r"]))

    def test_uppercase_C_also_continues(self):
        self.assertTrue(self._run_menu(["C", "\r"]))

    def test_backspace_removes_last_char(self):
        """打了 c 又退格 → 缓冲为空 → 视为"未输入" → 退出。"""
        self.assertFalse(self._run_menu(["c", "\x08", "\r"]))

    def test_ctrl_c_inside_menu_exits_immediately(self):
        self.assertFalse(self._run_menu(["c", "\x03", "\r"]))

    def test_other_input_is_exit(self):
        self.assertFalse(self._run_menu(["x", "\r"]))

    def test_signal_dfl_switch_failure_is_tolerated(self):
        """某些环境不允许改 SIGINT handler —— 那时菜单仍必须能读按键。

        `signal.signal` 抛异常时 _prev_handler 留 None,finally 里就不还原,
        不能因此让整个菜单失效。
        """
        import signal as _sig

        with mock.patch.object(_sig, "signal", side_effect=ValueError("not allowed")):
            self.assertFalse(self._run_menu([chr(13)]))

    def test_timeout_without_input_means_exit(self):
        """5s 内没按键 → 默认退出(不阻塞用户)。"""
        self.assertFalse(self._run_menu([]))

    def test_msvcrt_missing_falls_back(self):
        """非 Windows 无 msvcrt → 回退 thread+Queue 实现。"""
        with (
            mock.patch.dict(sys.modules, {"msvcrt": None}),
            mock.patch.object(pause, "_ctrlc_pause_menu_fallback", return_value=True) as fb,
        ):
            self.assertTrue(pause._ctrlc_pause_menu())
            fb.assert_called_once()


class TestCtrlCMenuFallback(_PauseCase):
    """回退实现:thread+Queue 读 stdin。"""

    def test_reads_c_and_continues(self):
        with mock.patch("builtins.input", return_value="c"):
            self.assertTrue(pause._ctrlc_pause_menu_fallback())

    def test_reads_empty_and_exits(self):
        with mock.patch("builtins.input", return_value=""):
            self.assertFalse(pause._ctrlc_pause_menu_fallback())

    def test_input_raising_still_returns(self):
        """input 抛 KeyboardInterrupt/EOFError 时也必须返回(不能卡死)。

        原实现捕获后不塞值,q.empty() 永远 True → 主线程走超时分支,
        表现为"按了键却像没反应"。
        """
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
            self.assertFalse(pause._ctrlc_pause_menu_fallback())


class TestResetState(_PauseCase):
    """runtime.reset_state:让 Ctrl+C 菜单选"继续"后能干净重启。"""

    def test_clears_everything(self):
        runtime.set_paused(True)
        runtime.set_force_stop(True)
        runtime.set_reset_needed(True)
        runtime._pause_event.set()

        runtime.reset_state()

        self.assertFalse(
            runtime.is_paused(), "重启后必须不是暂停态,否则第一个 wait_if_paused 就卡住"
        )
        self.assertFalse(runtime.is_force_stop())
        self.assertFalse(runtime._pause_event.is_set(), "事件残留会让阻塞调用直接穿透")


if __name__ == "__main__":
    unittest.main(verbosity=2)
