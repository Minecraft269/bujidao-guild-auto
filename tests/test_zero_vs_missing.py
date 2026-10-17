# -*- coding: utf-8 -*-
"""
test_zero_vs_missing.py —— 数据缺失 vs 真实零贡献
================================================
独立审查判定的**本轮唯一可能导致错误清人**的缺陷:
周贡献段标题存在但数值行一行都解析不出时,parse_weekly_exp 原样返回 (0, []),
0 被当成**真实周贡献**参与判定:
    entry 玩家 0 < 3500 → 踢出
    mid   玩家 0 < 5000 → 降职
即"面板被网易遮住 / 渲染未完成"会直接变成"该玩家零贡献,踢掉"。

这类缺陷最危险的地方:它不崩、不报错,只在你看不见的地方把人踢了。

修法:区分"数据缺失"与"真实零贡献"——
  数据缺失(一行有效数值都解析不出) → 抛 ParseError,主流程拿不到 record,
    判定根本不会被调用,该玩家标"需人工确认";
  真实零贡献(数值可解析且全为 0) → 正常返回 0,照常进 entry kick。

**两侧都必须测**:只测前者会把真实零贡献玩家误判成数据缺失而漏踢,
那也是业务错误(§4 红线:合法全零周必须照常进 entry kick)。
"""

import os
import sys
import unittest


from guild_auto.infrastructure import logger  # noqa: E402
from guild_auto.domain.decisions import decide_actions  # noqa: E402
from guild_auto.domain.parsers import ParseError, parse_guild_member, parse_weekly_exp  # noqa: E402

for _n in ("log_debug", "log_info", "log_warning", "log_error",
           "log_critical", "log_trace", "log_exception"):
    if not hasattr(logger, _n):
        setattr(logger, _n, lambda *a, **k: None)

# 阈值取自业务规则 §1(此处仅用于端到端串联,不改动任何判定逻辑)
THRESHOLDS = {
    "high_demote": 30000, "mid_promote": 30000, "mid_demote": 5000,
    "low_promote_2": 30000, "low_promote_1": 10000, "low_demote": 3500,
    "entry_promote_3": 30000, "entry_promote_2": 10000,
    "entry_promote_1": 3500, "entry_kick": 3500,
}


class TestRealZeroContribution(unittest.TestCase):
    """真实零贡献:数值可解析且全为 0 —— 必须正常返回 0。"""

    def test_all_zero_rows_return_zero(self):
        total, daily = parse_weekly_exp([
            "公会经验周贡献:",
            "    今天           : 0 公会经验",
            "    2026-08-12: 0 公会经验",
            "    2026-08-11: 0 公会经验",
        ])
        self.assertEqual(total, 0)
        self.assertTrue(daily, "真实零贡献也应解析出明细(daily 非空)")

    def test_single_zero_row(self):
        total, daily = parse_weekly_exp(["公会经验周贡献:", "    今天: 0 公会经验"])
        self.assertEqual(total, 0)
        self.assertEqual(daily, [("今天", 0)])

    def test_zero_contribution_entry_still_kicks(self):
        """端到端反向护栏:真实零贡献的 entry 玩家仍应被判定踢出。

        防止把"数据缺失"的保护做成"零贡献一律放过" —— 那会让真实零贡献
        玩家逃过清人,同样是业务错误。
        """
        blk = ["演示公会", "零贡献玩家",
               "公会经验周贡献:", "    今天: 0 公会经验",
               "    2026-08-12: 0 公会经验"]
        r = parse_guild_member(blk, expected_id="零贡献玩家")
        self.assertEqual(r["weekly_total"], 0)
        actions, trigger = decide_actions("entry", r["weekly_total"], THRESHOLDS)
        self.assertEqual(actions, ["kick"])
        self.assertEqual(trigger, "entry_kick")


class TestMissingDataMustNotBecomeZero(unittest.TestCase):
    """数据缺失:段存在但数值行解析不出 —— 必须抛错,不得伪装成 0。"""

    def test_all_rows_masked_raises(self):
        """数值全被网易遮成 *** / ??? → 抛 ParseError。"""
        with self.assertRaises(ParseError):
            parse_weekly_exp([
                "公会经验周贡献:",
                "    今天           : *** 公会经验",
                "    2026-08-12: ??? 公会经验",
            ])

    def test_masked_rows_never_reach_kick_decision(self):
        """最关键一条:数据缺失不得产出可判定的记录。

        原缺陷链路:weekly_total=0 → decide_actions("entry",0) → kick。
        现在 parse 阶段就该炸掉,主流程拿不到 record,判定根本不会被调用。
        """
        blk = ["演示公会", "被遮玩家",
               "公会经验周贡献:", "    今天: *** 公会经验"]
        with self.assertRaises(ParseError):
            parse_guild_member(blk, expected_id="被遮玩家")

    def test_partially_masked_still_parses(self):
        """部分行可解析 → 正常求和(不是全遮就不该抛)。"""
        total, daily = parse_weekly_exp([
            "公会经验周贡献:",
            "    今天           : *** 公会经验",   # 遮住,跳不出数值
            "    2026-08-12: 16 公会经验",          # 可解析
        ])
        self.assertEqual(total, 16)

    def test_no_header_does_not_raise(self):
        """无段标题时不由本函数抛错(标题检查归 parse_guild_member 负责)。"""
        total, daily = parse_weekly_exp(["演示公会", "演示玩家甲"])
        self.assertEqual((total, daily), (0, []))

    def test_section_immediately_terminated_raises(self):
        """段标题后紧跟结束分隔线 → 无有效行,同样抛错(不是返回 0)。"""
        with self.assertRaises(ParseError):
            parse_weekly_exp([
                "公会经验周贡献:",
                "-------------------------------------------- [C]",
            ])


if __name__ == "__main__":
    unittest.main(verbosity=2)
