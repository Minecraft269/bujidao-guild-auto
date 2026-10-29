# -*- coding: utf-8 -*-
"""
test_vision_sweep.py —— 网格扫掠、停留等待与完整性自检
=======================================================
验收(docs/UI识别模式详解.md §八·五 / §十二):
  * 停留点按格子坐标网格生成,与图标外观无关;
  * 每个停留点停住等 tooltip 渲染(stay_ms)后再截图识别 —— 不得跳过等待;
  * 扫掠后按「预期格子数 vs 识别到语义的格数」对账,缺口超阈值判不完整。
"""

import unittest

from guild_auto.vision.sweep import (
    MIN_STAY_MS,
    GridSpec,
    Sweeper,
    check_completeness,
    grid_points,
)

MEMBER_TEXT = "某某玩家\n公会身份: 入门成员\n公会经验周贡献: 0 公会经验"


class FakeDevice:
    """注入用假设备:记录悬停/等待/截图,OCR 按脚本返回文本与置信度。"""

    def __init__(self, texts, confidences=None):
        self.hovers = []
        self.sleeps = []
        self.captures = 0
        self._texts = list(texts)
        self._confidences = list(confidences or [])

    def hover(self, point):
        self.hovers.append(point)

    def capture(self):
        self.captures += 1
        return f"shot-{self.captures}"

    def ocr(self, _shot):
        text = self._texts.pop(0)
        confidence = self._confidences.pop(0) if self._confidences else 1.0
        return text, confidence

    def sleep(self, seconds):
        self.sleeps.append(seconds)


def make_sweeper(device, **kwargs):
    params = {
        "spec": GridSpec(0, 0, 36, 36, columns=2, rows=1),
        "expected_cells": 1,
        "stay_ms": MIN_STAY_MS,
        "hover": device.hover,
        "capture": device.capture,
        "ocr": device.ocr,
        "sleep": device.sleep,
    }
    params.update(kwargs)
    return Sweeper(**params)


class TestGridPoints(unittest.TestCase):
    def test_point_count_matches_grid(self):
        points = grid_points(GridSpec(0, 0, 36, 36, columns=3, rows=2))
        self.assertEqual(len(points), 6)

    def test_points_are_cell_centers_in_row_major_order(self):
        points = grid_points(GridSpec(100, 200, 36, 36, columns=2, rows=2))
        self.assertEqual(
            [p.center for p in points], [(118, 218), (154, 218), (118, 254), (154, 254)]
        )

    def test_step_ratio_below_one_densifies(self):
        points = grid_points(GridSpec(0, 0, 36, 36, columns=2, rows=1, step_ratio=0.5))
        self.assertEqual([p.center for p in points], [(18, 18), (36, 18)])

    def test_invalid_grid_rejected(self):
        with self.assertRaises(ValueError):
            grid_points(GridSpec(0, 0, 36, 36, columns=0, rows=1))
        with self.assertRaises(ValueError):
            grid_points(GridSpec(0, 0, 36, 36, columns=1, rows=1, step_ratio=0))
        with self.assertRaises(ValueError):
            grid_points(GridSpec(0, 0, 36, 36, columns=1, rows=1, step_ratio=1.5))


class TestCompleteness(unittest.TestCase):
    def test_exact_coverage(self):
        self.assertEqual(check_completeness(6, 6), (True, 0))

    def test_gap_detected(self):
        complete, missing = check_completeness(6, 4)
        self.assertFalse(complete)
        self.assertEqual(missing, 2)

    def test_tolerance_allows_gap(self):
        self.assertTrue(check_completeness(6, 4, max_missing_ratio=0.5)[0])

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            check_completeness(-1, 0)
        with self.assertRaises(ValueError):
            check_completeness(6, 6, max_missing_ratio=1.5)


class TestSweeper(unittest.TestCase):
    def test_visits_every_point_and_waits_before_capture(self):
        device = FakeDevice([MEMBER_TEXT, ""])
        sweeper = make_sweeper(device, expected_cells=1)
        result = sweeper.sweep()

        self.assertEqual(device.hovers, [(18, 18), (54, 18)])
        self.assertEqual(device.sleeps, [MIN_STAY_MS / 1000.0] * 2)
        self.assertEqual(device.captures, 2)
        self.assertEqual(len(result.points), 2)
        self.assertEqual([e.kind for e in result.elements], ["member", "unknown"])

    def test_completeness_self_check_flags_missing_cells(self):
        """预期 2 格但只识别到 1 格语义 → 不完整,交给上层重扫。"""
        device = FakeDevice([MEMBER_TEXT, ""])
        result = make_sweeper(device, expected_cells=2).sweep()
        self.assertFalse(result.complete)
        self.assertEqual(result.recognized_cells, 1)
        self.assertEqual(result.missing_cells, 1)

    def test_low_confidence_retries_once(self):
        device = FakeDevice([MEMBER_TEXT, MEMBER_TEXT], confidences=[0.2, 0.9])
        sweeper = make_sweeper(
            device, low_confidence=0.7, spec=GridSpec(0, 0, 36, 36, columns=1, rows=1)
        )
        result = sweeper.sweep()
        self.assertEqual(device.captures, 2)
        self.assertEqual(result.elements[0].kind, "member")
        self.assertEqual(result.elements[0].confidence, 0.9)

    def test_high_confidence_not_retried(self):
        device = FakeDevice([MEMBER_TEXT], confidences=[0.95])
        sweeper = make_sweeper(device, low_confidence=0.7, spec=GridSpec(0, 0, 36, 36, 1, 1))
        sweeper.sweep()
        self.assertEqual(device.captures, 1)

    def test_stay_below_minimum_rejected(self):
        """太短的停留会漏 tooltip,必须拒绝构造而不是悄悄跑快。"""
        device = FakeDevice([])
        with self.assertRaises(ValueError):
            make_sweeper(device, stay_ms=MIN_STAY_MS - 1)

    def test_invalid_low_confidence_rejected(self):
        device = FakeDevice([])
        with self.assertRaises(ValueError):
            make_sweeper(device, low_confidence=1.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
