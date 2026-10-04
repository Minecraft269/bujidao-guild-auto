# -*- coding: utf-8 -*-
"""
test_thresholds.py —— 阈值矩阵边界三点全覆盖
==============================================
对应硬性验收标准第 4 条:每个阈值取 边界-1 / 边界 / 边界+1 三点全部断言。

业务真源: docs/业务规则继承清单.md §1(零改动继承)

边界语义(必须逐条对照,写错即为业务缺陷):
  * high_demote      用 <=  → 边界值 30000 **会**降职;30001 不会
  * mid_promote      用 >=  → 边界值 30000 **会**升职;29999 不会
  * mid_demote       用 <   → 边界值 5000  **不会**降职;4999 会
  * low_promote_2    用 >=  → 边界值 30000 **会**升职 2 次
  * low_promote_1    用 >=  → 边界值 10000 **会**升职 1 次
  * low_demote       用 <   → 边界值 3500  **不会**降职;3499 会
  * entry_promote_3  用 >=  → 边界值 30000 **会**升职 3 次
  * entry_promote_2  用 >=  → 边界值 10000 **会**升职 2 次
  * entry_promote_1  用 >=  → 边界值 3500  **会**升职 1 次
  * entry_kick       用 <   → 边界值 3500  **不会**踢出;3499 会

30000 处的特殊语义:高活跃与中等活跃**同值反向**
  (high 在 30000 降职,mid 在 30000 升职)——原脚本如此,必须继承。

【重要:降职类阈值的三点语义与升职类相反】
"降职/踢出"是**贡献不足**才触发,阈值区间落在边界**内侧**:
    high_demote = 30000 判定 exp <= 30000 → 29999(边界-1)**仍然降职** ✓
    mid_demote  = 5000  判定 exp <  5000  → 5000(边界)**不降职**,4999 才降 ✓
"升职"是**贡献达标**才触发,阈值区间落在边界**外侧**:
    mid_promote = 30000 判定 exp >= 30000 → 29999(边界-1)**不升职** ✓
所以两类阈值的三点(边界-1/边界/边界+1)分别是:
    降职类 → (降职, 降职, 无动作)
    升职类 → (无动作, 升职, 升职)
写测试时按此对照,别把方向写反。

本文件补齐 tests/test_actions.py 的缺口:那边只测了"边界/边界+1"两点,
这里补上"边界-1",凑满验收要求的三点全覆盖。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logger  # noqa: E402
from actions import decide_actions, new_rank_after  # noqa: E402
from config import DEFAULT_CONFIG  # noqa: E402

# logger 未 init 时不写文件,这里只需保证被调用的 log_* 函数可用
for _n in ("log_debug", "log_info", "log_warning", "log_error",
           "log_critical", "log_trace", "log_exception"):
    if not hasattr(logger, _n):
        setattr(logger, _n, lambda *a, **k: None)


def _t():
    """取默认阈值(业务真源默认值)。"""
    return DEFAULT_CONFIG["thresholds"]


class TestHighRankThresholdMatrix(unittest.TestCase):
    """高活跃 high:唯一分支 exp <= 30000 -> demote ×1。"""

    def test_boundary_minus_one_demote(self):
        """29999(边界-1):仍 <= 30000 → **降职**。

        注意方向:high_demote 是"贡献低于 30000 就降职",所以边界**内**侧(29999)
        同样是降职,边界**外**侧(30001)才是无动作。这与 mid_demote 用 `<` 时
        "边界值本身不降职"的语义不同 —— 两个阈值的方向相反,别写混。"""
        self.assertEqual(decide_actions("high", 29999, _t()), (["demote"], "high_demote"))

    def test_at_boundary_demote(self):
        """30000(边界):<= 含边界 → 降职 1 次。"""
        self.assertEqual(decide_actions("high", 30000, _t()), (["demote"], "high_demote"))

    def test_boundary_plus_one_no_action(self):
        """30001(边界+1):仍 <= 30000? 否 → 无动作。"""
        self.assertEqual(decide_actions("high", 30001, _t()), ([], None))

    def test_zero_demote(self):
        """边界下方极端值 0 → 降职。"""
        self.assertEqual(decide_actions("high", 0, _t()), (["demote"], "high_demote"))


class TestMidRankThresholdMatrix(unittest.TestCase):
    """中等活跃 mid:两个分支,先升职后降职。"""

    def test_promote_boundary_minus_one_no_action(self):
        """29999(mid_promote 边界-1):< 30000 且 >= 5000 → 无动作。"""
        self.assertEqual(decide_actions("mid", 29999, _t()), ([], None))

    def test_promote_at_boundary(self):
        """30000(mid_promote 边界):>= 含边界 → 升职 1 次。"""
        self.assertEqual(decide_actions("mid", 30000, _t()), (["promote"], "mid_promote"))

    def test_promote_boundary_plus_one(self):
        """30001 → 升职。"""
        self.assertEqual(decide_actions("mid", 30001, _t()), (["promote"], "mid_promote"))

    def test_demote_boundary_plus_one_no_action(self):
        """5001(mid_demote 边界+1):< 5000? 否 → 无动作。"""
        self.assertEqual(decide_actions("mid", 5001, _t()), ([], None))

    def test_demote_at_boundary_no_action(self):
        """5000(mid_demote 边界):< 5000 **不含**边界 → 无动作(关键!)。"""
        self.assertEqual(decide_actions("mid", 5000, _t()), ([], None),
                         "mid_demote 用 < ,边界值 5000 不应降职")

    def test_demote_boundary_minus_one_demote(self):
        """4999(mid_demote 边界-1):< 5000 → 降职 1 次。"""
        self.assertEqual(decide_actions("mid", 4999, _t()), (["demote"], "mid_demote"))

    def test_between_thresholds_no_action(self):
        """5000~29999 区间:既不升职也不降职 → 无动作(抽查 20000)。"""
        self.assertEqual(decide_actions("mid", 20000, _t()), ([], None))


class TestLowRankThresholdMatrix(unittest.TestCase):
    """低活跃 low:三个分支(升2 / 升1 / 降1),if/elif 顺序不可颠倒。"""

    def test_promote2_boundary_minus_one_falls_to_promote1(self):
        """29999(low_promote_2 边界-1)→ 落到 low_promote_1 → 升职 1 次。
        关键:30000 升 2 次,29999 只升 1 次 —— 验证 if/elif 链顺序。"""
        self.assertEqual(decide_actions("low", 29999, _t()), (["promote"], "low_promote_1"))

    def test_promote2_at_boundary(self):
        """30000(low_promote_2 边界):>= → 升职 **2** 次。"""
        self.assertEqual(decide_actions("low", 30000, _t()),
                         (["promote", "promote"], "low_promote_2"))

    def test_promote2_boundary_plus_one(self):
        """30001 → 升职 2 次。"""
        self.assertEqual(decide_actions("low", 30001, _t()),
                         (["promote", "promote"], "low_promote_2"))

    def test_promote1_boundary_minus_one_no_action(self):
        """9999:>= 10000? 否;< 3500? 否 → 无动作(区间内取值)。"""
        self.assertEqual(decide_actions("low", 9999, _t()), ([], None))

    def test_promote1_at_boundary(self):
        """10000(low_promote_1 边界):>= → 升职 1 次。"""
        self.assertEqual(decide_actions("low", 10000, _t()), (["promote"], "low_promote_1"))

    def test_promote1_boundary_plus_one(self):
        """10001 → 升职 1 次。"""
        self.assertEqual(decide_actions("low", 10001, _t()), (["promote"], "low_promote_1"))

    def test_demote_boundary_plus_one_no_action(self):
        """3501(low_demote 边界+1):< 3500? 否 → 无动作。"""
        self.assertEqual(decide_actions("low", 3501, _t()), ([], None))

    def test_demote_at_boundary_no_action(self):
        """3500(low_demote 边界):< 3500 **不含**边界 → 无动作(关键!)。"""
        self.assertEqual(decide_actions("low", 3500, _t()), ([], None),
                         "low_demote 用 < ,边界值 3500 不应降职")

    def test_demote_boundary_minus_one_demote(self):
        """3499(low_demote 边界-1):< 3500 → 降职 1 次。"""
        self.assertEqual(decide_actions("low", 3499, _t()), (["demote"], "low_demote"))


class TestEntryRankThresholdMatrix(unittest.TestCase):
    """入门成员 entry:四个分支(升3 / 升2 / 升1 / 踢出)。"""

    def test_promote3_boundary_minus_one_falls_to_promote2(self):
        """29999(entry_promote_3 边界-1)→ 落到 entry_promote_2 → 升职 2 次。"""
        self.assertEqual(decide_actions("entry", 29999, _t()),
                         (["promote", "promote"], "entry_promote_2"))

    def test_promote3_at_boundary(self):
        """30000(entry_promote_3 边界):>= → 升职 **3** 次。"""
        self.assertEqual(decide_actions("entry", 30000, _t()),
                         (["promote", "promote", "promote"], "entry_promote_3"))

    def test_promote3_boundary_plus_one(self):
        """30001 → 升职 3 次。"""
        self.assertEqual(decide_actions("entry", 30001, _t()),
                         (["promote", "promote", "promote"], "entry_promote_3"))

    def test_promote2_boundary_minus_one_falls_to_promote1(self):
        """9999(entry_promote_2 边界-1)→ 落到 entry_promote_1 → 升职 1 次。"""
        self.assertEqual(decide_actions("entry", 9999, _t()), (["promote"], "entry_promote_1"))

    def test_promote2_at_boundary(self):
        """10000(entry_promote_2 边界):>= → 升职 2 次。"""
        self.assertEqual(decide_actions("entry", 10000, _t()),
                         (["promote", "promote"], "entry_promote_2"))

    def test_promote2_boundary_plus_one(self):
        """10001 → 升职 2 次。"""
        self.assertEqual(decide_actions("entry", 10001, _t()),
                         (["promote", "promote"], "entry_promote_2"))

    def test_promote1_boundary_minus_one_kicks(self):
        """3499(entry_promote_1 边界-1)→ 落到 entry_kick → 踢出。"""
        self.assertEqual(decide_actions("entry", 3499, _t()), (["kick"], "entry_kick"))

    def test_promote1_at_boundary(self):
        """3500(entry_promote_1 边界):>= → 升职 1 次。

        关键:3500 在 entry 等级**升职**(>= 含边界),但在 low 等级**不降职**(< 不含边界),
        而 3499 在 entry 等级**踢出** —— 这是最容易写错的一处边界。"""
        self.assertEqual(decide_actions("entry", 3500, _t()), (["promote"], "entry_promote_1"))

    def test_promote1_boundary_plus_one(self):
        """3501 → 升职 1 次(不会误落到 kick 分支)。"""
        self.assertEqual(decide_actions("entry", 3501, _t()), (["promote"], "entry_promote_1"))
        self.assertNotIn("kick", decide_actions("entry", 3501, _t())[0])

    def test_kick_boundary_minus_one(self):
        """3499(entry_kick 边界-1):< 3500 → 踢出 1 次。"""
        self.assertEqual(decide_actions("entry", 3499, _t()), (["kick"], "entry_kick"))

    def test_kick_zero(self):
        """0 → 踢出。"""
        self.assertEqual(decide_actions("entry", 0, _t()), (["kick"], "entry_kick"))


class TestThresholdCrossRankSameValue(unittest.TestCase):
    """跨等级同值行为(30000 处 high/mid 同值反向;3500 处 low/entry 行为不同)。"""

    def test_30000_high_demotes_mid_promotes(self):
        """30000:high 降职,mid 升职 —— 同值反向,原脚本语义必须保留。"""
        self.assertEqual(decide_actions("high", 30000, _t())[0], ["demote"])
        self.assertEqual(decide_actions("mid", 30000, _t())[0], ["promote"])

    def test_3500_entry_promotes_low_no_action(self):
        """3500:entry 升职 1 次,low 无动作 —— 同值不同行为。"""
        self.assertEqual(decide_actions("entry", 3500, _t())[0], ["promote"])
        self.assertEqual(decide_actions("low", 3500, _t())[0], [])

    def test_3499_entry_kicks_low_demotes(self):
        """3499:entry 踢出,low 降职。"""
        self.assertEqual(decide_actions("entry", 3499, _t())[0], ["kick"])
        self.assertEqual(decide_actions("low", 3499, _t())[0], ["demote"])


class TestUnknownRankAndAbnormalInput(unittest.TestCase):
    """未知等级 / 异常数值的安全性(配置写错时不得误踢人)。"""

    def test_unknown_rank_returns_no_action(self):
        self.assertEqual(decide_actions("unknown_rank", 0, _t()), ([], None))

    def test_empty_rank_returns_no_action(self):
        self.assertEqual(decide_actions("", 100, _t()), ([], None))

    def test_negative_weekly_does_not_crash(self):
        """负贡献(异常数据)不得崩溃。entry → 踢出;high → 降职。"""
        self.assertEqual(decide_actions("entry", -1, _t())[0], ["kick"])
        self.assertEqual(decide_actions("high", -1, _t())[0], ["demote"])

    def test_custom_thresholds_override_defaults(self):
        """阈值可配置:传入自定义阈值时按自定义判定,不受默认值影响。"""
        custom = {"high_demote": 100, "mid_promote": 200, "mid_demote": 50,
                  "low_promote_2": 300, "low_promote_1": 100, "low_demote": 30,
                  "entry_promote_3": 400, "entry_promote_2": 200, "entry_promote_1": 60,
                  "entry_kick": 60}
        self.assertEqual(decide_actions("high", 100, custom), (["demote"], "high_demote"))
        self.assertEqual(decide_actions("high", 101, custom), ([], None))
        self.assertEqual(decide_actions("mid", 200, custom), (["promote"], "mid_promote"))
        self.assertEqual(decide_actions("mid", 49, custom), (["demote"], "mid_demote"))


class TestNewRankAfter(unittest.TestCase):
    """动作执行后的新等级推导(报告用)。"""

    def test_no_action_keeps_rank(self):
        self.assertEqual(new_rank_after("mid", []), "mid")

    def test_promote_entry_to_low(self):
        self.assertEqual(new_rank_after("entry", ["promote"]), "low")

    def test_promote_twice_entry_to_mid(self):
        self.assertEqual(new_rank_after("entry", ["promote", "promote"]), "mid")

    def test_promote_thrice_entry_to_high(self):
        self.assertEqual(new_rank_after("entry", ["promote"] * 3), "high")

    def test_promote_capped_at_high(self):
        """超出等级链上限时封顶为 high,不越界。"""
        self.assertEqual(new_rank_after("high", ["promote"]), "high")

    def test_demote_mid_to_low(self):
        self.assertEqual(new_rank_after("mid", ["demote"]), "low")

    def test_demote_high_to_mid(self):
        self.assertEqual(new_rank_after("high", ["demote"]), "mid")

    def test_demote_entry_stays_entry(self):
        """entry 已是最低级,降职后仍是 entry(封底,不越界)。"""
        self.assertEqual(new_rank_after("entry", ["demote"]), "entry")

    def test_kick_marks_kicked(self):
        self.assertEqual(new_rank_after("entry", ["kick"]), "kicked")
        self.assertEqual(new_rank_after("high", ["kick"]), "kicked")


class TestTriggerKeyUniqueness(unittest.TestCase):
    """trigger_key 用于报告标注,必须唯一标识触发的分支(改错键名会导致报告失真)。"""

    def test_all_ten_threshold_keys_reachable(self):
        """10 个阈值键都能被真实触发。"""
        reachable = {
            "high_demote": ("high", 100),
            "mid_promote": ("mid", 30000),
            "mid_demote": ("mid", 100),
            "low_promote_2": ("low", 30000),
            "low_promote_1": ("low", 10000),
            "low_demote": ("low", 100),
            "entry_promote_3": ("entry", 30000),
            "entry_promote_2": ("entry", 10000),
            "entry_promote_1": ("entry", 3500),
            "entry_kick": ("entry", 100),
        }
        t = _t()
        triggered = set()
        for key, (rank, weekly) in reachable.items():
            actions, trigger = decide_actions(rank, weekly, t)
            self.assertEqual(trigger, key,
                             f"{rank}@{weekly} 应触发 {key},实际 {trigger} actions={actions}")
            triggered.add(trigger)
        self.assertEqual(len(triggered), 10, "10 个阈值键应全部可达")


if __name__ == "__main__":
    unittest.main(verbosity=2)