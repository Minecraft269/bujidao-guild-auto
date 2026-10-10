# -*- coding: utf-8 -*-
"""
test_main_build_targets.py —— main.py 可测化改造的第一批单测
=============================================================
main.py 覆盖率只有 8%,而它占项目总体覆盖率缺口的 95%。根因是逻辑全部
内嵌在 600 多行的 main() 里,函数体内直接调 LogWatcher / GameStateDetector /
find_game_window / input(),不真实按键就跑不到任何分支。

改造方向:把**纯逻辑**从 main() 抽成模块级函数,不依赖硬件即可单测。
本文件测第一个抽出来的 build_targets。

数据全部为合成玩家名,无任何真实数据。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logger  # noqa: E402
from main import build_targets  # noqa: E402

for _n in ("log_debug", "log_info", "log_warning", "log_error",
           "log_critical", "log_trace", "log_exception"):
    if not hasattr(logger, _n):
        setattr(logger, _n, lambda *a, **k: None)


def _groups(**kw):
    """构造 guild["groups"](等级 -> 玩家列表)。"""
    base = {"leader": [], "vice_leader": [], "admin": [],
            "high": [], "mid": [], "low": [], "entry": []}
    base.update(kw)
    return base


class TestBuildTargetsBasic(unittest.TestCase):
    """基本路径:范围过滤与跳过等级。"""

    def test_scope_all_keeps_everyone_except_skipped(self):
        g = _groups(high=["甲"], mid=["乙"], entry=["丙"])
        targets, dropped = build_targets(g, scope="all")
        self.assertEqual(dropped, 0)
        self.assertEqual(sorted(p for _, p in targets), ["丙", "乙", "甲"])

    def test_skip_ranks_excluded(self):
        g = _groups(leader=["会长"], admin=["管理"], high=["甲"], entry=["乙"])
        targets, _ = build_targets(
            g, scope="all", skip_ranks=("leader", "admin"))
        self.assertEqual([p for _, p in targets], ["甲", "乙"])

    def test_scope_single_rank_only(self):
        g = _groups(high=["甲"], mid=["乙"], entry=["丙"])
        targets, _ = build_targets(g, scope="entry")
        self.assertEqual(targets, [("entry", "丙")])

    def test_empty_guild_returns_empty(self):
        targets, dropped = build_targets(_groups(), scope="all")
        self.assertEqual((targets, dropped), ([], 0))

    def test_rank_preserved_in_pair(self):
        """每个目标必须带上自己的等级 —— 判定依赖它。"""
        targets, _ = build_targets(_groups(mid=["甲"]), scope="all")
        self.assertEqual(targets, [("mid", "甲")])


class TestBuildTargetsDedup(unittest.TestCase):
    """跨等级去重 —— 独立审查指出的真实缺陷。

    不去重时,同一玩家出现在两个等级段会被当两条独立目标:
    查询两遍、判定两遍,执行阶段**重复发送**升职/踢出命令。
    """

    def test_duplicate_across_ranks_keeps_first(self):
        """同名玩家跨等级:只保留**首次出现**的等级。"""
        g = _groups(high=["重复玩家"], mid=["重复玩家", "乙"])
        targets, dropped = build_targets(g, scope="all")
        self.assertEqual(dropped, 1)
        self.assertEqual(targets, [("high", "重复玩家"), ("mid", "乙")],
                         "必须保留首次出现的等级,且不重复")

    def test_duplicate_cannot_cause_double_execution(self):
        """护栏:重复玩家必须只剩**一条**目标 —— 否则执行阶段会重复发命令。

        直接对应真实风险:踢出命令发两次,第二次可能把刚升上来的别人踢掉,
        或者对同一玩家重复计数。

        断言只说"只剩一条"与 dropped 计数,**不写死保留哪个等级** ——
        保留哪一条取决于 dict 迭代顺序(面板等级段顺序),那是实现细节;
        这里真正要锁的业务不变式是"一条都不能多"。
        """
        g = _groups(entry=["危险玩家"], low=["危险玩家"],
                    mid=["危险玩家"], high=["危险玩家"])
        targets, dropped = build_targets(g, scope="all")
        self.assertEqual(len(targets), 1,
                         f"重复玩家产生了 {len(targets)} 条目标")
        self.assertEqual(dropped, 3)
        # 保留下来的那条必须仍是这个玩家(等级取面板首次出现的那一个)
        self.assertEqual(targets[0][1], "危险玩家")

    def test_same_rank_duplicate_also_dropped(self):
        """同一等级内重复出现(面板异常)同样去重。"""
        g = _groups(entry=["甲", "甲", "乙"])
        targets, dropped = build_targets(g, scope="all")
        self.assertEqual(targets, [("entry", "甲"), ("entry", "乙")])
        self.assertEqual(dropped, 1)

    def test_order_preserved_after_dedup(self):
        """去重后保持面板展示顺序。"""
        g = _groups(high=["甲", "乙"], mid=["乙", "丙"], entry=["甲", "丁"])
        targets, dropped = build_targets(g, scope="all")
        self.assertEqual([p for _, p in targets], ["甲", "乙", "丙", "丁"])
        self.assertEqual(dropped, 2)

    def test_no_duplicate_means_no_drop(self):
        """正常面板(无重复)dropped 必须为 0 —— 否则误报会误导用户。"""
        g = _groups(high=["甲"], mid=["乙"], low=["丙"], entry=["丁"])
        _, dropped = build_targets(g, scope="all")
        self.assertEqual(dropped, 0)


class TestBuildTargetsDedupWithScope(unittest.TestCase):
    """去重与范围过滤同时生效时的交互。"""

    def test_scope_narrows_then_dedups(self):
        """scope 收窄后仍要 dedup,且 dropped 只数范围内的重复。"""
        g = _groups(high=["甲"], mid=["甲", "乙"], entry=["甲"])
        targets, dropped = build_targets(g, scope="mid")
        self.assertEqual(targets, [("mid", "甲"), ("mid", "乙")])
        self.assertEqual(dropped, 0, "范围外的重复不应计入 dropped")

    def test_skip_and_dedup_together(self):
        g = _groups(leader=["甲"], high=["甲", "乙"], entry=["乙"])
        targets, dropped = build_targets(
            g, scope="all", skip_ranks=("leader", "admin", "vice_leader"))
        self.assertEqual([p for _, p in targets], ["甲", "乙"])
        self.assertEqual(dropped, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
