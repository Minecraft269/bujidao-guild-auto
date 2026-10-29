# -*- coding: utf-8 -*-
"""
test_vision_adapters.py —— 真实能力适配器的组装与失败边界
=========================================================
覆盖:
  * 没装 pytesseract 且未配置 ocr_command → 明确报错(禁止静默降级);
  * 配置了外部 OCR 命令 → 按模板执行并取 stdout;
  * 鼠标绑定的按键不能猜坐标 → 明确报错;
  * 每个模拟输入动作前都要打开"脚本输入"忽略窗口(否则自己暂停自己)。

零真实屏幕/OCR:subprocess 与输入原语全部替换。
"""

import copy
import unittest
from types import SimpleNamespace
from unittest import mock

from guild_auto.config import DEFAULT_CONFIG
from guild_auto.vision import adapters as adapters_mod
from guild_auto.vision.adapters import AdapterError, build_default_adapters, has_configured_ocr


def _cfg(ocr_command=""):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["vision"]["ocr_command"] = ocr_command
    return cfg


class _FakeImage:
    def __init__(self):
        self.saved = []

    def save(self, path, fmt):
        self.saved.append((path, fmt))


class _FakeWindowInput:
    def __init__(self):
        self.presses = []
        self.moves = []
        self.clicks = []

    def press_key(self, key):
        self.presses.append(key)

    def move_mouse(self, x, y):
        self.moves.append((x, y))

    def click_mouse(self, x, y):
        self.clicks.append((x, y))


class TestOcrSelection(unittest.TestCase):
    def test_missing_ocr_raises_clear_error(self):
        with mock.patch.object(adapters_mod, "_find_pytesseract", return_value=None):
            self.assertFalse(has_configured_ocr(_cfg()))
            with self.assertRaises(AdapterError) as ctx:
                build_default_adapters(_cfg())
        self.assertIn("未配置 OCR 适配器", str(ctx.exception))

    def test_external_command_is_preferred_and_used(self):
        image = _FakeImage()
        completed = SimpleNamespace(returncode=0, stdout="公会系统\n", stderr="")
        with (
            mock.patch.object(adapters_mod, "_find_pytesseract", return_value=None),
            mock.patch.object(adapters_mod, "_build_capture", return_value=(lambda: image, "fake")),
            mock.patch.object(adapters_mod.subprocess, "run", return_value=completed) as run,
        ):
            cfg = _cfg("ocr-tool --png {image}")
            self.assertTrue(has_configured_ocr(cfg))
            built = build_default_adapters(cfg)
            text, confidence = built.ocr(image)

        self.assertEqual(text, "公会系统\n")
        self.assertEqual(confidence, 1.0)
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], "ocr-tool")
        self.assertTrue(argv[-1].endswith(".png"), f"命令未拿到截图路径: {argv}")
        self.assertEqual(image.saved[0][1], "PNG")

    def test_external_command_failure_reports_stderr(self):
        completed = SimpleNamespace(returncode=2, stdout="", stderr="引擎崩溃")
        with mock.patch.object(adapters_mod.subprocess, "run", return_value=completed):
            ocr = adapters_mod._external_ocr("bad-ocr {image}")
            with self.assertRaises(AdapterError) as ctx:
                ocr(_FakeImage())
        self.assertIn("引擎崩溃", str(ctx.exception))


class TestInputAdapters(unittest.TestCase):
    def test_mouse_binding_is_rejected_not_guessed(self):
        fake_window_input = _FakeWindowInput()
        with mock.patch.object(adapters_mod, "_window_input", return_value=fake_window_input):
            press, _hover, _click, _escape = adapters_mod._build_input(None)
            with self.assertRaises(AdapterError) as ctx:
                press("key.mouse.4")
        self.assertIn("鼠标按键", str(ctx.exception))
        self.assertEqual(fake_window_input.presses, [])

    def test_every_action_marks_simulated_input(self):
        marker = mock.Mock()
        fake_window_input = _FakeWindowInput()
        with mock.patch.object(adapters_mod, "_window_input", return_value=fake_window_input):
            press, hover, click, escape = adapters_mod._build_input(marker)
            press("x")
            hover((10, 20))
            click((30, 40))
            escape()
        self.assertEqual(marker.call_count, 4, "每个模拟动作前都必须打开忽略窗口")
        self.assertEqual(fake_window_input.presses, ["x", "esc"])
        self.assertEqual(fake_window_input.moves, [(10, 20)])
        self.assertEqual(fake_window_input.clicks, [(30, 40)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
