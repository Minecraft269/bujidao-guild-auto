# -*- coding: utf-8 -*-
"""
test_parsers.py —— 解析器单元测试
==================================
运行:cd guild_auto_v2 && python -m unittest tests.test_parsers -v
覆盖:strip_chat_prefix(两种日志格式)、parse_guild_list(分组/●/多行名/统计)、
      parse_guild_member(千分位/今天/字段缺失/玩家校验)、parse_weekly_exp。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parsers import (  # noqa: E402
    ParseError,
    parse_guild_list,
    parse_guild_member,
    parse_weekly_exp,
    strip_chat_prefix,
)

SAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")

RANK_NAMES = {
    "leader": "会长",
    "admin": "管理员",
    "high": "高活跃成员",
    "mid": "中等活跃成员",
    "low": "低活跃成员",
    "entry": "入门成员",
}


def read_sample(name):
    with open(os.path.join(SAMPLES, name), encoding="utf-8") as f:
        return f.read()


def to_block_lines(text):
    """把单条 [CHAT] 消息日志文本转为块内容行(模拟 log_watcher.wait_for_chat_block 结果)。
    真实格式(网易版):整条面板为单物理行,内部换行以字面 "\\n" 写入,结尾线可能带 " [C]" 标记;
    也兼容标准 Java 版的物理多行格式。"""
    from log_watcher import is_block_end_line, is_block_start_line
    idx = text.find("[CHAT]")
    content = text[idx + len("[CHAT]"):] if idx >= 0 else text
    content = content.replace("\\n", "\n")  # 字面 \n → 真实换行(单行格式)
    block = []
    started = False
    for raw in content.splitlines():
        line = raw.strip()
        if line.endswith(" [C]"):
            line = line[:-4].strip()
        elif line.endswith("[C]"):
            line = line[:-3].strip()
        if not started:
            if is_block_start_line(line):
                started = True
            continue
        if is_block_end_line(line):
            break
        block.append(line)  # 保留空行:解析器多行名字拼接依赖空行终止
    return block


class TestStripChatPrefix(unittest.TestCase):
    def test_standard_format(self):
        line = "[13:52:03] [Server thread/INFO]: [CHAT] hello"
        self.assertEqual(strip_chat_prefix(line), "hello")

    def test_netease_format(self):
        line = ("[158?2026 00:23:40.454] [Render thread/INFO] "
                "[net.minecraft.client.gui.components.ChatComponent/]: [CHAT] 消息内容")
        self.assertEqual(strip_chat_prefix(line), "消息内容")

    def test_netease_copy_marker(self):
        line = ("[158?2026 00:23:40.454] [Render thread/INFO] "
                "[net.minecraft.client.gui.components.ChatComponent/]: [CHAT] 某人 > 你好. [C]")
        self.assertEqual(strip_chat_prefix(line), "某人 > 你好.")

    def test_no_chat_prefix(self):
        self.assertEqual(strip_chat_prefix("普通行"), "普通行")


class TestParseGuildList(unittest.TestCase):
    def setUp(self):
        self.lines = to_block_lines(read_sample("guild_list_sample.txt"))

    def test_guild_name(self):
        info = parse_guild_list(self.lines, RANK_NAMES)
        self.assertEqual(info["guild_name"], "演示公会")

    def test_groups_and_counts(self):
        info = parse_guild_list(self.lines, RANK_NAMES)
        self.assertEqual(info["total"], 200)
        self.assertEqual(info["online"], 43)
        self.assertEqual(info["groups"]["leader"], ["GuildLeader_01"])
        self.assertEqual(info["groups"]["admin"],
                         ["ViceAdmin_02", "演示玩家甲", "演示玩家乙", "演示玩家丙"])
        self.assertEqual(info["groups"]["high"], ["ActivePlayer_A", "演示玩家丁", "ActivePlayer_B"])
        self.assertEqual(info["groups"]["mid"], ["MidPlayer_A", "MidPlayer_B", "演示玩家戊"])
        self.assertEqual(info["groups"]["low"], ["LowPlayer_A", "LowPlayer_B", "演示玩家己"])

    def test_multiline_player_name(self):
        """名字被截断成两行:上行不以 ● 结尾,应拼接下行。"""
        info = parse_guild_list(self.lines, RANK_NAMES)
        entry = info["groups"]["entry"]
        self.assertIn("一个超长的演示玩家名字被游戏截断成两行", entry)
        self.assertIn("正常名_demo", entry)
        self.assertIn("带空格 演示名", entry)  # 名字含空格

    def test_dedup(self):
        info = parse_guild_list(self.lines, RANK_NAMES)
        for ids in info["groups"].values():
            self.assertEqual(len(ids), len(set(ids)), f"分组存在重复ID: {ids}")

    def test_empty_raises(self):
        with self.assertRaises(ParseError):
            parse_guild_list([], RANK_NAMES)


class TestParseGuildMember(unittest.TestCase):
    def setUp(self):
        self.lines = to_block_lines(read_sample("guild_member_sample.txt"))

    def test_weekly_total_with_thousands(self):
        """1,322 + 16 + 10 + 0*5(今天与三天0) = 1348。"""
        info = parse_guild_member(self.lines, expected_id="演示成员甲")
        self.assertEqual(info["weekly_total"], 1348)
        self.assertEqual(info["guild_name"], "演示公会")
        self.assertEqual(info["player"], "演示成员甲")

    def test_joined_and_last_online(self):
        info = parse_guild_member(self.lines, expected_id="演示成员甲")
        self.assertEqual(info["joined_at"], "2026-08-07 19:34:16")
        self.assertEqual(info["last_online"], "2026-08-12 18:40:19")

    def test_daily_detail(self):
        info = parse_guild_member(self.lines, expected_id="演示成员甲")
        daily = dict(info["daily"])
        self.assertEqual(daily.get("2026-08-08"), 1322)
        self.assertEqual(daily.get("今天"), 0)
        self.assertEqual(daily.get("2026-08-12"), 16)

    def test_expected_id_mismatch_raises(self):
        with self.assertRaises(ParseError):
            parse_guild_member(self.lines, expected_id="不存在的人")

    def test_short_output_raises(self):
        with self.assertRaises(ParseError):
            parse_guild_member(["只有一行"], expected_id="x")


class TestWeeklyExp(unittest.TestCase):
    def test_parse_weekly_exp_scope(self):
        lines = [
            "公会经验周贡献:",
            "    今天           : 5 公会经验",
            "    2026-08-08: 1,322 公会经验",
            "    2026-08-07: 0 公会经验",
        ]
        total, daily = parse_weekly_exp(lines)
        self.assertEqual(total, 1327)
        self.assertEqual(len(daily), 3)

    def test_ignores_lines_outside_weekly_section(self):
        lines = [
            "加入时间: 2026-08-07 19:34:16",
            "公会经验周贡献:",
            "    今天           : 0 公会经验",
            "    2026-08-08: 100 公会经验",
        ]
        total, _ = parse_weekly_exp(lines)
        self.assertEqual(total, 100)


if __name__ == "__main__":
    unittest.main()
