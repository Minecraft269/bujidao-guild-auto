# -*- coding: utf-8 -*-
"""
test_schema_pause_vision.py —— vision.grid / pause.* 校验的边界分支
==================================================================
阶段 6/7 与 US-009 给 schema 加了一批"用户把配置写坏"的分支：快捷栏只能
1~9、网格尺寸必须 ≥1、缺失率必须在 [0,1]、自动暂停的两个键类型。这些分支
不写坏就永远不执行，但它们恰恰是"配置错→启动就报错"的保证。
"""

import json
import unittest
from unittest import mock

from guild_auto.config import DEFAULT_CONFIG, validate_config


def _cfg():
    return json.loads(json.dumps(DEFAULT_CONFIG))


def _errors(cfg):
    return [m for lvl, m in validate_config(cfg) if lvl == "error"]


class TestVisionSection(unittest.TestCase):
    def test_vision_section_must_be_dict(self):
        cfg = _cfg()
        cfg["vision"] = "不是对象"
        self.assertTrue(any("vision" in m for m in _errors(cfg)))

    def test_hotbar_slot_out_of_range_rejected(self):
        for bad in (0, 10):
            cfg = _cfg()
            cfg["vision"]["hotbar_slot"] = bad
            self.assertTrue(any("hotbar_slot" in m for m in _errors(cfg)), f"{bad} 未被拒绝")

    def test_hotbar_slot_fractional_rejected(self):
        cfg = _cfg()
        cfg["vision"]["hotbar_slot"] = 1.5
        self.assertTrue(any("hotbar_slot" in m for m in _errors(cfg)))

    def test_ocr_command_must_be_string(self):
        cfg = _cfg()
        cfg["vision"]["ocr_command"] = 123
        self.assertTrue(any("ocr_command" in m for m in _errors(cfg)))

    def test_grid_must_be_dict(self):
        cfg = _cfg()
        cfg["vision"]["grid"] = [1, 2]
        self.assertTrue(any("vision.grid" in m for m in _errors(cfg)))

    def test_grid_origin_must_be_non_negative_int(self):
        cfg = _cfg()
        cfg["vision"]["grid"]["origin_x"] = -1
        self.assertTrue(any("origin_x" in m for m in _errors(cfg)))

        cfg = _cfg()
        cfg["vision"]["grid"]["origin_y"] = "10"
        self.assertTrue(any("origin_y" in m for m in _errors(cfg)))

    def test_grid_cell_size_must_be_positive(self):
        cfg = _cfg()
        cfg["vision"]["grid"]["cell_width"] = 0
        self.assertTrue(any("cell_width" in m for m in _errors(cfg)))

    def test_grid_missing_ratio_range(self):
        for bad in (-0.1, 1.5):
            cfg = _cfg()
            cfg["vision"]["grid"]["max_missing_ratio"] = bad
            self.assertTrue(any("max_missing_ratio" in m for m in _errors(cfg)), f"{bad} 未被拒绝")

    def test_default_vision_section_is_valid(self):
        self.assertEqual(_errors(_cfg()), [])


class TestPauseSection(unittest.TestCase):
    def test_auto_on_user_input_must_be_bool(self):
        cfg = _cfg()
        cfg["pause"]["auto_on_user_input"] = "true"
        self.assertTrue(any("auto_on_user_input" in m for m in _errors(cfg)))

    def test_grace_ms_must_be_non_negative_int(self):
        for bad in (-1, "300", True):
            cfg = _cfg()
            cfg["pause"]["user_input_grace_ms"] = bad
            self.assertTrue(
                any("user_input_grace_ms" in m for m in _errors(cfg)), f"{bad!r} 未被拒绝"
            )

    def test_grace_ms_zero_is_allowed(self):
        """0 表示不设忽略窗口,是合法配置(不是"缺失")。"""
        cfg = _cfg()
        cfg["pause"]["user_input_grace_ms"] = 0
        self.assertEqual(_errors(cfg), [])


class TestGameLogResolutionFailure(unittest.TestCase):
    def test_abspath_exception_is_swallowed(self):
        """路径解析抛 ValueError 时校验必须继续,不能整体崩掉。"""
        cfg = _cfg()
        with mock.patch("os.path.abspath", side_effect=ValueError("坏路径")):
            validate_config(cfg)  # 不抛异常即通过


if __name__ == "__main__":
    unittest.main(verbosity=2)
