# -*- coding: utf-8 -*-
"""
test_stage_query_async.py —— 异步查询阶段(规格 B→D→E)
====================================================
对应流程规格(plan.txt「异步(后台)模式」段):
    B → 逐条发送指令:脚本【不读日志、不验证】→ 全部发送完毕后等待 → D
    B → 后台系统读日志并验证 → 成功的忽略并抛弃;失败的记录结果等待 → E
    E → D:若有失败则重试(仅发送一次,后台继续验证)→ B (必须防死锁)

以及硬性验收第 6 条:异步模式下主线程【不等待】异步线程(不等验证)。

与旧实现的根本区别(为什么必须重写):
    旧 async_query_member 把 send_and_capture(含读日志+解析)投给
    AsyncCommandExecutor 的 worker,主线程再 _join_with_progress 等它
    —— 发送节奏仍被后台验证速度拖住,没达到规格要的更快的发送。

本测试用假 Runtime + 假 watcher 驱动 stage_query_async,零真实硬件。
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock


from guild_auto.infrastructure import logger  # noqa: E402
from guild_auto.infrastructure.log_watcher import extract_chat_block, split_messages  # noqa: E402
from guild_auto.application.stages import stage_query_async  # noqa: E402
from guild_auto.application.runtime import Runtime  # noqa: E402

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

GUILD_NAME = "演示公会"
PANEL_START = "--------------------公会---------------------"
PANEL_END = "--------------------------------------------"


def _member_panel(player, weekly_today=100):
    """构造 /guild member 的真实形态面板(单物理行 + 内部字面反斜杠n)。"""
    body = [
        GUILD_NAME,
        player,
        "加入时间: 2026-08-15 10:26:58",
        "上次在线: 2026-10-07 13:55:16",
        "公会经验周贡献:",
        f"    今天           : {weekly_today} 公会经验",
        "    2026-08-12: 0 公会经验",
    ]
    return (
        "[148月2026 21:55:41.046] [Render thread/INFO] "
        "[net.minecraft.client.gui.components.ChatComponent/]: [CHAT] "
        + "\\n".join([PANEL_START, *body, PANEL_END])
        + " [C]"
    )


class _FakeWatcher:
    """按 player 依次返回预置面板;未预置的 -> 返回 None(超时)。

    wait_calls 记录 wait_for_chat_block 被调了几次 —— 这是"验证确实发生过"
    的证据(异步模式下它只应由后台 verify_fn 调用)。
    """

    def __init__(self):
        self._pending = {}  # player -> [panel, ...] 队列
        self.wait_calls = []

    def feed(self, player, panel):
        self._pending.setdefault(player, []).append(panel)

    def wait_for_chat_block(self, timeout=12.0, stop_check=None):
        """返回**块内行列表**(与真实 log_watcher.wait_for_chat_block 契约一致)。

        生产链路是 split_messages -> extract_chat_block,这里走同一条,
        夹具才与真实形态同构(否则会漏掉字面反斜杠n 还原这类前提)。
        """
        self.wait_calls.append(timeout)
        if not self._pending:
            return None
        player = next(iter(self._pending))
        q = self._pending[player]
        physical = q.pop(0)
        if not q:
            del self._pending[player]
        messages = split_messages([physical])
        if not messages:
            return None
        return extract_chat_block(messages[0])


def _cfg():
    from guild_auto.config import DEFAULT_CONFIG
    import json

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["log"] = {"enabled": False, "level": 4, "dir": "logs"}
    cfg["switches"]["verbose_log"] = False  # 测试里不刷屏
    return cfg


def _make_rt(cfg, watcher, send_log=None):
    """构造带假依赖的 Runtime(零真实硬件)。"""
    human = MagicMock()
    human.chat_key = "t"
    if send_log is not None:
        human.send_chat_text.side_effect = lambda cmd, **kw: send_log.append(cmd)
    return Runtime(
        cfg=cfg,
        watcher=watcher,
        human=human,
        executor=MagicMock(),
        detector=None,
        hwnd=None,
        ask_confirm=lambda p, d=False: True,
        sleep=lambda s: None,
        stop_check=lambda: False,
    )


class TestStageQueryAsyncHappyPath(unittest.TestCase):
    """正常路径:全部玩家查询成功。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, True))

    def test_all_success_returns_records(self):
        w = _FakeWatcher()
        w.feed("甲", _member_panel("甲", 100))
        w.feed("乙", _member_panel("乙", 200))
        cfg = _cfg()
        rt = _make_rt(cfg, w)

        records, failures = stage_query_async(rt, [("entry", "甲"), ("low", "乙")], cfg)

        self.assertEqual(len(records), 2, f"应得 2 条记录,实际 {len(records)}")
        self.assertEqual(failures, [])
        # 记录要带上 rank(阶段3 判定依赖它)
        rank_of = {r["player"]: r["rank"] for r in records}
        self.assertEqual(rank_of.get("甲"), "entry")
        self.assertEqual(rank_of.get("乙"), "low")

    def test_weekly_total_parsed_from_panel(self):
        """面板数值必须被正确解析(端到端,不是只测解析器)。"""
        w = _FakeWatcher()
        w.feed("甲", _member_panel("甲", 1234))
        cfg = _cfg()
        rt = _make_rt(cfg, w)
        records, _ = stage_query_async(rt, [("mid", "甲")], cfg)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["weekly_total"], 1234)


class TestStageQueryAsyncFailures(unittest.TestCase):
    """异常路径:玩家无响应 → 记失败,不进 records(不可用于判定)。"""

    def test_no_response_goes_to_failures(self):
        w = _FakeWatcher()  # 不 feed → 永远超时
        cfg = _cfg()
        cfg["timeouts"]["max_retries"] = 2  # 少试几次,测试快
        cfg["timeouts"]["response_wait"] = 0.2
        rt = _make_rt(cfg, w)
        records, failures = stage_query_async(rt, [("entry", "甲")], cfg)
        self.assertEqual(records, [], "查不到数据却产出了记录 —— 会污染判定")
        self.assertEqual(len(failures), 1)
        self.assertIn("player", failures[0])

    def test_partial_failure_isolated(self):
        """部分成功部分失败:成功的进 records,失败的进 failures,互不影响。"""
        w = _FakeWatcher()
        w.feed("甲", _member_panel("甲", 50))
        # 乙 无响应
        cfg = _cfg()
        cfg["timeouts"]["max_retries"] = 2
        cfg["timeouts"]["response_wait"] = 0.2
        rt = _make_rt(cfg, w)
        records, failures = stage_query_async(rt, [("entry", "甲"), ("entry", "乙")], cfg)
        self.assertEqual([r["player"] for r in records], ["甲"])
        self.assertEqual([f["player"] for f in failures], ["乙"])

    def test_masked_player_is_not_sent_at_all(self):
        """屏蔽词玩家**不得发出任何指令**。

        /guild member *** 在网易服务器不会得到响应 —— 发出去只是白烧
        重试周期(历史真 bug:浪费 3 次重试后仍被丢弃)。所以必须在
        提交前就过滤掉,并单独记为 blocked。
        """
        sent = []
        w = _FakeWatcher()
        cfg = _cfg()
        cfg["timeouts"]["max_retries"] = 1
        cfg["timeouts"]["response_wait"] = 0.2
        rt = _make_rt(cfg, w, send_log=sent)
        records, failures = stage_query_async(rt, [("entry", "*" * 8)], cfg)

        self.assertEqual(records, [])
        self.assertTrue(all("*" * 8 not in c for c in sent), f"屏蔽词玩家仍被发送了指令: {sent}")
        self.assertEqual(len(failures), 1)
        self.assertTrue(failures[0].get("blocked"), f"屏蔽词玩家应标 blocked: {failures[0]}")
        self.assertIn("屏蔽", failures[0]["reason"])

    def test_masked_player_gets_readable_reason(self):
        """屏蔽词玩家的失败原因必须说清是网易过滤,而不是笼统的查询重试耗尽。"""
        w = _FakeWatcher()
        cfg = _cfg()
        cfg["timeouts"]["max_retries"] = 1
        cfg["timeouts"]["response_wait"] = 0.2
        rt = _make_rt(cfg, w)
        _, failures = stage_query_async(rt, [("entry", "*" * 8)], cfg)
        self.assertEqual(len(failures), 1)
        self.assertIn(
            "屏蔽", failures[0]["reason"], f"屏蔽词玩家的失败原因不可读: {failures[0]['reason']}"
        )


class TestStageQueryAsyncCoreSemantics(unittest.TestCase):
    """规格核心语义:发送后必须有读日志验证 + 防死锁。"""

    def test_verification_actually_happened(self):
        """验收第7条:所有指令发送后必须有读取日志并验证(异步下由后台承担)。

        证据:wait_for_chat_block 被调用过 —— 它只应出现在后台 verify_fn 里。
        若实现退化成"只发不验",这个计数会是 0。
        """
        w = _FakeWatcher()
        w.feed("甲", _member_panel("甲", 10))
        cfg = _cfg()
        rt = _make_rt(cfg, w)
        stage_query_async(rt, [("entry", "甲")], cfg)
        self.assertGreater(len(w.wait_calls), 0, "没有任何读日志验证发生 —— 违反验收第 7 条")

    def test_sender_only_sends(self):
        """主线程侧发送器只发指令。证据:send_chat_text 收到含玩家名的命令原文。"""
        sent = []
        w = _FakeWatcher()
        w.feed("甲", _member_panel("甲", 10))
        cfg = _cfg()
        rt = _make_rt(cfg, w, send_log=sent)
        stage_query_async(rt, [("entry", "甲")], cfg)
        self.assertTrue(any("甲" in c for c in sent), f"未发送含玩家名的查询指令: {sent}")

    def test_no_deadlock_when_background_hangs(self):
        """防死锁:后台验证一直拿不到响应时,流程必须有限收敛。

        若主线程无限等后台,这里会挂起。用短 max_retries + 短 response_wait
        让闭环自行收敛成失败。
        """
        w = _FakeWatcher()  # 永远不 feed
        cfg = _cfg()
        cfg["timeouts"]["max_retries"] = 2
        cfg["timeouts"]["response_wait"] = 0.2
        rt = _make_rt(cfg, w)
        records, failures = stage_query_async(rt, [("entry", "甲")], cfg)
        self.assertEqual(records, [])
        self.assertGreaterEqual(len(failures), 1)

    def test_empty_targets(self):
        """空目标 → 空结果,不抛异常。"""
        w = _FakeWatcher()
        rt = _make_rt(_cfg(), w)
        records, failures = stage_query_async(rt, [], _cfg())
        self.assertEqual((records, failures), ([], []))


if __name__ == "__main__":
    unittest.main(verbosity=2)
