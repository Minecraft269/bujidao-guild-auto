# -*- coding: utf-8 -*-
"""
test_parsers_edges.py —— 解析器边界分支补齐
=============================================
补齐 test_parsers.py 的覆盖率缺口,全部是**真实业务边界**(不是为凑数而写的空测试):

  1. 复制标记 "[C]" 的两种写法(" [C]" 带空格 / "[C]" 不带空格)—— 网易版客户端
     "点击复制"功能有人开有人关,两种都要能剥离
  2. 玩家名被游戏截断成两行时的拼接(上行不以 ● 结尾 → 与下行合并)
  3. 周贡献段内出现"非日期、非今天"的其它数值行(面板新增字段)也应计入
     ——业务规则 §4:段内**所有** "数值 公会经验" 行求和
  4. 周贡献段在遇到分隔线/加入时间/上次在线时正确终止(不能把段外数据算进来)
  5. 玩家名校验的注入防护(路径分隔符/控制字符/超长)
  6. 解析失败必须抛 ParseError 并给出中文原因
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logger  # noqa: E402
from parsers import (  # noqa: E402
    ParseError,
    parse_guild_list,
    parse_guild_member,
    parse_weekly_exp,
    validate_player_name,
)

for _n in ("log_debug", "log_info", "log_warning", "log_error",
           "log_critical", "log_trace", "log_exception"):
    if not hasattr(logger, _n):
        setattr(logger, _n, lambda *a, **k: None)

RANK_NAMES = {
    "leader": "会长",
    "vice_leader": "副会长",
    "admin": "管理员",
    "high": "高活跃",
    "mid": "中等活跃",
    "low": "低活跃",
    "entry": "入门成员",
}


class TestCopyMarkerVariants(unittest.TestCase):
    """复制标记 "[C]" 的两种写法都要能剥离(补 line 70/72 缺口)。"""

    def test_marker_with_space_stripped(self):
        """带空格 " [C]":应被剥离。"""
        total, _ = parse_weekly_exp(["公会经验周贡献:", "    今天           : 0 公会经验 [C]"])
        self.assertEqual(total, 0)

    def test_marker_without_space_stripped(self):
        """不带空格 "[C]":也应被剥离,数值正确解析(补 line 72 缺口)。"""
        total, _ = parse_weekly_exp(["公会经验周贡献:", "    2026-08-12: 16 公会经验[C]"])
        self.assertEqual(total, 16, "无空格 [C] 变体未正确剥离,导致整行解析失败")

    def test_marker_with_space_value_parsed(self):
        """带空格 "[C]" 且有真实数值:数值必须解析出来(不是被标记吃掉)。"""
        total, daily = parse_weekly_exp(
            ["公会经验周贡献:", "    2026-08-12: 1,322 公会经验 [C]"])
        self.assertEqual(total, 1322)
        self.assertEqual(daily, [("2026-08-12", 1322)])


class TestWeeklySectionBoundary(unittest.TestCase):
    """周贡献段必须在正确位置终止,段外数据不得计入(补 line 93 缺口)。"""

    def test_section_stops_at_dash_line(self):
        """遇到分隔线终止:分隔线后的数值不计入。"""
        total, _ = parse_weekly_exp([
            "公会经验周贡献:",
            "    今天: 10 公会经验",
            "-------------------------------------------- [C]",
            "    2026-01-01: 99999 公会经验",   # 段外,不得计入
        ])
        self.assertEqual(total, 10, "分隔线后的数据被错误计入周贡献")

    def test_section_stops_at_joined_at(self):
        """遇到"加入时间"终止(段外字段不得计入)。"""
        total, _ = parse_weekly_exp([
            "公会经验周贡献:",
            "    今天: 5 公会经验",
            "加入时间: 2026-08-07 19:34:16",
            "    2026-01-01: 88888 公会经验",
        ])
        self.assertEqual(total, 5)

    def test_section_stops_at_last_online(self):
        """遇到"上次在线"终止。"""
        total, _ = parse_weekly_exp([
            "公会经验周贡献:",
            "    今天: 7 公会经验",
            "上次在线: 2026-08-12 18:40:19(2.14天前)",
            "    2026-01-01: 77777 公会经验",
        ])
        self.assertEqual(total, 7)

    def test_lines_before_header_ignored(self):
        """周贡献标题**之前**的数值行不得计入。"""
        total, _ = parse_weekly_exp([
            "公会名",
            "    2026-01-01: 12345 公会经验",   # 标题之前,不得计入
            "公会经验周贡献:",
            "    今天: 3 公会经验",
        ])
        self.assertEqual(total, 3, "标题段之前的数据被错误计入")

    def test_other_numeric_line_in_section_counted(self):
        """段内其它含数值的行(如面板新增字段)也计入(补 line 105-107 缺口)。

        业务规则 §4:该段内**所有** "数值 公会经验" 行求和。"""
        total, _ = parse_weekly_exp([
            "公会经验周贡献:",
            "    今天: 10 公会经验",
            "    累计: 20 公会经验",       # 非日期、非今天的行
        ])
        self.assertEqual(total, 30, "段内其它数值行未被计入(业务规则 §4 要求全部计入)")

    def test_thousands_separator_removed(self):
        """千分位逗号必须去除后累加(1,322 → 1322)。"""
        total, _ = parse_weekly_exp([
            "公会经验周贡献:",
            "    今天: 1,000 公会经验",
            "    2026-08-12: 1,322 公会经验",
            "    2026-08-11: 2,500 公会经验",
        ])
        self.assertEqual(total, 4822)

    def test_empty_lines_skipped(self):
        """段内空行应跳过,不影响求和。"""
        total, _ = parse_weekly_exp([
            "公会经验周贡献:",
            "",
            "    今天: 4 公会经验",
            "   ",
            "    2026-08-12: 6 公会经验",
        ])
        self.assertEqual(total, 10)


class TestPlayerNameTruncationJoin(unittest.TestCase):
    """玩家名被游戏截断成两行时的拼接(补 line 151-157 缺口)。"""

    def test_two_line_name_joined_with_space(self):
        """名字跨行截断 → 拼接为一个完整名字。

        真实形态(见 tests/samples/guild_list_sample.txt):被截断的名字**独占一行
        且不以 ● 结尾**,下一行是它的后半段。注意:若该行本身以 ● 结尾,
        解析器按条目分隔符切分,不会跨行拼接 —— 所以这里不能带 ●。"""
        lines = [
            "演示公会",
            "-- 入门成员 --",
            "正常玩家 ●",
            "一个超长的演示玩家名字被游戏截断成两行",
            "接上后半段 ●",
            "成员总数: 2",
            "在线成员数: 1",
        ]
        r = parse_guild_list(lines, RANK_NAMES)
        self.assertIn("一个超长的演示玩家名字被游戏截断成两行 接上后半段",
                      r["groups"]["entry"],
                      "跨行截断的玩家名未被拼接")

    def test_join_stops_at_group_title(self):
        """拼接必须在遇到下一个分组标题时停止(不得把标题吞进名字)。"""
        lines = [
            "演示公会",
            "-- 入门成员 --",
            "被截断的名字",
            "-- 高活跃 --",
            "高活跃玩家 ●",
        ]
        r = parse_guild_list(lines, RANK_NAMES)
        self.assertIn("高活跃玩家", r["groups"]["high"])
        # 关键:标题没被拼进低等级的名字里
        for p in r["groups"]["entry"]:
            self.assertNotIn("--", p, f"分组标题被吞进玩家名: {p!r}")

    def test_join_stops_at_total_line(self):
        """拼接必须在遇到"成员总数"时停止。"""
        lines = [
            "演示公会",
            "-- 入门成员 --",
            "被截断的名字",
            "成员总数: 5",
        ]
        r = parse_guild_list(lines, RANK_NAMES)
        for p in r["groups"]["entry"]:
            self.assertNotIn("成员总数", p, f"统计行被吞进玩家名: {p!r}")


class TestValidatePlayerName(unittest.TestCase):
    """玩家名校验的注入防护(安全边界,必须有断言)。"""

    def test_valid_name_accepted(self):
        self.assertEqual(validate_player_name("正常玩家"), "正常玩家")

    def test_name_with_space_accepted(self):
        """真实样本存在"带空格 名字",必须接受(不能用严格白名单)。"""
        self.assertEqual(validate_player_name("带空格 演示名"), "带空格 演示名")

    def test_path_separator_rejected(self):
        """路径分隔符必须拒绝(防报告文件名穿越)。"""
        for bad in ("a/b", "a\\b"):
            with self.assertRaises(ParseError):
                validate_player_name(bad)

    def test_control_chars_rejected(self):
        """控制字符必须拒绝(防命令参数错位)。"""
        for bad in ("a\nb", "a\tb", "a\x00b", "a\rb"):
            with self.assertRaises(ParseError):
                validate_player_name(bad)

    def test_bullet_separator_rejected(self):
        """条目分隔符 ● 必须拒绝(否则伪造面板可注入幽灵条目)。"""
        with self.assertRaises(ParseError):
            validate_player_name("evil ● ghost")

    def test_format_braces_rejected(self):
        """花括号必须拒绝(防 format 注入)。"""
        with self.assertRaises(ParseError):
            validate_player_name("{player}")

    def test_empty_rejected(self):
        with self.assertRaises(ParseError):
            validate_player_name("")

    def test_none_rejected(self):
        with self.assertRaises(ParseError):
            validate_player_name(None)

    def test_too_long_rejected(self):
        """超过 48 字符必须拒绝。"""
        with self.assertRaises(ParseError):
            validate_player_name("x" * 49)

    def test_max_length_accepted(self):
        """恰好 48 字符必须接受(边界)。"""
        self.assertEqual(validate_player_name("x" * 48), "x" * 48)

    def test_colon_rejected(self):
        """冒号必须拒绝(Windows 文件名非法字符)。"""
        with self.assertRaises(ParseError):
            validate_player_name("a:b")


class TestParseErrors(unittest.TestCase):
    """解析失败必须抛 ParseError(补 line 133/196/234/253 缺口)。"""

    def test_empty_block_raises(self):
        """空块 → ParseError。"""
        with self.assertRaises(ParseError):
            parse_guild_list(["", "   "], RANK_NAMES)

    def test_no_groups_raises(self):
        """只有公会名无任何分组玩家 → ParseError。"""
        with self.assertRaises(ParseError):
            parse_guild_list(["演示公会"], RANK_NAMES)

    def test_unknown_group_title_yields_no_members(self):
        """分组标题不在 rank_names 中 → 该组不收玩家;全都不认识则 ParseError。"""
        with self.assertRaises(ParseError):
            parse_guild_list(["演示公会", "-- 未知职位 --", "玩家甲 ●"], RANK_NAMES)

    def test_member_too_short_raises(self):
        """/guild member 输出过短(缺公会名/玩家名)→ ParseError。"""
        with self.assertRaises(ParseError):
            parse_guild_member(["只有一个公会名行"], expected_id=None)

    def test_member_missing_weekly_section_raises(self):
        """缺"公会经验周贡献"段 → ParseError。"""
        block = ["演示公会", "演示成员", "加入时间: 2026-08-07 19:34:16",
                 "上次在线: 2026-08-12 18:40:19"]
        with self.assertRaises(ParseError):
            parse_guild_member(block, expected_id="演示成员")

    def test_member_expected_id_not_in_text_raises(self):
        """响应块中找不到目标玩家 → ParseError(可能是抓到了别人的面板)。"""
        block = ["演示公会", "别的玩家", "公会经验周贡献:",
                 "    今天: 0 公会经验"]
        with self.assertRaises(ParseError):
            parse_guild_member(block, expected_id="目标玩家")

    def test_member_invalid_expected_id_raises(self):
        """查询目标名本身含危险字符 → ParseError(防命令参数注入)。"""
        block = ["演示公会", "x", "公会经验周贡献:", "    今天: 0 公会经验"]
        with self.assertRaises(ParseError):
            parse_guild_member(block, expected_id="bad/name")


class TestParseGuildMemberHappyPath(unittest.TestCase):
    """成员面板正常解析(与真实样本一致)。"""

    def _block(self):
        return [
            "演示公会",
            "演示成员甲",
            "加入时间: 2026-08-07 19:34:16",
            "上次在线: 2026-08-12 18:40:19(2.14天前)",
            "公会经验周贡献:",
            "    今天           : 0 公会经验",
            "    2026-08-13: 0 公会经验",
            "    2026-08-12: 16 公会经验",
            "    2026-08-11: 0 公会经验",
            "    2026-08-10: 10 公会经验",
            "    2026-08-09: 0 公会经验",
            "    2026-08-08: 1,322 公会经验",
        ]

    def test_weekly_total_sums_all_rows(self):
        """周贡献 = 今天 + 全部日期行(含千分位)。"""
        r = parse_guild_member(self._block(), expected_id="演示成员甲")
        self.assertEqual(r["weekly_total"], 1348, "周贡献求和口径错误(应为 0+0+16+0+10+0+1322)")
        self.assertEqual(r["player"], "演示成员甲")
        self.assertEqual(r["guild_name"], "演示公会")

    def test_joined_at_extracted(self):
        r = parse_guild_member(self._block(), expected_id="演示成员甲")
        self.assertEqual(r["joined_at"], "2026-08-07 19:34:16")

    def test_last_online_excludes_parenthetical(self):
        """上次在线不应含括号内的"X天前"部分。"""
        r = parse_guild_member(self._block(), expected_id="演示成员甲")
        self.assertEqual(r["last_online"], "2026-08-12 18:40:19")

    def test_daily_includes_today_row(self):
        """明细应含"今天"行(业务规则 §4 要求计入)。"""
        r = parse_guild_member(self._block(), expected_id="演示成员甲")
        self.assertIn(("今天", 0), r["daily"])
        self.assertEqual(len(r["daily"]), 7, "应为 今天 + 6 个日期行")

    def test_missing_joined_at_returns_none(self):
        """缺"加入时间"字段 → None 而非抛异常(面板可能裁剪)。"""
        block = ["演示公会", "演示成员甲", "公会经验周贡献:", "    今天: 5 公会经验"]
        r = parse_guild_member(block, expected_id="演示成员甲")
        self.assertIsNone(r["joined_at"])
        self.assertIsNone(r["last_online"])


class TestParseGuildListStats(unittest.TestCase):
    """公会列表的统计行解析。"""

    def test_total_and_online_parsed(self):
        lines = ["演示公会", "-- 入门成员 --", "玩家甲 ●", "玩家乙 ●",
                 "成员总数: 200", "在线成员数: 43"]
        r = parse_guild_list(lines, RANK_NAMES)
        self.assertEqual(r["total"], 200)
        self.assertEqual(r["online"], 43)

    def test_duplicate_players_deduped(self):
        """同名玩家去重保序(面板异常时可能重复)。"""
        lines = ["演示公会", "-- 入门成员 --", "玩家甲 ●", "玩家乙 ●", "玩家甲 ●"]
        r = parse_guild_list(lines, RANK_NAMES)
        self.assertEqual(r["groups"]["entry"], ["玩家甲", "玩家乙"])

    def test_multiple_names_on_one_line(self):
        """一行多个 ● 分隔的玩家名必须全部拆出。"""
        lines = ["演示公会", "-- 入门成员 --", "甲 ● 乙 ● 丙 ●"]
        r = parse_guild_list(lines, RANK_NAMES)
        self.assertEqual(r["groups"]["entry"], ["甲", "乙", "丙"])

    def test_colon_variant_stats(self):
        """全角冒号也应能解析统计行。"""
        lines = ["演示公会", "-- 入门成员 --", "玩家甲 ●",
                 "成员总数：200", "在线成员数：43"]
        r = parse_guild_list(lines, RANK_NAMES)
        self.assertEqual(r["total"], 200)
        self.assertEqual(r["online"], 43)


if __name__ == "__main__":
    unittest.main(verbosity=2)