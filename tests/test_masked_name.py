# -*- coding: utf-8 -*-
"""
test_masked_name.py —— 网易屏蔽词(星号名)专项
============================================
真实事故:一次清人运行后,报告【未执行】3 人,其中 2 人的名字全是星号,
原因写着"查询重试耗尽"。根因不是网络抖动,是**本地校验在发命令之前**
就把网易屏蔽词产物(整串星号)当成了注入攻击。

网易客户端会把敏感词(玩家名/聊天/公会名)统一替换成一整串星号,
长度 3~704 不等,同一玩家不同次渲染长度都不同(`/guild list` 某一整段被抹平
时是 704 星;只遮单个玩家时是 8 星)。这是真实存在的合法 UI 显示。

本文件锚住三条边界,任何人改写解析器都会先在这里撞墙:

  A. 不能误伤:整串纯星号名必须能穿过 validate_player_name,
     否则一个 704 星字符串就能把整个花名册炸掉 —— 那一整段约 78 人
     连查询都轮不到。
  B. 安全边界不能松:带星号又带其它字符的混串(`*x*`/`a*b`)仍必须被拒。
     `*` 是 Windows 通配符,继续留在黑名单里挡 glob 注入;只有
     "整串纯星号"被单独放行。
  C. 破坏性操作不得建在猜测上:查询目标名是星号时**必然抛 ParseError**,
     即使 /guild member 面板里恰好也回显了同样长度的星号也不行。
     "谁"在这个面板上根本不可读,拿它的周贡献去升/降/踢就是猜 ——
     宁可漏一人,不可错踢一人。

所有数据均为合成值,不含任何真实玩家名/公会名/群号/本机路径。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logger  # noqa: E402
from actions import decide_actions  # noqa: E402
from parsers import (  # noqa: E402
    ParseError,
    is_masked_name,
    parse_guild_list,
    parse_guild_member,
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
    "high": "高活跃成员",
    "mid": "中等活跃成员",
    "low": "低活跃成员",
    "entry": "入门成员",
}

THRESHOLDS = {"mid_promote": 30000, "mid_demote": 5000,
              "entry_promote_1": 3500, "entry_kick": 3500}

# 真实样本里的两种长度:整段名单被抹平 → 704;单个玩家被遮 → 8
MASKED_704 = "*" * 704
MASKED_8 = "*" * 8


def _member_block(player="合成玩家甲", weekly_today=0):
    """构造一个 /guild member 面板块(玩家名行可换成星号)。"""
    return [
        "演示公会",
        player,
        "加入时间: 2026-08-15 10:26:58",
        "上次在线: 加载中(???)",
        "公会经验周贡献:",
        f"    今天           : {weekly_today} 公会经验",
        "    2026-10-09: 0 公会经验",
    ]


class TestIsMaskedName(unittest.TestCase):
    """识别器本身:只认"整串纯星号"。"""

    def test_pure_star_strings_are_masked(self):
        for s in ("*" * 3, "*" * 8, "*" * 68, MASKED_704):
            with self.subTest(length=len(s)):
                self.assertTrue(is_masked_name(s))

    def test_empty_and_none_are_not_masked(self):
        self.assertFalse(is_masked_name(""))
        self.assertFalse(is_masked_name(None))

    def test_mixed_star_names_are_not_masked(self):
        """带星号又带其它字符 → 不是屏蔽词产物,仍走黑名单。"""
        for s in ("*x*", "a*b", " * ", "* *", "x*", "**a"):
            with self.subTest(name=s):
                self.assertFalse(is_masked_name(s))


class TestMaskedNamePassesValidation(unittest.TestCase):
    """A. 屏蔽词名必须能穿过名单校验(否则整个花名册被一条 704 星炸掉)。"""

    def test_704_star_masked_name_accepted(self):
        """整段被遮成 704 星:不能因"超长"被拒。

        真实事故:704 星串触发了 MAX_NAME_LEN 校验,parse_guild_list 抛错
        → main 重发 3 次 /guild list 全炸 → 那一整段约 78 人连 targets
        都不是,连"查一次"都没发生。
        """
        self.assertEqual(validate_player_name(MASKED_704), MASKED_704)

    def test_8_star_masked_name_accepted(self):
        """单个玩家被遮成 8 星:不能因"含非法字符 *"被拒。"""
        self.assertEqual(validate_player_name(MASKED_8), MASKED_8)

    def test_empty_still_rejected(self):
        """空名仍必须拒绝(放行屏蔽名不等于放行空名)。"""
        for bad in ("", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ParseError):
                    validate_player_name(bad)

    def test_overlong_real_name_still_rejected(self):
        """真名字仍守 48 上限 —— 放行的只是"整串纯星号"这一个形态。"""
        with self.assertRaises(ParseError):
            validate_player_name("x" * 49)


class TestMixedStarNamesStillRejected(unittest.TestCase):
    """B. 安全边界不能松:混串仍被黑名单拒绝(glob 注入防线)。"""

    def test_mixed_star_names_rejected(self):
        for bad in ("*x*", "a*b", " * ", "* *"):
            with self.subTest(bad=bad):
                with self.assertRaises(ParseError):
                    validate_player_name(bad)

    def test_other_dangerous_chars_still_rejected(self):
        for bad in ("a/b", "a\\b", "a:b", "a●b", "{player}", "a\nb", "a\tb"):
            with self.subTest(bad=bad):
                with self.assertRaises(ParseError):
                    validate_player_name(bad)

    def test_real_names_with_spaces_unaffected(self):
        """真实样本存在"带空格 名字",放行屏蔽名不得波及它们。"""
        for good in ("暴躁的星球跳芭蕾", "_络绎不绝", "灰________灰", "123463278"):
            with self.subTest(good=good):
                self.assertEqual(validate_player_name(good), good)


class TestMaskedNameInGuildList(unittest.TestCase):
    """A 的落地点:整段被遮的名单必须能被解析出来,而不是炸掉整个面板。"""

    def test_whole_segment_masked_to_704_stars(self):
        """真实形态:"中等活跃成员"整段被抹成一行 704 星(一个 ● 都没有)。"""
        lines = [
            "演示公会",
            "-- 高活跃成员 --",
            "演示高活跃甲 ● 演示高活跃乙 ●",
            "-- 中等活跃成员 --",
            MASKED_704,
            "-- 低活跃成员 --",
            "演示低活跃甲 ●",
            "成员总数: 4",
            "在线成员数: 2",
        ]
        r = parse_guild_list(lines, RANK_NAMES)
        self.assertEqual(r["groups"]["high"], ["演示高活跃甲", "演示高活跃乙"])
        self.assertEqual(r["groups"]["mid"], [MASKED_704],
                         "整段被遮的名单必须保留为一条星号条目")
        self.assertEqual(r["groups"]["low"], ["演示低活跃甲"])

    def test_single_masked_player_among_real_names(self):
        """真实形态:同一段里只有第 20 个条目是 8 星,前后都是正常 ● 分隔。"""
        lines = [
            "演示公会",
            "-- 中等活跃成员 --",
            "天越awa ● " + MASKED_8 + " ● Ao_yuzhi ●",
            "成员总数: 3",
            "在线成员数: 1",
        ]
        r = parse_guild_list(lines, RANK_NAMES)
        self.assertEqual(r["groups"]["mid"], ["天越awa", MASKED_8, "Ao_yuzhi"])
        self.assertEqual(len(r["groups"]["mid"]), 3,
                         "屏蔽条目把相邻的真名字吞掉了")

    def test_masked_segment_does_not_swallow_group_title(self):
        """704 星行不得把下一个分组标题也吞进名字里。"""
        lines = [
            "演示公会",
            "-- 中等活跃成员 --",
            MASKED_704,
            "-- 低活跃成员 --",
            "演示低活跃甲 ●",
            "成员总数: 2",
            "在线成员数: 1",
        ]
        r = parse_guild_list(lines, RANK_NAMES)
        self.assertEqual(r["groups"]["low"], ["演示低活跃甲"])
        self.assertNotIn("--", "".join(r["groups"]["mid"]))


class TestMaskedNameNeverYieldsRecord(unittest.TestCase):
    """C. 破坏性操作不得建在猜测上:查询目标名是星号 → 必然 ParseError。"""

    def test_masked_expected_id_with_normal_panel(self):
        with self.assertRaises(ParseError):
            parse_guild_member(_member_block("合成玩家甲"), expected_id=MASKED_8)

    def test_masked_expected_id_with_704(self):
        with self.assertRaises(ParseError):
            parse_guild_member(_member_block("合成玩家甲"), expected_id=MASKED_704)

    def test_masked_expected_id_with_reject_echo(self):
        """真实回显:查询被服务端拒绝,面板只有"该玩家不在公会中!"一行。"""
        with self.assertRaises(ParseError):
            parse_guild_member(["该玩家不在公会中!"], expected_id=MASKED_8)

    def test_masked_expected_id_when_panel_row_also_masked(self):
        """最危险的一条:面板里玩家名行也恰好被遮成**同样长度**的星号。

        此时"expected_id 出现在面板里"的存在性检查会误判为"面板证实了此人存在",
        进而返回一个带周贡献的记录 → 上游 decide_actions 可能对它下达
        降职/踢出指令。网易的星号长度随渲染上下文浮动,同长度并非不可能。
        这里必须抛错,宁可漏一人,不可错踢一人。
        """
        panel = _member_block(player=MASKED_8, weekly_today=0)
        with self.assertRaises(ParseError):
            parse_guild_member(panel, expected_id=MASKED_8)

    def test_normal_player_with_masked_name_row_still_parses(self):
        """反向护栏:真实名查询,面板玩家名行被遮成 8 星 —— 面板本身可读,
        不应因为面板被遮就拒绝(存在的星号串与真实名无关,存在性检查本就
        不会命中真实名,仍按"未找到目标玩家"失败)。"""
        with self.assertRaises(ParseError):
            parse_guild_member(_member_block(player=MASKED_8),
                               expected_id="合成玩家甲")

    def test_masked_player_cannot_reach_kick_decision(self):
        """端到端护栏:屏蔽名玩家永远拿不到可判定的记录。

        直接锁死"星号名 → decide_actions → kick"这条链:解析层抛错后,
        主流程拿不到 record,判定根本不会被调用。
        """
        panel = _member_block(player=MASKED_8, weekly_today=0)
        with self.assertRaises(ParseError):
            parse_guild_member(panel, expected_id=MASKED_8)
        # 若哪天解析层被改松了,这一行就让 kick 落在这个不可读的名字上
        # (阈值用真实默认值:entry < 3500 → 踢出)
        actions, _ = decide_actions("entry", 0, THRESHOLDS)
        self.assertEqual(actions, ["kick"],
                         "test 自身的阈值假设变了?这条只是把危险链显式化")


if __name__ == "__main__":
    unittest.main(verbosity=2)
