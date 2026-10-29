# -*- coding: utf-8 -*-
"""
test_auto_pause.py —— UI 模式自动暂停(US-009)
=============================================
触发条件(缺一不可):operation_mode="ui" + pause.auto_on_user_input +
有进行中的阶段任务(auto_pause_guard) + 用户键鼠活动 + 非脚本模拟输入。

不触发:command 模式、确认等待/空闲、暂停键本身、脚本自己模拟的输入。
恢复:仅暂停键可解除;恢复后必须重置窗口并重新全扫。

零真实硬件:pynput 监听器全部 patch,回调直接调用。
"""

import copy
import unittest
from unittest import mock

from pynput import keyboard

from guild_auto.application import pause, runtime
from guild_auto.config import DEFAULT_CONFIG


def _key(char):
    return keyboard.KeyCode.from_char(char)


class _AutoPauseCase(unittest.TestCase):
    def setUp(self):
        runtime.reset_state()
        self.addCleanup(runtime.reset_state)
        self.lines = []
        patcher = mock.patch.object(
            pause, "console", lambda msg, **kwargs: self.lines.append(str(msg))
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _configure(self, mode="ui", enabled=True, grace_ms=300):
        runtime.configure_auto_pause(operation_mode=mode, enabled=enabled, grace_ms=grace_ms)

    def _execute(self):
        runtime.set_auto_pause_guard(True)

    def _idle(self):
        runtime.set_auto_pause_guard(False)

    def _auto_pause_messages(self):
        return [line for line in self.lines if "检测到用户键鼠活动" in line]


class TestTriggerConditions(_AutoPauseCase):
    def test_command_mode_never_triggers(self):
        self._configure(mode="command")
        self._execute()
        pause._on_press(_key("z"))
        pause._on_mouse_move(10, 10)
        pause._on_mouse_click(10, 10, None, True)
        self.assertFalse(runtime.is_paused())
        self.assertEqual(runtime.auto_pause_stats()["triggers"], 0)

    def test_ui_mode_during_execution_triggers_on_key(self):
        self._configure()
        self._execute()
        pause._on_press(_key("z"))
        self.assertTrue(runtime.is_paused(), "UI 模式执行中按任意键应自动暂停")
        self.assertEqual(runtime.auto_pause_stats()["triggers"], 1)
        self.assertTrue(runtime.consume_reset_flag(), "暂停后必须重置窗口")
        self.assertTrue(runtime.consume_rescan_flag(), "暂停后恢复必须重新全扫")
        self.assertTrue(self._auto_pause_messages(), f"未提示原因: {self.lines}")

    def test_ui_mode_triggers_on_mouse_move(self):
        self._configure(grace_ms=0)
        self._execute()
        pause._on_mouse_move(100, 200)
        self.assertTrue(runtime.is_paused())

    def test_ui_mode_triggers_on_mouse_click_press_only(self):
        self._configure(grace_ms=0)
        self._execute()
        pause._on_mouse_click(1, 2, None, False)
        self.assertFalse(runtime.is_paused(), "抬起事件不该触发")
        pause._on_mouse_click(1, 2, None, True)
        self.assertTrue(runtime.is_paused())

    def test_disabled_by_config(self):
        self._configure(enabled=False)
        self._execute()
        pause._on_press(_key("z"))
        self.assertFalse(runtime.is_paused())

    def test_idle_or_report_output_does_not_trigger(self):
        """没有进行中的阶段任务(等待确认/空闲/报告输出)不触发。"""
        self._configure(grace_ms=0)
        self._idle()
        pause._on_press(_key("z"))
        pause._on_mouse_move(1, 1)
        self.assertFalse(runtime.is_paused())

    def test_pause_key_itself_does_not_count_as_user_activity(self):
        self._configure()
        runtime.set_pause_key_cfg("u")
        self._execute()
        pause._on_press(_key("u"))  # 手动暂停
        self.assertTrue(runtime.is_paused())
        self.assertEqual(runtime.auto_pause_stats()["triggers"], 0, "暂停键不能算作自动暂停触发")
        self.assertEqual(self._auto_pause_messages(), [])


class TestSimulatedInput(_AutoPauseCase):
    def test_script_input_within_grace_window_is_ignored(self):
        self._configure(grace_ms=5000)
        self._execute()
        runtime.note_simulated_input()
        pause._on_mouse_move(1, 1)
        pause._on_mouse_click(1, 1, None, True)
        pause._on_press(_key("z"))
        self.assertFalse(runtime.is_paused(), "脚本自己模拟的输入不得触发自动暂停")
        self.assertEqual(runtime.auto_pause_stats()["triggers"], 0)

    def test_user_input_after_grace_window_triggers(self):
        self._configure(grace_ms=0)
        self._execute()
        runtime.note_simulated_input()  # 0ms 窗口 = 立即失效
        pause._on_mouse_move(1, 1)
        self.assertTrue(runtime.is_paused())


class TestResume(_AutoPauseCase):
    def _auto_pause(self):
        self._configure()
        runtime.set_pause_key_cfg("u")
        self._execute()
        pause._on_mouse_move(5, 5)
        self.assertTrue(runtime.is_paused())
        # 清掉触发时的标志,便于断言"恢复动作本身"置位的标志
        runtime.consume_reset_flag()
        runtime.consume_rescan_flag()

    def test_other_keys_cannot_resume(self):
        self._auto_pause()
        pause._on_press(_key("z"))
        pause._on_mouse_move(6, 6)
        self.assertTrue(runtime.is_paused(), "非暂停键不得解除自动暂停")

    def test_pause_key_resumes_and_requests_rescan(self):
        self._auto_pause()
        pause._on_press(_key("u"))
        self.assertFalse(runtime.is_paused())
        self.assertTrue(runtime.consume_reset_flag(), "恢复后必须重置窗口")
        self.assertTrue(runtime.consume_rescan_flag(), "恢复后必须重新全扫确认界面")
        self.assertEqual(runtime.auto_pause_stats()["resumes"], 1)

    def test_manual_pause_resume_in_ui_mode_also_rescans(self):
        """手动按暂停键也一样:UI 模式恢复后界面可能已变,必须重扫。"""
        self._configure()
        runtime.set_pause_key_cfg("u")
        pause._toggle_pause()
        pause._toggle_pause()
        self.assertTrue(runtime.consume_rescan_flag())

    def test_already_paused_does_not_double_count(self):
        self._auto_pause()
        pause._on_mouse_move(7, 7)
        self.assertEqual(runtime.auto_pause_stats()["triggers"], 1)


class TestListenerWiring(unittest.TestCase):
    def setUp(self):
        runtime.reset_state()
        self.addCleanup(runtime.reset_state)

    def _cfg(self, mode):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["operation_mode"] = mode
        cfg["pause"] = {"auto_on_user_input": True, "user_input_grace_ms": 300}
        return cfg

    def test_ui_mode_starts_mouse_listener(self):
        with (
            mock.patch.object(pause.keyboard, "Listener") as key_listener,
            mock.patch.object(pause.mouse, "Listener") as mouse_listener,
        ):
            listener = pause.start_pause_listener("u", cfg=self._cfg("ui"))
            key_listener.assert_called_once()
            mouse_listener.assert_called_once()
            self.assertTrue(runtime.auto_pause_enabled())
            listener.stop()
        mouse_listener.return_value.stop.assert_called_once()

    def test_command_mode_does_not_hook_mouse(self):
        with (
            mock.patch.object(pause.keyboard, "Listener"),
            mock.patch.object(pause.mouse, "Listener") as mouse_listener,
        ):
            pause.start_pause_listener("u", cfg=self._cfg("command"))
        mouse_listener.assert_not_called()
        self.assertFalse(runtime.auto_pause_enabled())

    def test_config_switch_disables_auto_pause(self):
        cfg = self._cfg("ui")
        cfg["pause"]["auto_on_user_input"] = False
        with (
            mock.patch.object(pause.keyboard, "Listener"),
            mock.patch.object(pause.mouse, "Listener") as mouse_listener,
        ):
            pause.start_pause_listener("u", cfg=cfg)
        mouse_listener.assert_not_called()
        self.assertFalse(runtime.auto_pause_enabled())


if __name__ == "__main__":
    unittest.main(verbosity=2)
