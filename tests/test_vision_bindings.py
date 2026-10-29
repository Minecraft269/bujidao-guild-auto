# -*- coding: utf-8 -*-
"""
test_vision_bindings.py —— hotbar 按键绑定的运行时解析
======================================================
核心验收(docs/UI识别模式详解.md §十二):
  * 按键绑定运行时读取:代码/测试中不出现任何硬编码键值作为默认假设;
  * 玩家改键后仍可解析;
  * 文件缺失 / 没有该键 → None,**不返回任何"常见默认键"**。
"""

import os
import tempfile
import unittest

from guild_auto.vision.bindings import (
    binding_to_press_key,
    hotbar_key,
    parse_options_binding,
    read_hotbar_binding,
)

# 下面的键值只是**测试夹具**,用来证明"任意绑定都能解析"。
# 生产代码里没有任何默认键值 —— 解析不到就返回 None。
OPTIONS_TEXT = """\
version:4325
autoJump:false
key_key.hotbar.1:key.keyboard.1
key_key.hotbar.7:key.keyboard.k
key_key.chat:key.keyboard.t
"""


class TestParseOptionsBinding(unittest.TestCase):
    def test_reads_actual_binding(self):
        self.assertEqual(parse_options_binding(OPTIONS_TEXT, 7), "key.keyboard.k")

    def test_rebound_key_still_parsed(self):
        """玩家把 hotbar.7 改到鼠标键/其它字母键后仍能解析(支持任意绑定)。"""
        text = "key_key.hotbar.7:key.mouse.4\n"
        self.assertEqual(parse_options_binding(text, 7), "key.mouse.4")
        text = "key_key.hotbar.7:key.keyboard.q\n"
        self.assertEqual(parse_options_binding(text, 7), "key.keyboard.q")

    def test_missing_slot_returns_none_not_default(self):
        self.assertIsNone(parse_options_binding(OPTIONS_TEXT, 5))

    def test_empty_or_unrelated_text_returns_none(self):
        self.assertIsNone(parse_options_binding("", 7))
        self.assertIsNone(parse_options_binding("key_key.chat:key.keyboard.t\n", 7))

    def test_empty_value_returns_none(self):
        """键存在但值为空(未绑定)→ None,不回退任何默认键。"""
        self.assertIsNone(parse_options_binding("key_key.hotbar.7:\n", 7))

    def test_slot_is_not_confused_with_prefix(self):
        """hotbar.7 不能被 hotbar.70 或 hotbar.7x 误命中。"""
        text = "key_key.hotbar.70:key.keyboard.z\nkey_key.hotbar.7x:key.keyboard.y\n"
        self.assertIsNone(parse_options_binding(text, 7))


class TestReadHotbarBinding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "options.txt")

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, text, encoding="utf-8"):
        with open(self.path, "w", encoding=encoding) as f:
            f.write(text)
        return self.path

    def test_reads_from_file(self):
        path = self._write(OPTIONS_TEXT)
        self.assertEqual(read_hotbar_binding(path, 7), "key.keyboard.k")

    def test_missing_file_returns_none(self):
        missing = os.path.join(self.tmp.name, "nope.txt")
        self.assertIsNone(read_hotbar_binding(missing, 7))

    def test_file_without_slot_returns_none(self):
        path = self._write("key_key.hotbar.1:key.keyboard.1\n")
        self.assertIsNone(read_hotbar_binding(path, 7))

    def test_gbk_file_supported(self):
        path = self._write("key_key.hotbar.7:key.keyboard.k\n", encoding="gbk")
        self.assertEqual(read_hotbar_binding(path, 7), "key.keyboard.k")

    def test_empty_path_returns_none(self):
        self.assertIsNone(read_hotbar_binding("", 7))

    def test_hotbar_key_returns_press_key(self):
        path = self._write(OPTIONS_TEXT)
        self.assertEqual(hotbar_key(path, 7), "k")

    def test_hotbar_key_missing_file_returns_none(self):
        self.assertIsNone(hotbar_key(os.path.join(self.tmp.name, "nope.txt"), 7))


class TestBindingToPressKey(unittest.TestCase):
    def test_keyboard_bindings(self):
        self.assertEqual(binding_to_press_key("key.keyboard.k"), "k")
        self.assertEqual(binding_to_press_key("key.keyboard.7"), "7")
        self.assertEqual(binding_to_press_key("key.keyboard.f5"), "f5")

    def test_mouse_binding_kept_for_adapter(self):
        """鼠标键保持原样交给输入适配器解释,这里不猜具体按钮。"""
        self.assertEqual(binding_to_press_key("key.mouse.4"), "key.mouse.4")

    def test_unbound_and_unknown_return_none(self):
        self.assertIsNone(binding_to_press_key(None))
        self.assertIsNone(binding_to_press_key(""))
        self.assertIsNone(binding_to_press_key("key.keyboard.unknown"))
        self.assertIsNone(binding_to_press_key("key.keyboard.some_future_key"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
