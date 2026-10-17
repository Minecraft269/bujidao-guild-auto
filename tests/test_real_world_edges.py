# -*- coding: utf-8 -*-
"""
test_real_world_edges.py —— 真实日志跑出来的边界情况
====================================================
本文件的用例**全部来自一次真实清人运行**的日志形态，不是凭空构造的假设。

真实素材来源（本地文件，**不入库**）：一份远古版脚本在网易版 MC 上的完整运行输出，
191 名玩家，收尾行「完成!查询 188 人,失败 3 人,升职 43 人,降职 38 人,踢出 34 人」。

从中提取到三类**真实存在且现有测试未覆盖**的形态：

  1. `上次在线: 加载中` —— 高频出现（抽样 25 条里过半），服务端对"上次在线"
     懒加载。现有测试只测了"正常时间"和"字段缺失"两种。
     风险：`last_online` 是**未定义值**，下游若拿它做时间计算会把"加载中"
     当时间解析而崩溃或误判。本模块虽然只用它展示，但契约必须写明。

  2. 玩家名被网易屏蔽成 `*`（星号串）—— 见 test_masked_name.py 专项。
     真实事故：报告里 【未执行】3 人，其中 2 个名字全是星号，
     原因"查询重试耗尽"。

  3. 奇形玩家名 —— 真实名库存在含 `乄` `彡` `罒` `灬` 等 Unicode 装饰字符、
     整段下划线填充、纯数字、中英混排带下划线后缀的名字。
     黑名单校验不能误杀这些。

本文件所有测试数据均为**合成名**，不含任何真实玩家名/公会名/群号/本机路径。
"""

import os
import sys
import unittest


from guild_auto.infrastructure import logger  # noqa: E402
from guild_auto.domain.parsers import (  # noqa: E402
    ParseError,
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
    "high": "高活跃",
    "mid": "中等活跃",
    "low": "低活跃",
    "entry": "入门成员",
}


def _member_block(last_online_line=None, joined="2026-08-15 10:26:58",
                  player="合成玩家甲"):
    """构造一个 /guild member 面板块。

    last_online_line 为 None 时省略该行（测"字段缺失"）；
    传入字符串则原样写入（测各种真实值，如"加载中"、正常时间）。
    """
    lines = [
        "演示公会",
        player,
        f"加入时间: {joined}",
    ]
    if last_online_line is not None:
        lines.append(f"上次在线: {last_online_line}")
    lines += [
        "公会经验周贡献:",
        "    今天           : 0 公会经验",
        "    2026-08-12: 16 公会经验",
        "    2026-08-11: 0 公会经验",
    ]
    return lines


class TestLastOnlineLoadingState(unittest.TestCase):
    """真实高频形态:`上次在线: 加载中`。

    契约要求:解析器**必须稳定**,不得让"加载中"污染周贡献计算。
    阈值判定只认 weekly_total,与 last_online 无关 —— 这条必须守住。
    """

    def test_loading_state_does_not_break_parse(self):
        """'加载中' 不得导致解析失败 —— 真实运行里过半玩家会遇到。"""
        r = parse_guild_member(_member_block("加载中"), expected_id="合成玩家甲")
        self.assertEqual(r["weekly_total"], 16, "'加载中' 影响了周贡献计算")

    def test_loading_state_preserved_as_is(self):
        """'加载中' 原样保留(不伪装成时间) —— 下游才能识别并区别对待。"""
        r = parse_guild_member(_member_block("加载中"), expected_id="合成玩家甲")
        self.assertIsNotNone(r["last_online"])
        self.assertIn("加载中", r["last_online"],
                      "'加载中' 被吞掉了 —— 下游会误以为没有这个字段")

    def test_loading_state_does_not_affect_numbers(self):
        """关键:'加载中' 与正常时间算出的周贡献/明细必须一致。"""
        loading = parse_guild_member(_member_block("加载中"), expected_id="合成玩家甲")
        normal = parse_guild_member(_member_block("2026-10-07 13:55:16"),
                                    expected_id="合成玩家甲")
        self.assertEqual(loading["weekly_total"], normal["weekly_total"])
        self.assertEqual(loading["daily"], normal["daily"],
                         "last_online 的形态差异影响了日贡献明细")

    def test_loading_state_with_paren_suffix(self):
        """真实形态里 '加载中' 也可能带括号后缀。"""
        r = parse_guild_member(_member_block("加载中(未同步)"), expected_id="合成玩家甲")
        self.assertEqual(r["weekly_total"], 16)

    def test_other_placeholder_words(self):
        """真实日志里还见过 '???公告' / '未知' / 'ERROR' 等占位文案,
        都不得让解析崩掉或影响数值。"""
        for placeholder in ("???公告", "未知", "加载中...", "ERROR"):
            with self.subTest(placeholder=placeholder):
                r = parse_guild_member(_member_block(placeholder), expected_id="合成玩家甲")
                self.assertEqual(r["weekly_total"], 16,
                                 f"占位文案 {placeholder!r} 影响了周贡献")

    def test_joined_at_placeholder_state(self):
        """真实运行里 '加入时间' 始终正常,但如果它也变成占位值,
        周贡献计算同样不能受影响(加入时间不参与判定)。"""
        r = parse_guild_member(_member_block(last_online_line="加载中",
                                             joined="加载中"),
                               expected_id="合成玩家甲")
        self.assertEqual(r["weekly_total"], 16)


class TestExoticRealNames(unittest.TestCase):
    """真实名库里的奇形名字:黑名单校验不能误杀。

    以下形态全部来自真实名库(此处用合成名复现形态):
      - 装饰性 Unicode 字符(乄 彡 罒 灬 等)
      - 整段下划线填充
      - 纯数字
      - 中英混排带下划线后缀
    """

    def test_cjk_decorative_chars_accepted(self):
        for name in ("演示乄浅听雨", "静轩彡", "风尘彡叹流年罒", "花缘灬遗忘"):
            with self.subTest(name=name):
                self.assertEqual(validate_player_name(name), name)

    def test_underscore_filled_mid_name(self):
        """名字中间整段下划线(真实形态:灰________灰)。"""
        self.assertEqual(validate_player_name("灰________灰"), "灰________灰")

    def test_numeric_only_name(self):
        """纯数字名(真实形态:123463278)。"""
        self.assertEqual(validate_player_name("123463278"), "123463278")

    def test_cjk_ascii_mixed_with_suffix(self):
        """中英混排带下划线后缀(真实形态:抖音梦幻awa_Mc)。"""
        self.assertEqual(validate_player_name("抖音梦幻awa_Mc"), "抖音梦幻awa_Mc")

    def test_long_name_within_limit(self):
        """真实名库里有较长的名字,不能因为"看起来太长"被拒。"""
        name = "机灵的兔子在奔跑十二三四五六七八九十"
        self.assertLessEqual(len(name), 48)
        self.assertEqual(validate_player_name(name), name)

    def test_dangerous_names_still_rejected(self):
        """反向护栏:支持奇形名字不等于放松安全校验。

        危险字符仍必须拒绝 —— 否则伪造面板可注入幽灵条目 / 命令参数错位 /
        报告文件名穿越。这条确保上面那些放行没有把安全边界一起放开。
        """
        for bad in ("a/b", "a\\b", "a\nb", "a\x00b", "{player}", "a●b", "a:b", "x" * 49):
            with self.subTest(bad=bad):
                with self.assertRaises(ParseError):
                    validate_player_name(bad)


class TestMixedGuildList(unittest.TestCase):
    """混合面板:同一批里既有奇形玩家也有正常玩家,互不影响。"""

    def test_mixed_panel_all_players_parsed(self):
        """一个面板块里混有多种形态,全部玩家都要被解析出来(不被漏掉)。"""
        lines = [
            "演示公会",
            "-- 高活跃 --",
            "正常玩家甲 ● 装饰乄玩家 ● 灰_灰 ●",
            "-- 中等活跃 --",
            "抖音梦幻awa_Mc ● 123463278 ●",
            "成员总数: 200",
            "在线成员数: 43",
        ]
        r = parse_guild_list(lines, RANK_NAMES)
        self.assertEqual(r["total"], 200)
        self.assertEqual(r["online"], 43)
        self.assertIn("正常玩家甲", r["groups"]["high"])
        self.assertIn("装饰乄玩家", r["groups"]["high"])
        self.assertIn("灰_灰", r["groups"]["high"])
        self.assertIn("抖音梦幻awa_Mc", r["groups"]["mid"])
        self.assertIn("123463278", r["groups"]["mid"])

    def test_each_exotic_player_queryable(self):
        """混合面板里每个玩家都要能被单独查询并算出周贡献。

        覆盖真实场景:一个 191 人的公会里各种奇形名字混在一起,
        任何一个解析失败都会让该玩家被放弃(真实事故就是这样丢掉 3 人)。
        """
        exotic_names = ["装饰乄玩家", "灰________灰", "抖音梦幻awa_Mc", "123463278"]
        for name in exotic_names:
            with self.subTest(player=name):
                validate_player_name(name)      # 先过名单校验
                r = parse_guild_member(_member_block("加载中", player=name),
                                       expected_id=name)
                self.assertEqual(r["player"], name)
                self.assertEqual(r["weekly_total"], 16,
                                 f"{name} 的周贡献没算出来 → 该玩家会被放弃")


if __name__ == "__main__":
    unittest.main(verbosity=2)
