# -*- coding: utf-8 -*-
"""
test_name_lists.py —— 黑白名单与保护名单(阶段 5)
==============================================
优先级(短路,先命中先返回):

    功能开关 > blacklist > 屏蔽词策略 > whitelist > whitelist_protected > 阈值判定

本文件锁定的行为:
  1. 名字匹配**大小写不敏感**;match_wildcard 关掉时 * 只是普通字符
  2. 黑名单优先于白名单(同时命中时按黑名单处理)
  3. 白名单为空 = 不启用限制;非空时**只有**名单内的人可动
  4. 保护名单只剥夺降职/踢出,**升职仍生效**
  5. 名单不命中时,阈值判定与改造前完全一致(业务规则零改动)
"""
import json
import unittest

from guild_auto.config import DEFAULT_CONFIG
from guild_auto.domain.decisions import (
    BLOCK_BLACKLIST,
    BLOCK_NOT_WHITELISTED,
    decide_for_player,
    is_protected,
    name_list_block_reason,
    name_matches,
)


def _cfg(**overrides):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg.update(overrides)
    return cfg


class TestNameMatches(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(name_matches("甲", ["甲"]))
        self.assertFalse(name_matches("乙", ["甲"]))

    def test_case_insensitive_exact(self):
        self.assertTrue(name_matches("Alice", ["alice"], wildcard=False))
        self.assertTrue(name_matches("ALICE", ["alice"], wildcard=False))

    def test_case_insensitive_wildcard(self):
        self.assertTrue(name_matches("TestPlayer", ["test*"], wildcard=True))
        self.assertTrue(name_matches("TESTPLAYER", ["test*"], wildcard=True))

    def test_wildcard_disabled_treats_star_literally(self):
        """关闭通配时 '*' 只是普通字符 —— 防止误把玩家名里的星号当模式。"""
        self.assertFalse(name_matches("测试甲", ["测试*"], wildcard=False))
        self.assertTrue(name_matches("测试*", ["测试*"], wildcard=False))

    def test_question_mark_single_char(self):
        self.assertTrue(name_matches("甲", ["?"], wildcard=True))
        self.assertFalse(name_matches("甲乙", ["?"], wildcard=True))

    def test_empty_patterns_never_match(self):
        self.assertFalse(name_matches("甲", []))
        self.assertFalse(name_matches("甲", None))


class TestBlockReason(unittest.TestCase):
    def test_blacklist_blocks(self):
        cfg = _cfg(blacklist=["捣乱玩家"])
        self.assertEqual(name_list_block_reason("捣乱玩家", cfg), BLOCK_BLACKLIST)

    def test_blacklist_wins_over_whitelist(self):
        """同时出现在两个名单时按黑名单处理(优先级更高)。"""
        cfg = _cfg(blacklist=["甲"], whitelist=["甲"])
        self.assertEqual(name_list_block_reason("甲", cfg), BLOCK_BLACKLIST)

    def test_empty_whitelist_does_not_restrict(self):
        cfg = _cfg(whitelist=[])
        self.assertIsNone(name_list_block_reason("任意路人", cfg))

    def test_non_empty_whitelist_blocks_outsiders(self):
        cfg = _cfg(whitelist=["甲"])
        self.assertIsNone(name_list_block_reason("甲", cfg))
        self.assertEqual(name_list_block_reason("乙", cfg), BLOCK_NOT_WHITELISTED)

    def test_wildcard_applies_to_lists(self):
        cfg = _cfg(blacklist=["测试*"])
        self.assertEqual(name_list_block_reason("测试甲", cfg), BLOCK_BLACKLIST)
        self.assertIsNone(name_list_block_reason("正常甲", cfg))


class TestProtected(unittest.TestCase):
    def test_protected_membership(self):
        cfg = _cfg(whitelist_protected=["新人甲"])
        self.assertTrue(is_protected("新人甲", cfg))
        self.assertFalse(is_protected("路人乙", cfg))

    def test_empty_protected_list(self):
        self.assertFalse(is_protected("任何人", _cfg()))


class TestDecideForPlayer(unittest.TestCase):
    """名单短路 + 阈值判定的组合。"""

    TH = {"entry_kick": 3500, "entry_promote_1": 10000}

    def _cfg_with_th(self, **overrides):
        cfg = _cfg(**overrides)
        cfg["thresholds"] = self.TH
        return cfg

    def test_blacklisted_player_gets_no_action(self):
        """即使贡献为 0 该被踢,黑名单玩家也完全不动。"""
        cfg = self._cfg_with_th(blacklist=["甲"])
        actions, trigger = decide_for_player("甲", "entry", 0, cfg)
        self.assertEqual(actions, [])
        self.assertEqual(trigger, BLOCK_BLACKLIST)

    def test_whitelist_outsider_gets_no_action(self):
        cfg = self._cfg_with_th(whitelist=["甲"])
        actions, trigger = decide_for_player("乙", "entry", 0, cfg)
        self.assertEqual(actions, [])
        self.assertEqual(trigger, BLOCK_NOT_WHITELISTED)

    def test_whitelisted_player_follows_threshold(self):
        cfg = self._cfg_with_th(whitelist=["甲"])
        actions, trigger = decide_for_player("甲", "entry", 0, cfg)
        self.assertEqual(actions, ["kick"])
        self.assertEqual(trigger, "entry_kick")

    def test_protected_player_keeps_promote(self):
        from guild_auto.domain.decisions import decide_actions
        cfg = self._cfg_with_th(whitelist_protected=["甲"])
        actions, trigger = decide_for_player("甲", "entry", 20000, cfg)
        # 与纯阈值判定逐字对照(不硬编码期望,避免夹具阈值不全导致误判)
        expected_actions, expected_trigger = decide_actions("entry", 20000, cfg["thresholds"])
        self.assertTrue(expected_actions and expected_actions[0] == "promote")
        self.assertEqual(actions, expected_actions, "保护名单不该剥夺升职")
        self.assertEqual(trigger, expected_trigger)

    def test_protected_player_loses_kick(self):
        cfg = self._cfg_with_th(whitelist_protected=["甲"])
        actions, trigger = decide_for_player("甲", "entry", 0, cfg)
        self.assertEqual(actions, [], "保护名单玩家不该被踢")
        self.assertEqual(trigger, "whitelist_protected")

    def test_unlisted_player_behaves_exactly_as_before(self):
        """名单为空时,判定必须与 decide_actions 逐字一致 —— 回归护栏。"""
        from guild_auto.domain.decisions import decide_actions
        cfg = self._cfg_with_th()
        for rank in ("entry", "low", "mid", "high"):
            for weekly in (0, 3000, 4000, 10000, 20000, 29999, 30000, 30001):
                got = decide_for_player("路人", rank, weekly, cfg)
                want = decide_actions(rank, weekly, cfg["thresholds"])
                self.assertEqual(got, want,
                                 f"rank={rank} weekly={weekly} 的判定被名单逻辑改动了")


if __name__ == "__main__":
    unittest.main(verbosity=2)