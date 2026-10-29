# -*- coding: utf-8 -*-
"""
test_report_analysis.py —— 第三份「运行分析报告」的回归
======================================================
对应 docs/详细报告规范.md 的验收项:

  * report_stats 的四个纯函数:数值正确性与边界(空列表 / 单元素 / 缺 weekly_total)
  * 报告七节齐全;command 模式不出第六节,ui 模式出第六节
  * 全员名册对账:名册条目数 = 成员总数时差值必须为 0
  * report.analysis_enabled=false 时不产第三份,前两份照常
  * 前两份报告**逐字节不变**(硬编码 golden 文本比对)

最后一条最关键:第三份是"只增不改"的新文件,前两份格式必须与改造前
一字不差 —— 用整文件逐字节比对才有意义(片段 assertIn 抓不住空行/
顺序/尾换行的变化)。
"""

import copy
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime

from guild_auto.config import DEFAULT_CONFIG
from guild_auto.domain.report_stats import (
    action_counts,
    contribution_stats,
    percentiles,
    rank_transition_matrix,
)
from guild_auto.domain.reporter import (
    build_roster,
    write_action_report,
    write_analysis_report,
    write_contribution_report,
)
from tests.test_main_flow import _MainCase


def _cfg():
    """深拷贝默认配置:报告要读 rank_names / thresholds / switches 等真实配置。"""
    return copy.deepcopy(DEFAULT_CONFIG)


def _today():
    return date.today().strftime("%Y-%m-%d")


def _records():
    return [
        {
            "player": "合成玩家甲",
            "rank": "high",
            "weekly_total": 25000,
            "joined_at": "2026-02-11 10:22:31",
            "last_online": "2026-10-17 19:03",
            "daily": [("今天", 0), ("10-16", 25000)],
            "actions": ["demote"],
            "trigger": "high_demote",
            "executed": True,
            "error": None,
            "new_rank": "mid",
        },
        {
            "player": "合成玩家乙",
            "rank": "entry",
            "weekly_total": 3000,
            "joined_at": None,
            "last_online": None,
            "actions": ["kick"],
            "trigger": "entry_kick",
            "executed": True,
            "error": "执行失败(kick): 权限不足",
            "new_rank": "entry",
        },
    ]


def _roster():
    """5 人名册:3 人已处理 + 1 屏蔽词 + 1 查询失败。"""
    return [
        {"player": "合成甲", "rank": "high", "status": "processed"},
        {"player": "合成乙", "rank": "mid", "status": "processed"},
        {"player": "合成丙", "rank": "low", "status": "processed"},
        {"player": "********", "rank": "entry", "status": "blocked", "reason": "屏蔽词"},
        {"player": "合成丁", "rank": "entry", "status": "query_failed", "reason": "查询重试耗尽"},
    ]


def _run_meta(mode="command", member_total=5):
    return {
        "started_at": datetime(2026, 10, 17, 20, 14, 3),
        "ended_at": datetime(2026, 10, 17, 21, 7, 42),
        "operation_mode": mode,
        "build_variant": "source",
        "capabilities": ["core", "async"],
        "stage_timings": [
            {"name": "查询公会列表", "seconds": 4, "ok": 1, "fail": 0, "note": ""},
            {"name": "逐人查询", "seconds": 2292, "ok": 3, "fail": 1, "note": ""},
        ],
        "roster": _roster(),
        "counts": {
            "member_total": member_total,
            "online_total": 43,
            "queried": 3,
            "decided": 2,
            "query_ok": 2,
            "query_fail": 1,
            "blocked_name": 1,
        },
    }


class _TmpDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="report_analysis_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))


class TestPercentiles(unittest.TestCase):
    def test_linear_interpolation(self):
        """线性插值口径:(n-1)*p 位置插值,四舍五入到整数。"""
        self.assertEqual(
            percentiles([60, 0, 40, 20]),
            {"p25": 15, "p50": 30, "p75": 45, "p90": 54},
        )

    def test_empty_list_returns_none(self):
        self.assertEqual(percentiles([]), {"p25": None, "p50": None, "p75": None, "p90": None})

    def test_single_value(self):
        self.assertEqual(percentiles([7]), {"p25": 7, "p50": 7, "p75": 7, "p90": 7})

    def test_non_numeric_and_bool_count_as_zero(self):
        """weekly_total 缺失/非数值不得抛异常;bool 不能被当成 1 分贡献。"""
        self.assertEqual(
            percentiles([None, "坏值", True, 10]),
            {"p25": 0, "p50": 0, "p75": 2, "p90": 7},
        )


class TestContributionStats(unittest.TestCase):
    def test_basic_values(self):
        stats = contribution_stats(
            [
                {"player": "甲", "weekly_total": 100},
                {"player": "乙", "weekly_total": 300},
                {"player": "丙", "weekly_total": 200},
            ]
        )
        self.assertEqual(stats["count"], 3)
        self.assertEqual(stats["total"], 600)
        self.assertEqual(stats["mean"], 200)
        self.assertEqual(stats["median"], 200)
        self.assertEqual((stats["max_value"], stats["max_player"]), (300, "乙"))
        self.assertEqual((stats["min_value"], stats["min_player"]), (100, "甲"))

    def test_empty_list(self):
        stats = contribution_stats([])
        self.assertEqual(stats["count"], 0)
        self.assertEqual(stats["total"], 0)
        self.assertEqual(stats["max_player"], "")

    def test_missing_weekly_total_counts_as_zero(self):
        stats = contribution_stats(
            [
                {"player": "甲"},
                {"player": "乙", "weekly_total": "坏值"},
                {"player": "丙", "weekly_total": 40},
            ]
        )
        self.assertEqual(stats["count"], 3)
        self.assertEqual(stats["total"], 40)
        self.assertEqual(stats["mean"], 13)
        self.assertEqual(stats["median"], 0)
        self.assertEqual((stats["max_value"], stats["max_player"]), (40, "丙"))
        self.assertEqual((stats["min_value"], stats["min_player"]), (0, "甲"))


class TestRankTransitionMatrix(unittest.TestCase):
    def test_rows_sum_to_rank_counts(self):
        records = [
            {"rank": "high", "new_rank": "mid"},
            {"rank": "high", "new_rank": "high"},
            {"rank": "high"},
            {"rank": "entry", "new_rank": "kicked"},
            {"rank": "mid", "new_rank": "未知值"},
        ]
        matrix = rank_transition_matrix(records, ["high", "mid", "low", "entry"])
        self.assertEqual(matrix["high"]["mid"], 1)
        self.assertEqual(matrix["high"]["unchanged"], 2)
        self.assertEqual(matrix["entry"]["kicked"], 1)
        # 未知新等级保守归入"不变",不得凭空多出一列
        self.assertEqual(matrix["mid"]["unchanged"], 1)
        self.assertEqual(sum(matrix["low"].values()), 0)
        for rank, cells in matrix.items():
            self.assertEqual(
                sum(cells.values()),
                sum(1 for r in records if r.get("rank") == rank),
                f"{rank} 行之和应等于该等级记录数",
            )

    def test_empty_records(self):
        matrix = rank_transition_matrix([], ["high", "entry"])
        self.assertEqual(sum(matrix["high"].values()), 0)

    def test_rank_outside_order_is_ignored(self):
        """跳过职位(leader 等)没有判定结果,不属于迁移矩阵,不得凭空加行。"""
        matrix = rank_transition_matrix(
            [{"rank": "leader", "new_rank": "leader"}], ["high", "entry"]
        )
        self.assertEqual(set(matrix), {"high", "entry"})
        self.assertEqual(sum(matrix["high"].values()), 0)


class TestActionCounts(unittest.TestCase):
    def test_counts_by_first_action(self):
        counts = action_counts(
            [
                {"actions": []},
                {"actions": ["promote", "promote"]},
                {"actions": ["demote"]},
                {"actions": ["kick"]},
                {"actions": ["notify"]},
                {},
            ]
        )
        self.assertEqual(counts, {"promote": 1, "demote": 1, "kick": 1, "none": 3})


class TestBuildRoster(_TmpDirCase):
    def test_status_from_records_and_failures(self):
        groups = {
            "leader": ["会长甲"],
            "high": ["合成甲", "黑名单乙"],
            "entry": ["********", "合成丁"],
        }
        records = [
            {"player": "合成甲", "rank": "high", "actions": [], "trigger": None},
            {"player": "黑名单乙", "rank": "high", "actions": [], "trigger": "blacklist"},
        ]
        failures = [
            {"player": "合成丁", "rank": "entry", "reason": "查询重试耗尽"},
        ]
        roster = build_roster(groups, records, failures, _cfg())
        statuses = {str(e["player"]): e["status"] for e in roster}
        self.assertEqual(statuses["会长甲"], "skip_rank")
        self.assertEqual(statuses["合成甲"], "processed")
        self.assertEqual(statuses["黑名单乙"], "blacklist")
        self.assertEqual(statuses["********"], "blocked")
        self.assertEqual(statuses["合成丁"], "query_failed")
        self.assertEqual(len(roster), 5)

    def test_player_with_no_record_and_no_failure_still_listed(self):
        """名册必须零遗漏:既没查成功也没报错的人也要出现。"""
        roster = build_roster({"high": ["查漏甲"]}, [], [], _cfg())
        self.assertEqual(len(roster), 1)
        self.assertEqual(roster[0]["status"], "unprocessed")


class TestAnalysisReportSections(_TmpDirCase):
    def _write(self, mode="command", member_total=5, cfg=None):
        cfg = cfg or _cfg()
        path = write_analysis_report(
            self.tmp, "演示公会", _records(), cfg, _run_meta(mode, member_total)
        )
        self.assertIsNotNone(path, "报告写入失败")
        with open(path, encoding="utf-8") as f:
            return path, f.read()

    def test_seven_sections_present(self):
        path, text = self._write()
        self.assertEqual(os.path.basename(path), f"{_today()} 演示公会公会 运行分析报告.txt")
        expected_headers = [
            "=== 演示公会公会 运行分析报告 ===",
            "--- 一、阶段耗时 ---",
            "--- 二、逐人决策明细 ---",
            "--- 三、失败与跳过 ---",
            "--- 四、统计汇总 ---",
            "--- 五、配置快照 ---",
            "--- 七、全员名册",
        ]
        for header in expected_headers:
            self.assertIn(header, text, f"缺少 {header}")

    def test_command_mode_has_no_ui_section(self):
        _, text = self._write(mode="command")
        self.assertNotIn("--- 六、UI识别运行记录 ---", text)

    def test_ui_mode_has_ui_section(self):
        _, text = self._write(mode="ui")
        self.assertIn("--- 六、UI识别运行记录 ---", text)

    def test_decision_triple_and_daily_detail(self):
        """每个判定过的玩家都要有「阈值 + 输入值 + 结论」三元组。"""
        _, text = self._write()
        self.assertIn("阈值 high_demote(<=30000) 输入 25000 → 降职", text)
        self.assertIn("阈值 entry_kick(<3500) 输入 3000 → 踢出", text)
        self.assertIn("今天 0 / 10-16 25000", text)
        self.assertIn("执行失败(kick): 权限不足", text)

    def test_daily_detail_can_be_disabled(self):
        cfg = _cfg()
        cfg["report"]["include_daily_detail"] = False
        _, text = self._write(cfg=cfg)
        self.assertNotIn("(每日:", text)

    def test_config_snapshot_can_be_disabled(self):
        cfg = _cfg()
        cfg["report"]["include_config_snapshot"] = False
        _, text = self._write(cfg=cfg)
        self.assertIn("已按 report.include_config_snapshot=false 关闭", text)

    def test_counts_are_rendered(self):
        _, text = self._write()
        self.assertIn("成员总数: 5", text)
        self.assertIn("查询失败: 1", text)
        self.assertIn("屏蔽词不可查: 1", text)

    def test_write_failure_logs_warning_and_returns_none(self):
        """报告目录不可写时不得抛异常(报告是流程最后一步,不能拖垮运行)。"""
        blocker = os.path.join(self.tmp, "占位文件")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        self.assertIsNone(
            write_analysis_report(blocker, "演示公会", _records(), _cfg(), _run_meta())
        )


class TestRosterReconciliation(_TmpDirCase):
    def _text(self, member_total):
        path = write_analysis_report(
            self.tmp, "演示公会", _records(), _cfg(), _run_meta(member_total=member_total)
        )
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_five_entries_match_member_total(self):
        text = self._text(5)
        self.assertIn("--- 七、全员名册(共 5 人 = 成员总数 5,无遗漏)---", text)
        self.assertIn("名册条目合计: 5", text)
        self.assertIn("差值: 0 ✅ 无遗漏", text)
        # 被跳过 / 屏蔽词 / 查询失败的人都必须出现在名册里
        self.assertIn("********", text)
        self.assertIn("合成丁", text)
        self.assertIn("屏蔽词不可查", text)

    def test_mismatch_reports_defect(self):
        text = self._text(6)
        self.assertIn("差值: -1 ❌ 报告缺陷", text)


class TestAnalysisToggleInMain(_MainCase):
    """report.analysis_enabled 控制第三份是否产出,且不影响前两份。"""

    def test_enabled_by_default(self):
        mocks = self._run()
        mocks["write_contrib"].assert_called_once()
        mocks["write_action"].assert_called_once()
        mocks["write_analysis"].assert_called_once()

    def test_disabled_skips_third_report_only(self):
        mocks = self._run(cfg_override={"report": {"analysis_enabled": False}})
        mocks["write_contrib"].assert_called_once()
        mocks["write_action"].assert_called_once()
        mocks["write_analysis"].assert_not_called()


class TestFirstTwoReportsByteIdentical(_TmpDirCase):
    """前两份报告逐字节回归:整文件 golden 比对(片段断言抓不到排版漂移)。"""

    def test_contribution_report_golden(self):
        records = [
            {"player": "甲", "rank": "high", "weekly_total": 25000, "actions": ["demote"]},
            {"player": "乙", "rank": "entry", "weekly_total": 40000, "actions": ["promote"] * 3},
            {"player": "丙", "rank": "mid", "weekly_total": 100, "actions": []},
        ]
        path = write_contribution_report(self.tmp, "演示公会", records, "演示管理员")
        self.assertEqual(os.path.basename(path), f"{_today()} 演示公会公会 所有玩家详细贡献.txt")
        expected = (
            f"=== 演示公会公会 所有玩家详细贡献(统计日 {_today()}) ===\n"
            "执行人: 演示管理员\n"
            "\n"
            "统计玩家数: 3   周贡献合计: 65100\n"
            "\n"
            "---高活跃---\n"
            "玩家:甲 周贡献为:25000 执行操作:降职\n"
            "\n"
            "---中等活跃---\n"
            "玩家:丙 周贡献为:100 执行操作:无\n"
            "\n"
            "---入门成员---\n"
            "玩家:乙 周贡献为:40000 执行操作:升职(3次)\n"
        )
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), expected)

    def test_action_report_golden(self):
        action_records = [
            {
                "player": "K1",
                "rank": "entry",
                "new_rank": "kicked",
                "weekly_total": 3000,
                "actions": ["kick"],
                "trigger": "entry_kick",
                "executed": True,
                "error": None,
            },
            {
                "player": "D1",
                "rank": "high",
                "new_rank": "mid",
                "weekly_total": 20000,
                "actions": ["demote"],
                "trigger": "high_demote",
                "executed": True,
                "error": None,
            },
        ]
        cfg = {"contact_text": "若有异议联系群管理员{admin}", "admin": "演示管理员"}
        path = write_action_report(self.tmp, "演示公会", action_records, cfg)
        self.assertEqual(os.path.basename(path), f"演示公会_{_today()}.txt")
        expected = (
            "被升职（0人）\n"
            "若有异议联系群管理员演示管理员\n"
            "\n"
            "被踢出（1人）\n"
            "K1\n"
            "若有异议联系群管理员演示管理员\n"
            "\n"
            "被降职（1人）\n"
            "D1\n"
            "若有异议联系群管理员演示管理员\n"
        )
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
