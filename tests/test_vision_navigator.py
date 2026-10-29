# -*- coding: utf-8 -*-
"""
test_vision_navigator.py —— 按键试探与导航
==========================================
验收(docs/UI识别模式详解.md §八 / §十二):
  * 首选键来自 options.txt 解析,失败按候选键换键,成功后缓存;
  * 每次按键/导航后重新全扫确认,禁止假设生效;
  * 候选耗尽 / 未提供公会入口动作 → 抛错停下,不盲点;
  * 连续误按超阈值 → Esc 回退并重新全扫。
"""

import unittest

from guild_auto.vision.navigator import NavigationError, Navigator

SETTINGS_TITLE = "装扮与个人设定"
GUILD_TITLE = "公会系统"


class FakeUI:
    """假界面:press 记录按键,scan_title 按脚本依次返回标题。"""

    def __init__(self, titles):
        self.titles = list(titles)
        self.pressed = []
        self.escaped = 0
        self.opened = 0

    def press(self, key):
        self.pressed.append(key)

    def scan_title(self):
        return self.titles.pop(0) if self.titles else "某个未知界面"

    def sleep(self, _seconds):
        return None

    def escape(self):
        self.escaped += 1

    def open_guild(self):
        self.opened += 1


def make_navigator(ui, first_key="first-key", candidates=(), **kwargs):
    return Navigator(
        first_key=first_key,
        candidates=candidates,
        press=ui.press,
        scan_title=ui.scan_title,
        sleep=ui.sleep,
        escape=ui.escape,
        **kwargs,
    )


class TestKeyProbe(unittest.TestCase):
    def test_first_key_success_with_guild_entry(self):
        ui = FakeUI([SETTINGS_TITLE, GUILD_TITLE])
        nav = make_navigator(ui)
        key = nav.enter_guild_ui(ui.open_guild)
        self.assertEqual(key, "first-key")
        self.assertEqual(ui.pressed, ["first-key"])
        self.assertEqual(ui.opened, 1)
        self.assertEqual(nav.cached_key, "first-key")

    def test_first_key_already_on_guild(self):
        ui = FakeUI([GUILD_TITLE])
        nav = make_navigator(ui)
        self.assertEqual(nav.enter_guild_ui(), "first-key")
        self.assertEqual(ui.opened, 0)

    def test_falls_back_to_candidate_and_caches(self):
        ui = FakeUI(["", GUILD_TITLE])
        nav = make_navigator(ui, first_key="bad-key", candidates=("good-key",))
        key = nav.enter_guild_ui()
        self.assertEqual(key, "good-key")
        self.assertEqual(ui.pressed, ["bad-key", "good-key"])
        self.assertEqual(nav.cached_key, "good-key")

    def test_cached_key_is_tried_first_next_time(self):
        ui = FakeUI(["", GUILD_TITLE, GUILD_TITLE])
        nav = make_navigator(ui, first_key="bad-key", candidates=("good-key",))
        nav.enter_guild_ui()
        ui.pressed.clear()
        self.assertEqual(nav.enter_guild_ui(), "good-key")
        self.assertEqual(ui.pressed, ["good-key"])

    def test_all_keys_fail_raises(self):
        ui = FakeUI(["", "", ""])
        nav = make_navigator(ui, first_key="a", candidates=("b",))
        with self.assertRaises(NavigationError):
            nav.enter_guild_ui()

    def test_no_candidate_keys_raises_without_pressing(self):
        ui = FakeUI([])
        nav = make_navigator(ui, first_key=None, candidates=())
        with self.assertRaises(NavigationError):
            nav.enter_guild_ui()
        self.assertEqual(ui.pressed, [])

    def test_empty_strings_are_not_candidates(self):
        ui = FakeUI([GUILD_TITLE])
        nav = make_navigator(ui, first_key="", candidates=("", "good-key"))
        self.assertEqual(nav.enter_guild_ui(), "good-key")


class TestSafeStop(unittest.TestCase):
    def test_settings_without_open_action_raises(self):
        """已进设置界面却没有公会入口动作 → 停下,不在界面上乱点。"""
        ui = FakeUI([SETTINGS_TITLE])
        nav = make_navigator(ui)
        with self.assertRaises(NavigationError):
            nav.enter_guild_ui(open_guild=None)

    def test_open_action_failing_to_reach_guild_raises(self):
        ui = FakeUI([SETTINGS_TITLE, SETTINGS_TITLE])
        nav = make_navigator(ui)
        with self.assertRaises(NavigationError):
            nav.enter_guild_ui(ui.open_guild)
        self.assertEqual(ui.opened, 1)

    def test_consecutive_misclicks_trigger_escape_and_rescan(self):
        ui = FakeUI(["", ""])
        nav = make_navigator(ui, first_key="bad", max_misclick=1)
        with self.assertRaises(NavigationError):
            nav.enter_guild_ui()
        self.assertEqual(ui.escaped, 1)

    def test_invalid_thresholds_rejected(self):
        ui = FakeUI([])
        with self.assertRaises(ValueError):
            make_navigator(ui, max_misclick=0)
        with self.assertRaises(ValueError):
            make_navigator(ui, render_ms=-1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
