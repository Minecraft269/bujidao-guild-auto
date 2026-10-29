# -*- coding: utf-8 -*-
"""
test_vision_runner.py —— 定位 → 采集 → 转 records
==================================================
覆盖:成功采集产出与指令模式同结构的记录(额外带 online)、
任何一步失败都安全回退(不盲点)、不可读字段保持哨兵不被写成 0。
"""

import unittest

from guild_auto.vision.models import UNREADABLE, Rect, UIElement
from guild_auto.vision.navigator import NavigationError
from guild_auto.vision.runner import VisionRunner
from guild_auto.vision.sweep import SweepResult

MEMBER_TEXT = """\
某某玩家
公会身份: 中等活跃成员
首次加入: 2026-09-26 21:38:03
上次在线: 2026-10-25 19:55:27(2.75天前)
公会经验周贡献: 6,799 公会经验
    今天: 0 公会经验
    2026-10-27: 0 公会经验
    2026-10-26: 0 公会经验
    2026-10-25: 564 公会经验
    2026-10-24: 1,310 公会经验
    2026-10-23: 4,925 公会经验
    2026-10-22: 0 公会经验
"""


def element(kind, text):
    return UIElement(kind=kind, rect=Rect(0, 0, 36, 36), text=text, confidence=0.99)


def sweep_result(elements, *, complete=True, expected=2, recognized=None):
    recognized = (
        len([e for e in elements if e.kind != "unknown"]) if recognized is None else recognized
    )
    return SweepResult(
        elements=tuple(elements),
        points=tuple(e.rect for e in elements),
        expected_cells=expected,
        recognized_cells=recognized,
        missing_cells=max(expected - recognized, 0),
        complete=complete,
    )


class FakeNavigator:
    def __init__(self, *, screen="guild", key="bound-key", error=None):
        self.screen = screen
        self.key = key
        self.error = error
        self.open_called = 0

    def enter_guild_ui(self, open_guild=None):
        if self.error is not None:
            raise self.error
        if open_guild is not None:
            open_guild()
            self.open_called += 1
        return self.key

    def current_screen(self):
        return self.screen


class FakeSweeper:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def sweep(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def make_runner(navigator, sweeper, **kwargs):
    return VisionRunner(navigator=navigator, sweeper=sweeper, **kwargs)


class TestMemberTotalFromGuildDetail(unittest.TestCase):
    """成员总数必须来自公会详情 tooltip,不能拿扫到的条数冒充。

    这是终审指出的对账自证循环修复点:读不到就要如实返回 None,
    让上层退回逐页累计并标注来源。
    """

    def _run(self, reader, elements=None):
        elements = elements or [
            element("member", MEMBER_TEXT),
            element("guild_detail", "公会详情"),
        ]
        runner = make_runner(
            FakeNavigator(),
            FakeSweeper(sweep_result(elements, expected=len(elements))),
            guild_detail_reader=reader,
        )
        return runner.collect()

    def test_parses_member_total_from_tooltip(self):
        result = self._run(lambda _e: "公会详情\n人数: [3/50]\n今日经验: 1,000")
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.member_total, 3)

    def test_missing_detail_element_yields_none(self):
        result = self._run(lambda _e: "人数: [3/50]", elements=[element("member", MEMBER_TEXT)])
        self.assertIsNone(result.member_total, "没有公会详情物品时不得凭空得出总数")

    def test_reader_failure_yields_none(self):
        def boom(_e):
            raise RuntimeError("OCR 挂了")

        self.assertIsNone(self._run(boom).member_total)

    def test_reader_without_total_line_yields_none(self):
        self.assertIsNone(self._run(lambda _e: "公会详情\n名称: 演示公会").member_total)

    def test_no_reader_configured_yields_none(self):
        runner = make_runner(
            FakeNavigator(),
            FakeSweeper(sweep_result([element("member", MEMBER_TEXT)], expected=1)),
        )
        self.assertIsNone(runner.collect().member_total)


class TestCollectSuccess(unittest.TestCase):
    def test_member_records_include_online(self):
        navigator = FakeNavigator()
        sweeper = FakeSweeper(
            sweep_result(
                [element("member", MEMBER_TEXT), element("party", "发起组队")],
                expected=2,
            )
        )
        runner = make_runner(navigator, sweeper, online_sampler=lambda _e: True)
        result = runner.collect()

        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.key_used, "bound-key")
        self.assertEqual(len(result.records), 1)
        record = result.records[0]
        self.assertEqual(record["player"], "某某玩家")
        self.assertEqual(record["weekly_total"], 6799)
        self.assertIs(record["online"], True)
        self.assertEqual(record["source"], "ui")
        self.assertEqual(len(record["daily"]), 7)

    def test_no_online_sampler_marks_unreadable(self):
        """没注入像素采样就不能猜在线/离线,记不可读。"""
        sweeper = FakeSweeper(sweep_result([element("member", MEMBER_TEXT)], expected=1))
        result = make_runner(FakeNavigator(), sweeper).collect()
        self.assertTrue(result.ok, result.reason)
        self.assertIsNone(result.records[0]["online"])
        self.assertIn("online", result.records[0]["unreadable"])

    def test_unreadable_weekly_stays_sentinel(self):
        text = MEMBER_TEXT.replace("公会经验周贡献: 6,799 公会经验", "")
        sweeper = FakeSweeper(sweep_result([element("member", text)], expected=1))
        result = make_runner(FakeNavigator(), sweeper, online_sampler=lambda _e: False).collect()
        self.assertEqual(result.records[0]["weekly_total"], UNREADABLE)
        self.assertIn("weekly_total", result.records[0]["unreadable"])


class TestSafeFallback(unittest.TestCase):
    def test_navigation_failure_returns_error_without_sweeping(self):
        sweeper = FakeSweeper(sweep_result([]))
        runner = make_runner(FakeNavigator(error=NavigationError("按键试探失败")), sweeper)
        result = runner.collect()
        self.assertFalse(result.ok)
        self.assertEqual(result.records, ())
        self.assertIn("按键试探失败", result.reason)
        self.assertEqual(sweeper.calls, 0)

    def test_unexpected_screen_stops_collection(self):
        sweeper = FakeSweeper(sweep_result([]))
        result = make_runner(FakeNavigator(screen="settings"), sweeper).collect()
        self.assertFalse(result.ok)
        self.assertIn("不是公会系统", result.reason)
        self.assertEqual(sweeper.calls, 0)

    def test_incomplete_sweep_stops_collection(self):
        sweeper = FakeSweeper(
            sweep_result([element("member", MEMBER_TEXT)], complete=False, expected=3, recognized=1)
        )
        result = make_runner(FakeNavigator(), sweeper).collect()
        self.assertFalse(result.ok)
        self.assertIn("扫掠不完整", result.reason)
        self.assertEqual(result.records, ())

    def test_sweep_exception_stops_collection(self):
        sweeper = FakeSweeper(error=ValueError("停留时长非法"))
        result = make_runner(FakeNavigator(), sweeper).collect()
        self.assertFalse(result.ok)
        self.assertIn("扫掠失败", result.reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
