# -*- coding: utf-8 -*-
"""
test_vision_tooltip.py —— 三类 tooltip 解析与语义分类
=====================================================
只按**文字**判定语义:材质包改变图标外观但 tooltip 文字与标题不变。
覆盖:六字段齐全 / 缺字段→"不可读"(绝不当 0) / 千分位 / 7 日明细 /
`人数: [280/280]` → 280 / 组队钻石被排除 / 翻页与升降踢按文字识别。
"""

import unittest

from guild_auto.vision.models import UNREADABLE
from guild_auto.vision.tooltip import (
    KIND_BACK,
    KIND_CANCEL,
    KIND_CONFIRM,
    KIND_DEMOTE,
    KIND_GUILD_DETAIL,
    KIND_KICK,
    KIND_MEMBER,
    KIND_NEXT_PAGE,
    KIND_PARTY,
    KIND_PREV_PAGE,
    KIND_PROMOTE,
    KIND_UNKNOWN,
    TITLE_CONFIRM,
    TITLE_GUILD,
    TITLE_MEMBER_DETAIL,
    TITLE_SETTINGS,
    TITLE_UNKNOWN,
    classify_element,
    classify_title,
    parse_guild_detail,
    parse_member_tooltip,
)

MEMBER_TOOLTIP = """\
某某玩家
在线状态: ●
公会身份: 中等活跃成员
首次加入: 2026-09-26 21:38:03
上次在线: 2026-10-25 19:55:27(2.75天前)
公会经验周贡献: 6,799 公会经验
    今天          : 0 公会经验
    2026-10-27: 0 公会经验
    2026-10-26: 0 公会经验
    2026-10-25: 564 公会经验
    2026-10-24: 1,310 公会经验
    2026-10-23: 4,925 公会经验
    2026-10-22: 0 公会经验
minecraft:player_head
"""

GUILD_DETAIL_TOOLTIP = """\
公会详情
名称: 某公会
会长: 某会长
描述: 某描述
标签: [某标签]
人数: [280/280]
今日经验: 28,975
minecraft:book
"""


class TestParseMemberTooltip(unittest.TestCase):
    def test_six_fields_complete(self):
        member = parse_member_tooltip(MEMBER_TOOLTIP, online=True)
        self.assertEqual(member.player, "某某玩家")
        self.assertIs(member.online, True)
        self.assertEqual(member.rank, "中等活跃成员")
        self.assertEqual(member.joined_at, "2026-09-26 21:38:03")
        self.assertEqual(member.last_online, "2026-10-25 19:55:27")
        self.assertEqual(member.last_online_relative, "2.75天前")
        self.assertEqual(member.weekly_total, 6799)
        self.assertEqual(member.unreadable_fields(), [])

    def test_thousands_separator(self):
        member = parse_member_tooltip(MEMBER_TOOLTIP, online=False)
        self.assertEqual(member.weekly_total, 6799)

    def test_seven_daily_rows(self):
        member = parse_member_tooltip(MEMBER_TOOLTIP, online=True)
        self.assertEqual(len(member.daily), 7)
        self.assertTrue(member.daily_complete)
        self.assertEqual(member.daily[0], ("今天", 0))
        self.assertIn(("2026-10-24", 1310), member.daily)

    def test_item_id_line_not_treated_as_player(self):
        member = parse_member_tooltip(MEMBER_TOOLTIP, online=True)
        self.assertNotEqual(member.player, "minecraft:player_head")

    def test_missing_weekly_is_unreadable_not_zero(self):
        """缺周贡献行必须标「不可读」——把它当 0 会直接导致误踢。"""
        text = MEMBER_TOOLTIP.replace("公会经验周贡献: 6,799 公会经验", "")
        member = parse_member_tooltip(text, online=True)
        self.assertEqual(member.weekly_total, UNREADABLE)
        self.assertNotEqual(member.weekly_total, 0)
        self.assertIn("weekly_total", member.unreadable_fields())

    def test_missing_joined_and_last_online_are_unreadable(self):
        text = MEMBER_TOOLTIP.replace("首次加入: 2026-09-26 21:38:03", "")
        text = text.replace("上次在线: 2026-10-25 19:55:27(2.75天前)", "")
        member = parse_member_tooltip(text, online=True)
        self.assertEqual(member.joined_at, UNREADABLE)
        self.assertEqual(member.last_online, UNREADABLE)
        self.assertEqual(member.last_online_relative, UNREADABLE)

    def test_missing_daily_rows_marked_incomplete(self):
        """每日明细不足 7 行 → daily 不完整,记入不可读清单。"""
        marker = "    2026-10-22: 0 公会经验\n"
        member = parse_member_tooltip(MEMBER_TOOLTIP.replace(marker, ""), online=True)
        self.assertEqual(len(member.daily), 6)
        self.assertFalse(member.daily_complete)
        self.assertIn("daily", member.unreadable_fields())

    def test_online_requires_pixel_sampling(self):
        """OCR 只读到"●",没有像素采样就不能猜在线/离线。"""
        member = parse_member_tooltip(MEMBER_TOOLTIP)
        self.assertEqual(member.online, UNREADABLE)
        self.assertIn("online", member.unreadable_fields())

    def test_masked_player_name_is_kept(self):
        """屏蔽词玩家名字是整串星号,工具层照读,不因屏蔽放弃该行。"""
        text = MEMBER_TOOLTIP.replace("某某玩家", "********")
        member = parse_member_tooltip(text, online=True)
        self.assertEqual(member.player, "********")
        self.assertEqual(member.weekly_total, 6799)

    def test_full_width_parentheses_and_colon(self):
        text = MEMBER_TOOLTIP.replace("(2.75天前)", "（2.75天前）")
        text = text.replace("公会身份: ", "公会身份：")
        member = parse_member_tooltip(text, online=True)
        self.assertEqual(member.last_online_relative, "2.75天前")
        self.assertEqual(member.rank, "中等活跃成员")

    def test_record_shape_matches_command_mode(self):
        member = parse_member_tooltip(MEMBER_TOOLTIP, online=True)
        record = member.as_record()
        for key in ("player", "rank", "joined_at", "last_online", "weekly_total", "daily"):
            self.assertIn(key, record)
        self.assertIs(record["online"], True)
        self.assertEqual(record["source"], "ui")
        self.assertNotIn("unreadable", record)

    def test_record_keeps_unreadable_sentinel(self):
        text = MEMBER_TOOLTIP.replace("公会经验周贡献: 6,799 公会经验", "")
        record = parse_member_tooltip(text, online=True).as_record()
        self.assertEqual(record["weekly_total"], UNREADABLE)
        self.assertIn("weekly_total", record["unreadable"])


class TestParseGuildDetail(unittest.TestCase):
    def test_member_total_from_count_line(self):
        self.assertEqual(parse_guild_detail(GUILD_DETAIL_TOOLTIP), 280)

    def test_capacity_is_ignored(self):
        self.assertEqual(parse_guild_detail("人数: [17/280]"), 17)

    def test_full_width_brackets(self):
        self.assertEqual(parse_guild_detail("人数: ［42/100］"), 42)

    def test_missing_returns_none_not_zero(self):
        self.assertIsNone(parse_guild_detail("公会详情\n名称: 某公会"))
        self.assertIsNone(parse_guild_detail(""))


class TestClassifyTitle(unittest.TestCase):
    def test_known_titles(self):
        self.assertEqual(classify_title("装扮与个人设定"), TITLE_SETTINGS)
        self.assertEqual(classify_title("公会系统"), TITLE_GUILD)
        self.assertEqual(classify_title("确认界面"), TITLE_CONFIRM)

    def test_player_name_is_member_detail(self):
        self.assertEqual(classify_title("某某玩家"), TITLE_MEMBER_DETAIL)

    def test_masked_player_title_is_member_detail(self):
        self.assertEqual(classify_title("********"), TITLE_MEMBER_DETAIL)

    def test_empty_or_other_ui_is_unknown(self):
        self.assertEqual(classify_title(""), TITLE_UNKNOWN)
        self.assertEqual(classify_title(None), TITLE_UNKNOWN)
        self.assertEqual(classify_title("聊天设置界面"), TITLE_UNKNOWN)


class TestClassifyElement(unittest.TestCase):
    """元素语义只按 tooltip 文字判断,禁止按图标/像素。"""

    def test_member(self):
        self.assertEqual(classify_element(MEMBER_TOOLTIP), KIND_MEMBER)

    def test_guild_detail(self):
        self.assertEqual(classify_element(GUILD_DETAIL_TOOLTIP), KIND_GUILD_DETAIL)

    def test_paging_and_back(self):
        self.assertEqual(classify_element("上一页"), KIND_PREV_PAGE)
        self.assertEqual(classify_element("下一页"), KIND_NEXT_PAGE)
        self.assertEqual(classify_element("返回公会界面"), KIND_BACK)

    def test_promote_demote_kick(self):
        self.assertEqual(classify_element("升职\n升职玩家的公会职位"), KIND_PROMOTE)
        self.assertEqual(classify_element("降职\n降职玩家的公会职位"), KIND_DEMOTE)
        self.assertEqual(
            classify_element("踢出某某玩家\n所有操作将会被记录\n请谨慎操作！"), KIND_KICK
        )

    def test_confirm_is_not_misread_as_kick(self):
        """确认界面 tooltip 同时含"踢出",必须先判确认,否则会误点。"""
        text = "确认\n请确定您要踢出此用户\n点击确认"
        self.assertEqual(classify_element(text), KIND_CONFIRM)

    def test_cancel(self):
        self.assertEqual(classify_element("返回\n点击返回"), KIND_CANCEL)

    def test_party_diamond_excluded(self):
        """组队钻石是无关物品:识别为 party,绝不当成员或执行项。"""
        text = "发起组队\n邀请某某玩家一起组队\nminecraft:diamond"
        self.assertEqual(classify_element(text), KIND_PARTY)

    def test_unknown(self):
        self.assertEqual(classify_element(""), KIND_UNKNOWN)
        self.assertEqual(classify_element("一堆无关文字"), KIND_UNKNOWN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
