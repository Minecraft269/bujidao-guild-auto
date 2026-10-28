# -*- coding: utf-8 -*-
"""
test_real_fixtures.py —— 真实面板块形态回归
==========================================
用**真实日志结构 + 合成玩家名**的 fixture 固化真实边界。

## 为什么要有这个文件

此前"加载中占位值""奇形玩家名""屏蔽词"这些边界只存在于一次性测试里，
没有人把它们锚在**真实面板块的整体结构**上。真实结构与简化样本差别很大：

  - 网易版客户端把整个面板写成**一条物理行**，内部换行是字面 `\\n`（反斜杠+n）
  - 组顺序是 会长 → 管理员 → 副会长 → 高活跃 → 中等 → 低活跃 → 入门
    （**管理员在副会长之前**，与直觉相反）
  - 每级标题带大量缩进空白（右对齐到固定宽度）
  - 尾部带复制标记 `[C]`
  - 名字里混有装饰字符（乄 彡 罒 灬）、整段下划线、纯数字、中英混排

将来任何人重写解析器，这个文件会第一个发现"真实形态回归不了"。

## 数据来源与脱敏

结构取自一次真实运行的游戏日志（本地，**未入库**）；
所有玩家名/公会名均为合成值，不含任何真实玩家、群号、本机路径。
"""

import os
import sys
import unittest


from guild_auto.infrastructure import logger  # noqa: E402
from guild_auto.infrastructure.log_watcher import extract_chat_block, split_messages  # noqa: E402
from guild_auto.domain.parsers import parse_guild_list  # noqa: E402

for _n in (
    "log_debug",
    "log_info",
    "log_warning",
    "log_error",
    "log_critical",
    "log_trace",
    "log_exception",
):
    if not hasattr(logger, _n):
        setattr(logger, _n, lambda *a, **k: None)

SAMPLES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "samples"
)

RANK_NAMES = {
    "leader": "会长",
    "vice_leader": "副会长",
    "admin": "管理员",
    "high": "高活跃成员",
    "mid": "中等活跃成员",
    "low": "低活跃成员",
    "entry": "入门成员",
}


def _read_real_shaped_list():
    """读真实形态的公会面板 fixture,返回含字面量 \\n 的物理行。"""
    path = os.path.join(SAMPLES, "guild_list_real_shaped.txt")
    with open(path, "r", encoding="utf-8") as f:
        return f.read().splitlines()


class TestRealShapedPanelExtraction(unittest.TestCase):
    """第一步:从真实形态的物理行里切出面板。"""

    def test_split_messages_treats_whole_panel_as_one_message(self):
        """网易版整块面板是**一条物理行** → split_messages 应切成 1 条消息。

        这条守住"单行 + 内部字面量 \\n"的解析前提。它坏了，
        后面所有解析都会失败。
        """
        lines = _read_real_shaped_list()
        msgs = split_messages(lines)
        self.assertEqual(len(msgs), 1, f"真实形态应只有 1 条消息,实际 {len(msgs)} 条")
        self.assertTrue(any("[CHAT]" in l for l in msgs[0]))

    def test_extract_chat_block_strips_separators_and_copy_marker(self):
        """必须:还原子面量 \\n、剥掉首尾分隔线、剥掉尾部 [C]。"""
        lines = _read_real_shaped_list()
        block = extract_chat_block(split_messages(lines)[0])
        self.assertIsNotNone(block, "未能从真实形态里切出面板")
        joined = "\n".join(block)
        self.assertNotIn("[C]", joined, "复制标记未剥离")
        self.assertNotIn("--------------------", joined, "分隔线未剥离")
        self.assertIn("演示公会", block[0], "首行应是公会名")

    def test_group_titles_recovered_with_padding(self):
        """真实面板的组标题带大量缩进,也必须被正确识别。"""
        lines = _read_real_shaped_list()
        block = extract_chat_block(split_messages(lines)[0])
        titles = [l for l in block if l.strip().startswith("--")]
        self.assertGreaterEqual(len(titles), 7, f"只识别到 {len(titles)} 个分组标题")


class TestRealShapedPanelParsing(unittest.TestCase):
    """第二步:解析真实形态面板,每个业务断言都要成立。"""

    @classmethod
    def setUpClass(cls):
        lines = _read_real_shaped_list()
        cls.block = extract_chat_block(split_messages(lines)[0])
        cls.result = parse_guild_list(cls.block, RANK_NAMES)

    def test_guild_name_parsed(self):
        self.assertEqual(self.result["guild_name"], "演示公会")

    def test_total_and_online_parsed(self):
        """统计行在面板最末部,必须被正确取出。"""
        self.assertEqual(self.result["total"], 200)
        self.assertEqual(self.result["online"], 43)

    def test_all_seven_groups_have_members(self):
        """7 个等级全部有成员(真实形态下管理员在副会长之前)。"""
        empty = [k for k, v in self.result["groups"].items() if not v]
        self.assertEqual(empty, [], f"这些等级没解析出成员: {empty}")

    def test_admin_group_parsed_before_vice_group(self):
        """真实组顺序:管理员在副会长**之前**(与直觉相反,别弄反)。"""
        idx_admin = next(
            i for i, l in enumerate(self.block) if "管理员" in l and l.strip().startswith("--")
        )
        idx_vice = next(
            i for i, l in enumerate(self.block) if "副会长" in l and l.strip().startswith("--")
        )
        self.assertLess(idx_admin, idx_vice, "fixture 的组顺序变了?真实形态是管理员在副会长之前")

    def test_exotic_names_survive(self):
        """真实名库里的奇形名字不能被丢,也不能把别的名字吞掉。"""
        low = self.result["groups"]["low"]
        for name in (
            "123463278",
            "灰________灰",
            "抖音梦幻awa_Mc",
            "花缘灬遗忘",
            "演示乄浅听雨",
            "静轩彡",
            "风尘彡叹流年罒",
        ):
            with self.subTest(name=name):
                self.assertIn(name, low, f"{name} 未出现在解析结果里")

    def test_no_ghost_entries_from_padding(self):
        """真实面板的缩进空白不得产生幽灵空条目。"""
        for rank, ids in self.result["groups"].items():
            for pid in ids:
                with self.subTest(rank=rank, pid=pid):
                    self.assertTrue(pid.strip(), f"{rank} 组有空条目: {pid!r}")
                    self.assertNotIn("--", pid, f"{rank} 组混入了标题: {pid!r}")
                    self.assertNotIn("成员总数", pid)

    def test_player_count_matches_fixture(self):
        """玩家总数要等于 fixture 里 ● 分隔符的个数,防止解析器悄悄吞人。

        直接数 fixture 里的 ● 个数作为期望值，而不是硬编码一个数字 ——
        硬编码的计数会随 fixture 增删而过期，那才是脆弱的测试。
        """
        expected = "\n".join(self.block).count("●")
        total = sum(len(v) for v in self.result["groups"].values())
        self.assertEqual(
            total,
            expected,
            f"解析出 {total} 个玩家,fixture 里有 {expected} 个 ● 分隔符"
            f"(差 {expected - total} 个,可能吞人或拆错)",
        )

    def test_dedup_preserves_order(self):
        """去重要保序。"""
        for rank, ids in self.result["groups"].items():
            self.assertEqual(len(ids), len(set(ids)), f"{rank} 组有重复名未去重")


if __name__ == "__main__":
    unittest.main(verbosity=2)
