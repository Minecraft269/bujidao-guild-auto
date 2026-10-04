# -*- coding: utf-8 -*-
"""
test_actions.py —— 业务判定单元测试
====================================
运行:cd guild_auto_v2 && python -m unittest tests.test_actions -v
覆盖:decide_actions 阈值矩阵(含边界语义,与原脚本逐字一致)、new_rank_after 等级链、
      build_notification 四类文案(继承原脚本)、build_command 命令格式。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from actions import (  # noqa: E402
    build_command,
    build_notification,
    decide_actions,
    new_rank_after,
)

# 继承自原脚本的默认阈值(与 config.DEFAULT_CONFIG 一致)
T = {
    "high_demote": 30000,
    "mid_promote": 30000,
    "mid_demote": 5000,
    "low_promote_2": 30000,
    "low_promote_1": 10000,
    "low_demote": 3500,
    "entry_promote_3": 30000,
    "entry_promote_2": 10000,
    "entry_promote_1": 3500,
    "entry_kick": 3500,
}

CFG = {
    "guild_qq": "YOUR_QQ_GROUP",
    "notify_templates": {
        "none": "{player} 这周贡献为:{weekly} 执行操作:无 非常感谢你对公会做出的贡献 公会群:{guild_qq}",
        "promote": "{player} 这周贡献为:{weekly} 执行操作:升职({count}次) 非常感谢你对公会做出的贡献 公会群:{guild_qq}",
        "demote": "{player} 这周贡献为:{weekly} 执行操作:降职 若有异议请去群里找执行此操作的管理员 公会群:{guild_qq}",
        "kick": "{player} 这周贡献为:{weekly} 执行操作:踢出 未完成这周最低标准 若有异议请去群里寻找执行管理员 公会群:{guild_qq}",
    },
    "commands": {
        "guild_member": "/guild member {player}",
        "guild_promote": "/guild promote {player}",
        "guild_demote": "/guild demote {player}",
        "guild_kick": "/guild kick {player} {reason}",
        "kick_reason": "未完成一周最低标准",
        "gc_prefix": "/gc",
    },
}


class TestDecideActions(unittest.TestCase):
    """阈值矩阵与边界语义(对照原脚本 run_auto/mid/low/entry.py)。"""

    # ---- 高活跃(run_auto.py:80: exp <= 30000 → demote) ----
    def test_high_demote_at_boundary(self):
        self.assertEqual(decide_actions("high", 30000, T)[0], ["demote"])  # 含边界
        self.assertEqual(decide_actions("high", 30001, T)[0], [])

    def test_high_no_action(self):
        self.assertEqual(decide_actions("high", 50000, T)[0], [])

    # ---- 中等活跃(run_mid.py:81-84) ----
    def test_mid_promote_at_boundary(self):
        self.assertEqual(decide_actions("mid", 30000, T)[0], ["promote"])  # 含边界
        self.assertEqual(decide_actions("mid", 29999, T)[0], [])

    def test_mid_demote_strict_less(self):
        self.assertEqual(decide_actions("mid", 4999, T)[0], ["demote"])
        self.assertEqual(decide_actions("mid", 5000, T)[0], [])  # 5000 不降(原脚本 <5000)

    # ---- 低活跃(run_low.py:79-84) ----
    def test_low_promote_2(self):
        self.assertEqual(decide_actions("low", 30000, T)[0], ["promote", "promote"])
        self.assertEqual(decide_actions("low", 29999, T)[0], ["promote"])

    def test_low_promote_1(self):
        self.assertEqual(decide_actions("low", 10000, T)[0], ["promote"])  # 含边界

    def test_low_demote_strict_less(self):
        self.assertEqual(decide_actions("low", 3499, T)[0], ["demote"])
        self.assertEqual(decide_actions("low", 3500, T)[0], [])  # 3500 保持

    # ---- 入门成员(run_entry.py:79-86) ----
    def test_entry_promote_3(self):
        self.assertEqual(decide_actions("entry", 30000, T)[0], ["promote"] * 3)

    def test_entry_promote_2(self):
        self.assertEqual(decide_actions("entry", 10000, T)[0], ["promote"] * 2)

    def test_entry_promote_1_at_boundary(self):
        self.assertEqual(decide_actions("entry", 3500, T)[0], ["promote"])  # 3500 升职
        self.assertEqual(decide_actions("entry", 3499, T)[0], ["kick"])  # 3499 踢出

    def test_entry_trigger_key(self):
        _, trigger = decide_actions("entry", 3499, T)
        self.assertEqual(trigger, "entry_kick")
        _, trigger = decide_actions("high", 100, T)
        self.assertEqual(trigger, "high_demote")

    def test_no_action_trigger_none(self):
        _, trigger = decide_actions("mid", 10000, T)
        self.assertIsNone(trigger)


class TestNewRankAfter(unittest.TestCase):
    """等级链:entry < low < mid < high(原脚本 入门→低→中→高)。"""

    def test_promote_counts(self):
        self.assertEqual(new_rank_after("entry", ["promote"]), "low")
        self.assertEqual(new_rank_after("entry", ["promote", "promote"]), "mid")
        self.assertEqual(new_rank_after("entry", ["promote"] * 3), "high")
        self.assertEqual(new_rank_after("low", ["promote", "promote"]), "high")
        self.assertEqual(new_rank_after("mid", ["promote"]), "high")

    def test_demote(self):
        self.assertEqual(new_rank_after("high", ["demote"]), "mid")
        self.assertEqual(new_rank_after("mid", ["demote"]), "low")
        self.assertEqual(new_rank_after("low", ["demote"]), "entry")

    def test_kick(self):
        self.assertEqual(new_rank_after("entry", ["kick"]), "kicked")

    def test_no_action(self):
        self.assertEqual(new_rank_after("high", []), "high")


class TestBuildNotification(unittest.TestCase):
    """四类文案与原脚本 generate_notification 逐字一致。"""

    def test_none(self):
        msg = build_notification("测试玩家", 12345, [], CFG)
        self.assertEqual(msg, "测试玩家 这周贡献为:12345 执行操作:无 非常感谢你对公会做出的贡献 公会群:YOUR_QQ_GROUP")

    def test_promote_with_count(self):
        msg = build_notification("测试玩家", 30000, ["promote", "promote"], CFG)
        self.assertEqual(msg, "测试玩家 这周贡献为:30000 执行操作:升职(2次) 非常感谢你对公会做出的贡献 公会群:YOUR_QQ_GROUP")

    def test_demote(self):
        msg = build_notification("测试玩家", 4999, ["demote"], CFG)
        self.assertEqual(msg, "测试玩家 这周贡献为:4999 执行操作:降职 若有异议请去群里找执行此操作的管理员 公会群:YOUR_QQ_GROUP")

    def test_kick(self):
        msg = build_notification("测试玩家", 3499, ["kick"], CFG)
        self.assertEqual(msg, "测试玩家 这周贡献为:3499 执行操作:踢出 未完成这周最低标准 若有异议请去群里寻找执行管理员 公会群:YOUR_QQ_GROUP")


class TestBuildCommand(unittest.TestCase):
    """命令格式与原脚本一致(执行动作:promote/demote/kick;
    member 查询命令由 main 直接按 commands 配置构造,不经此函数)。"""

    def test_promote(self):
        self.assertEqual(build_command(CFG, "promote", "某玩家"), "/guild promote 某玩家")

    def test_demote(self):
        self.assertEqual(build_command(CFG, "demote", "某玩家"), "/guild demote 某玩家")

    def test_kick_with_reason(self):
        self.assertEqual(build_command(CFG, "kick", "某玩家"),
                         "/guild kick 某玩家 未完成一周最低标准")


if __name__ == "__main__":
    unittest.main()
