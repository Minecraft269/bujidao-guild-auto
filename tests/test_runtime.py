# -*- coding: utf-8 -*-
"""
test_runtime.py —— Runtime 容器与 build_runtime
==============================================
runtime.py 是 main() 能被测试的前提,它自己此前只有 48% 覆盖。
本文件补齐四块:

  1. confirm / ask  —— **默认拒绝**语义:EOF 与 Ctrl+C 一律 False。
     这条是安全红线:终端异常时绝不能"默认同意"继续发破坏性命令。
  2. pause_gate     —— 暂停期间阻塞、恢复后重置窗口;未暂停时只做重置。
  3. 异步三件套      —— async_verify_submit 惰性建系统、send_fn 只发、
     verify_fn 按命令类型分流(/gc 要求文案回显,其它命令有块即成功)。
  4. build_runtime  —— 真实构造路径(全 mock 硬件),含聊天键为 enter 的警告
     与 OCR 不可用的降级分支。

零真实硬件:LogWatcher / HumanInput / GameStateDetector / find_game_window /
ActionExecutor 全部替换。
"""
import json
import unittest
from unittest import mock

from guild_auto.application import runtime as rt_mod
from guild_auto.config import DEFAULT_CONFIG


def _cfg(**overrides):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["log"] = {"enabled": False, "level": 4, "dir": "logs"}
    cfg.update(overrides)
    return cfg


class TestConfirmAndAsk(unittest.TestCase):
    def _rt(self, confirm_impl):
        return rt_mod.Runtime(cfg=_cfg(), ask_confirm=confirm_impl,
                              sleep=lambda s: None, stop_check=lambda: False)

    def test_confirm_true(self):
        self.assertTrue(self._rt(lambda p, d=False: True).confirm("?"))
        self.assertTrue(self._rt(lambda p, d=False: True).ask("?"))

    def test_confirm_false(self):
        self.assertFalse(self._rt(lambda p, d=False: False).confirm("?"))

    def test_eof_means_reject(self):
        """终端被关闭 → 一律拒绝。绝不能默认同意后继续发命令。"""
        def boom(p, d=False):
            raise EOFError
        self.assertFalse(self._rt(boom).confirm("?", default=True))
        self.assertFalse(self._rt(boom).ask("?", default=True))

    def test_keyboard_interrupt_means_reject(self):
        def boom(p, d=False):
            raise KeyboardInterrupt
        self.assertFalse(self._rt(boom).confirm("?", default=True))
        self.assertFalse(self._rt(boom).ask("?", default=True))

    def test_non_bool_return_is_coerced(self):
        self.assertTrue(self._rt(lambda p, d=False: 1).confirm("?"))
        self.assertFalse(self._rt(lambda p, d=False: "").confirm("?"))


class TestPauseGate(unittest.TestCase):
    def test_not_paused_just_resets(self):
        r = rt_mod.Runtime(cfg=_cfg(), sleep=lambda s: None,
                           stop_check=lambda: False)
        with mock.patch.object(r, "reset_window_state_after_pause") as m_reset:
            r.pause_gate()
        m_reset.assert_called_once()

    def test_paused_blocks_until_resumed(self):
        """暂停时循环等待,恢复后才重置窗口并返回。"""
        state = {"n": 0}

        def paused():
            state["n"] += 1
            return state["n"] <= 3        # 前 3 次为暂停,之后恢复

        sleeps = []
        r = rt_mod.Runtime(cfg=_cfg(), sleep=sleeps.append, stop_check=paused)
        with mock.patch.object(r, "reset_window_state_after_pause") as m_reset:
            r.pause_gate()
        self.assertGreaterEqual(len(sleeps), 1, "暂停期间必须真正等待")
        m_reset.assert_called_once()
        self.assertGreater(state["n"], 3)

    def test_reset_delegates_to_main_impl(self):
        r = rt_mod.Runtime(cfg=_cfg(), hwnd=42, detector="DET",
                           sleep=lambda s: None, stop_check=lambda: False)
        with mock.patch("guild_auto.main.reset_window_state_after_pause",
                        return_value=True) as m:
            self.assertTrue(r.reset_window_state_after_pause())
        m.assert_called_once_with(42, "DET")

    def test_reset_accepts_overrides(self):
        r = rt_mod.Runtime(cfg=_cfg(), hwnd=42, detector="DET",
                           sleep=lambda s: None, stop_check=lambda: False)
        with mock.patch("guild_auto.main.reset_window_state_after_pause",
                        return_value=False) as m:
            # detector=None 表示"用 self.detector",不是"传 None"
            r.reset_window_state_after_pause(hwnd=7, detector=None)
        m.assert_called_once_with(7, "DET")


class TestAsyncPieces(unittest.TestCase):
    def _rt(self, **kw):
        human = mock.Mock()
        watcher = mock.Mock()
        r = rt_mod.Runtime(cfg=_cfg(), human=human, watcher=watcher,
                           sleep=lambda s: None, stop_check=lambda: False, **kw)
        return r, human, watcher

    def test_send_fn_only_sends(self):
        r, human, watcher = self._rt()
        r._async_send_fn("/guild list", "甲", {})
        human.send_chat_text.assert_called_once()
        human.wait_between_commands.assert_called_once()
        watcher.wait_for_chat_block.assert_not_called()

    def test_verify_fn_timeout_is_failure(self):
        r, _, watcher = self._rt()
        watcher.wait_for_chat_block.return_value = None
        ok, err, data = r._async_verify_fn("/guild member 甲", "甲", {})
        self.assertFalse(ok)
        self.assertIn("超时", err)
        self.assertIsNone(data)

    def test_verify_fn_gc_requires_echo(self):
        """广播验证要求文案回显;没回显就是失败(否则会谎报通知成功)。"""
        r, _, watcher = self._rt()
        # 块里必须真的包含广播文案,才算"有回显"
        watcher.wait_for_chat_block.return_value = ["[CHAT] /gc 公告正文"]
        ok, err, _ = r._async_verify_fn("/gc 公告正文", "summary",
                                        {"msg": "/gc 公告正文"})
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_verify_fn_gc_missing_echo_fails(self):
        r, _, watcher = self._rt()
        watcher.wait_for_chat_block.return_value = ["别的内容"]
        ok, err, _ = r._async_verify_fn("/gc 公告正文", "summary",
                                        {"msg": "/gc 公告正文"})
        self.assertFalse(ok)
        self.assertIn("未回显", err)

    def test_verify_fn_non_gc_block_is_success(self):
        r, _, watcher = self._rt()
        watcher.wait_for_chat_block.return_value = ["面板"]
        ok, err, _ = r._async_verify_fn("/guild member 甲", "甲", {})
        self.assertTrue(ok)

    def test_async_verify_submit_creates_system_lazily_and_reuses(self):
        r, _, _ = self._rt()
        fake_sys = mock.Mock()
        fake_sys.submit.return_value = "task-1"
        with mock.patch("guild_auto.concurrency.async_system.AsyncVerifySystem",
                        return_value=fake_sys) as cls:
            self.assertEqual(r.async_verify_submit("/c", "甲"), "task-1")
            r.async_verify_submit("/c", "乙")
        cls.assert_called_once()
        fake_sys.start.assert_called_once()
        self.assertEqual(fake_sys.submit.call_count, 2)

    def test_close_is_idempotent_and_swallows_errors(self):
        r, _, watcher = self._rt()
        watcher.close.side_effect = RuntimeError("boom")
        r.close()
        r.close()          # 第二次不能抛
        self.assertGreaterEqual(watcher.close.call_count, 2)

    def test_close_stops_async_system_and_listener(self):
        r, _, _ = self._rt()
        r.async_system = mock.Mock()
        r.listener = mock.Mock()
        r.close()
        # AsyncVerifySystem 的停止方法叫 stop(timeout)，listener 是 stop()
        r.async_system.stop.assert_called_once_with(timeout=3)
        r.listener.stop.assert_called_once_with()


class TestDefaultConfirm(unittest.TestCase):
    def test_empty_input_uses_default(self):
        with mock.patch("builtins.input", return_value=""):
            self.assertTrue(rt_mod._default_confirm("?", default=True))
            self.assertFalse(rt_mod._default_confirm("?", default=False))

    def test_yes_variants(self):
        for ans in ("y", "YES", "是", "好", "确认"):
            with mock.patch("builtins.input", return_value=ans):
                self.assertTrue(rt_mod._default_confirm("?"), ans)

    def test_no(self):
        with mock.patch("builtins.input", return_value="n"):
            self.assertFalse(rt_mod._default_confirm("?"))

    def test_eof_returns_false(self):
        with mock.patch("builtins.input", side_effect=EOFError):
            self.assertFalse(rt_mod._default_confirm("?", default=True))


class TestBuildRuntime(unittest.TestCase):
    """真实构造路径(全 mock 硬件)。"""

    def _build(self, cfg=None, chat_key="t", detector_available=True,
               hwnd=123, state_enabled=True):
        cfg = cfg or _cfg()
        cfg["chat_key"] = chat_key
        cfg["state_check"]["enabled"] = state_enabled
        msgs = []
        with mock.patch.object(rt_mod, "LogWatcher") as m_w, \
                mock.patch.object(rt_mod, "HumanInput") as m_h, \
                mock.patch.object(rt_mod, "GameStateDetector") as m_d, \
                mock.patch.object(rt_mod, "find_game_window", return_value=hwnd), \
                mock.patch.object(rt_mod, "ActionExecutor") as m_e:
            m_w.return_value.encoding = "utf-8"
            m_h.return_value.chat_key = chat_key
            m_d.return_value.available = detector_available
            rt = rt_mod.build_runtime(cfg, console_fn=msgs.append)
        return rt, msgs, m_e

    def test_builds_and_wires_reset_hook(self):
        rt, msgs, m_exec = self._build()
        self.assertIsInstance(rt, rt_mod.Runtime)
        m_exec.return_value.set_reset_hook.assert_called_once()
        self.assertTrue(any("日志已连接" in m for m in msgs))

    def test_chat_key_enter_warns_but_proceeds(self):
        rt, msgs, _ = self._build(chat_key="enter")
        self.assertTrue(any("警告" in m for m in msgs))
        self.assertIsInstance(rt, rt_mod.Runtime)

    def test_ocr_unavailable_degrades(self):
        rt, msgs, _ = self._build(detector_available=False)
        self.assertTrue(any("界面状态检测不可用" in m for m in msgs))

    def test_window_not_found_warns(self):
        rt, msgs, _ = self._build(hwnd=None)
        self.assertTrue(any("未找到游戏窗口" in m for m in msgs))

    def test_state_check_disabled_skips_detector(self):
        rt, msgs, _ = self._build(state_enabled=False)
        self.assertIsNone(rt.detector)


if __name__ == "__main__":
    unittest.main(verbosity=2)