# -*- coding: utf-8 -*-
"""
test_ui.py —— UI 层：确认词、工具栏图标、入口门禁、窗口冒烟
==========================================================
tkinter 在无桌面环境（部分 CI）不可用，因此：
  * 纯逻辑（确认词比对、图标常量、入口门禁）无条件测试
  * 需要 Tk 根窗口的用例加 require_tk / skipUnless，无显示时自动 skip

重点锁定的行为：
  1. 踢出确认词**逐字**匹配（区分大小写、空字符串一律拒绝）
  2. 大窗/小窗图标互斥切换；置顶图钉高亮
  3. --ui 在 ui.enabled=false 时**报错**，不得静默降级到控制台
  4. 首次运行未修改配置时禁止切页
"""
import functools
import json
import os
import tempfile
import unittest
from unittest import mock

from guild_auto.config import DEFAULT_CONFIG
from guild_auto.ui import confirm_dialog, toolbar


def _tk_available():
    try:
        import tkinter as tk

        root = tk.Tk()
        root.destroy()
        return True
    except Exception:
        return False


TK_OK = _tk_available()


def require_tk(fn):
    @functools.wraps(fn)
    def _wrapped(*a, **k):
        if not TK_OK:
            raise unittest.SkipTest("当前环境无可用 Tk 显示")
        return fn(*a, **k)

    return _wrapped


class TestConfirmText(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(confirm_dialog.check_confirm_text("kick-confirm", "kick-confirm"))

    def test_whitespace_trimmed(self):
        self.assertTrue(confirm_dialog.check_confirm_text("  kick-confirm  ", "kick-confirm"))

    def test_case_sensitive(self):
        self.assertFalse(confirm_dialog.check_confirm_text("KICK-CONFIRM", "kick-confirm"))

    def test_empty_input_rejected(self):
        self.assertFalse(confirm_dialog.check_confirm_text("", "kick-confirm"))

    def test_empty_expected_rejected(self):
        """配置把确认词清空时，绝不能因此"空输入即通过"。"""
        self.assertFalse(confirm_dialog.check_confirm_text("", ""))

    def test_none_expected_uses_default(self):
        self.assertTrue(confirm_dialog.check_confirm_text("kick-confirm", None))

    def test_chinese_confirm_text(self):
        self.assertTrue(confirm_dialog.check_confirm_text("确认踢出", "确认踢出"))


class TestToolbarIcons(unittest.TestCase):
    def test_arrow_icons_differ(self):
        self.assertNotEqual(toolbar.ARROW_FROM_BIG, toolbar.ARROW_FROM_SMALL)

    def test_pin_icons_differ(self):
        self.assertNotEqual(toolbar.PIN_OFF, toolbar.PIN_ON)


class TestUiEntryGate(unittest.TestCase):
    """--ui 与 ui.enabled 的交互（不依赖 Tk）。"""

    def _run_main_with(self, ui_enabled):
        from guild_auto import main as main_mod

        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg["ui"]["enabled"] = ui_enabled
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)

        with mock.patch.object(main_mod, "parse_args") as m_args, mock.patch.object(
            main_mod, "prepare_config", return_value=cfg
        ):
            m_args.return_value = mock.Mock(
                config=path,
                admin=None,
                log=None,
                options=None,
                dry_run=False,
                debug=False,
                debug_skip=None,
                ui=True,
            )
            return main_mod.main()

    def test_ui_disabled_raises_instead_of_silent_fallback(self):
        """ui.enabled=false 时必须报错 —— 禁止静默降级。"""
        with self.assertRaises(SystemExit) as cm:
            self._run_main_with(ui_enabled=False)
        self.assertIn("ui.enabled", str(cm.exception))

    @require_tk
    def test_ui_enabled_runs_ui(self):
        with mock.patch("guild_auto.ui.app.run_ui", return_value=0) as m_run:
            self.assertEqual(self._run_main_with(ui_enabled=True), 0)
        m_run.assert_called_once()


@unittest.skipUnless(TK_OK, "当前环境无可用 Tk 显示")
class TestAppSmoke(unittest.TestCase):
    """真实 Tk 冒烟：创建窗口、切模式、置顶状态。"""

    def setUp(self):
        from guild_auto.ui.app import App

        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "config.json")
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg["ui"]["enabled"] = True
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        self.app = App(self.path, first_run=True)

    def tearDown(self):
        try:
            self.app.destroy()
        except Exception:
            pass

    def test_window_geometry_toggles(self):
        self.app._dirty = True  # 首次运行需先改配置才允许切页
        self.app.toggle_mode()
        self.assertTrue(self.app.compact)
        self.assertEqual(self.app.toolbar.mode_btn.cget("text"), toolbar.ARROW_FROM_SMALL)
        self.app.toggle_mode()
        self.assertFalse(self.app.compact)
        self.assertEqual(self.app.toolbar.mode_btn.cget("text"), toolbar.ARROW_FROM_BIG)

    def test_first_run_blocks_page_switch_until_modified(self):
        with mock.patch("guild_auto.ui.app.messagebox.showwarning") as m_warn:
            self.app.toggle_mode()
        m_warn.assert_called_once()
        self.assertFalse(self.app.compact, "未修改配置时不应切到小窗")

    def test_hot_reload_reads_disk(self):
        """热重载：磁盘配置改动后，点重载应反映到 self.cfg。"""
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg["admin"] = "改后管理员"
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        self.assertTrue(self.app.hot_reload())
        self.assertEqual(self.app.cfg["admin"], "改后管理员")

    def test_kick_confirm_dialog_mode_uses_dialog(self):
        from unittest import mock

        self.app.cfg["ui"]["kick_confirm_mode"] = "dialog"
        with mock.patch("guild_auto.ui.app.ask_confirm_text", return_value=True) as m:
            self.assertTrue(self.app.confirm_kick("甲"))
        m.assert_called_once()
        self.assertIn("甲", m.call_args[0][2])

    def test_kick_confirm_inline_mode_uses_messagebox(self):
        from unittest import mock

        self.app.cfg["ui"]["kick_confirm_mode"] = "inline"
        with mock.patch("guild_auto.ui.app.messagebox.askyesno", return_value=False) as m:
            self.assertFalse(self.app.confirm_kick("乙"))
        m.assert_called_once()

    def test_topmost_toggle_updates_icon(self):
        self.app.toggle_on_top()
        self.assertTrue(self.app.always_on_top)
        self.assertEqual(self.app.toolbar.pin_btn.cget("text"), toolbar.PIN_ON)
        self.app.toggle_on_top()
        self.assertEqual(self.app.toolbar.pin_btn.cget("text"), toolbar.PIN_OFF)


class TestToolbarCallbacks(unittest.TestCase):
    """Toolbar 的回调与外观同步（不需要真实 Tk：用假 master）。"""

    @require_tk
    def test_callbacks_invoked(self):
        import tkinter as tk

        from guild_auto.ui.toolbar import Toolbar

        root = tk.Tk()
        try:
            calls = {"mode": 0, "top": 0}
            tb = Toolbar(root, lambda: calls.__setitem__("mode", calls["mode"] + 1),
                         lambda: calls.__setitem__("top", calls["top"] + 1))
            tb._toggle_mode()
            tb._toggle_top()
            self.assertEqual(calls, {"mode": 1, "top": 1})
            tb.set_compact(True)
            self.assertEqual(tb.mode_btn.cget("text"), toolbar.ARROW_FROM_SMALL)
            tb.set_on_top(True)
            self.assertEqual(tb.pin_btn.cget("text"), toolbar.PIN_ON)
        finally:
            root.destroy()

    @require_tk
    def test_no_callback_is_safe(self):
        """回调为 None 时点击不得抛异常（构造期尚未接线的场景）。"""
        import tkinter as tk

        from guild_auto.ui.toolbar import Toolbar

        root = tk.Tk()
        try:
            tb = Toolbar(root, None, None)
            tb._toggle_mode()
            tb._toggle_top()
        finally:
            root.destroy()


class TestConfirmDialog(unittest.TestCase):
    """确认弹窗：用假的 tkinter 组件驱动 _ok/_cancel 分支。"""

    def _drive(self, typed, expected):
        import sys
        import types

        from guild_auto.ui import confirm_dialog as cd

        captured = {}

        class FakeEntry:
            def __init__(self, master, **kw):
                pass

            def pack(self, **kw):
                pass

            def focus_set(self):
                pass

            def get(self):
                return typed

        class FakeButton:
            def __init__(self, master, text=None, command=None, **kw):
                captured[text] = command

            def pack(self, **kw):
                pass

        class FakeToplevel:
            def __init__(self, master, **kw):
                pass

            def title(self, *a):
                pass

            def transient(self, *a):
                pass

            def grab_set(self):
                pass

            def protocol(self, *a):
                pass

            def wait_window(self):
                # 模拟用户点了"确认执行"
                if "确认执行" in captured:
                    captured["确认执行"]()

            def destroy(self):
                pass

        fake_tk = types.SimpleNamespace(
            Toplevel=FakeToplevel, Entry=FakeEntry, Button=FakeButton,
            Label=lambda *a, **k: type("L", (), {"pack": lambda self, **kw: None})(),
            Frame=lambda *a, **k: type("F", (), {"pack": lambda self, **kw: None})(),
        )
        old = sys.modules.get("tkinter")
        sys.modules["tkinter"] = fake_tk
        try:
            # 重新加载模块使 tk 引用指向假实现
            import importlib

            importlib.reload(cd)
            return cd.ask_confirm_text(None, "t", "m", expected)
        finally:
            if old is not None:
                sys.modules["tkinter"] = old
            importlib.reload(cd)

    def test_ok_with_correct_text(self):
        self.assertTrue(self._drive("kick-confirm", "kick-confirm"))

    def test_wrong_text_returns_false(self):
        self.assertFalse(self._drive("nope", "kick-confirm"))

    def test_empty_returns_false(self):
        self.assertFalse(self._drive("", "kick-confirm"))


@unittest.skipUnless(TK_OK, "当前环境无可用 Tk 显示")
class TestConfigPanel(unittest.TestCase):
    """配置面板：收集、标红、保存成功/失败。"""

    def setUp(self):
        import tkinter as tk

        from guild_auto.config import DEFAULT_CONFIG
        from guild_auto.ui.config_panel import ConfigPanel

        self.root = tk.Tk()
        self.cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        self.saved = []
        self.panel = ConfigPanel(self.root, self.cfg, self.saved.append)

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def test_collect_contains_all_fields(self):
        edits = self.panel.collect()
        self.assertIn("admin", edits)
        self.assertIn("thresholds.high_demote", edits)
        self.assertGreater(len(edits), 50)

    def test_save_success_updates_cfg(self):
        self.panel.vars["admin"].set("面板管理员")
        self.assertTrue(self.panel.save())
        self.assertEqual(self.saved[-1]["admin"], "面板管理员")

    def test_save_failure_marks_red_and_warns(self):
        from unittest import mock

        self.panel.vars["thresholds.high_demote"].set("abc")
        with mock.patch("guild_auto.ui.config_panel.messagebox.showerror") as m_err:
            self.assertFalse(self.panel.save())
        m_err.assert_called_once()
        self.assertEqual(str(self.panel.widgets["thresholds.high_demote"].cget("background")),
                         "#ffd6d6")

    def test_mark_errors_resets_others(self):
        self.panel.mark_errors([{"path": "admin", "message": "x"}])
        self.panel.mark_errors([])
        self.assertEqual(str(self.panel.widgets["admin"].cget("background")), "white")


@unittest.skipUnless(TK_OK, "当前环境无可用 Tk 显示")
class TestAppMore(unittest.TestCase):
    """App 的保存/运行/置顶降级路径。"""

    def setUp(self):
        from guild_auto.config import DEFAULT_CONFIG
        from guild_auto.ui.app import App

        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "config.json")
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg["ui"]["enabled"] = True
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        self.ran = []
        self.app = App(self.path, first_run=False, on_run=self.ran.append)

    def tearDown(self):
        try:
            self.app.destroy()
        except Exception:
            pass

    def test_start_run_saves_then_runs(self):
        self.app.panel.vars["admin"].set("运行管理员")
        self.assertTrue(self.app.start_run())
        self.assertEqual(self.ran[-1]["admin"], "运行管理员")

    def test_save_writes_file(self):
        self.app.panel.vars["admin"].set("落盘管理员")
        self.assertTrue(self.app.save())
        with open(self.path, encoding="utf-8") as f:
            self.assertIn("落盘管理员", f.read())

    def test_topmost_falls_back_when_win32_missing(self):
        """没有 pywin32 时必须退回 Tk 的 -topmost，而不是抛异常。"""
        from unittest import mock

        with mock.patch.dict("sys.modules", {"win32gui": None, "win32con": None}):
            self.app.toggle_on_top()
        self.assertTrue(self.app.always_on_top)

    def test_compact_mode_hides_body(self):
        self.app._dirty = True
        self.app.toggle_mode()
        self.assertEqual(self.app.body.winfo_ismapped(), 0)

    def test_on_close_destroys(self):
        self.app._on_close()   # 不应抛异常


if __name__ == "__main__":
    unittest.main(verbosity=2)