# -*- coding: utf-8 -*-
"""P0-4f：发送间隔下限 1.5 + 三处文档记录「布吉岛公会发言常限 3s」。

守三件事：
  1. `schema.py` 对 `delays.min_send_interval` 设**下限 1.5**（1.4 报错，1.5 / 3.2 通过），
     **不设 3.0 硬限**（用户 2026-11-14 口径：该限制「有时候没有」，且重试兜底已存在）；
  2. `DEFAULT_CONFIG` 的默认值仍是 1.5 / 3.0，三处文档与代码口径一致；
  3. 间隔不足导致**消息被吞**时，既有重试兜底真的能「重发一次 → 成功」（P0-1 的逻辑不重写）。
"""

from __future__ import annotations

import copy
import os
import tempfile
import unittest

from guild_auto.concurrency.async_system import AsyncVerifySystem, run_async_round
from guild_auto.config.defaults import CONFIG_COMMENTS, DEFAULT_CONFIG
from guild_auto.config.schema import validate_config
from guild_auto.infrastructure import logger

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC_RULES = os.path.join(REPO, "docs", "业务规则继承清单.md")
DOC_CONFIG = os.path.join(REPO, "docs", "配置说明.md")
# logger 模块的全局状态（照 test_logger_levels_mock._GLOBALS）：改了就还回去
_LOGGER_GLOBALS = ("_logger", "_log_dir", "_current_level", "_enabled", "_initialized")


def _issues(value, max_value=None):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["delays"]["min_send_interval"] = value
    if max_value is not None:
        cfg["delays"]["max_send_interval"] = max_value
    return [msg for level, msg in validate_config(cfg) if "min_send_interval" in msg]


def _mentions_3s(text):
    """文档里「3s」「3 秒」两种写法都算数，只要说清了这条服务器限制。"""
    return "3 秒" in text or "3s" in text


class TestSendIntervalFloor(unittest.TestCase):
    def test_below_floor_is_rejected(self):
        msgs = _issues(1.4)
        self.assertTrue(msgs, "1.4 应当报错")
        self.assertIn("1.5", msgs[0], "错误文案要说清下限是 1.5")
        self.assertTrue(_mentions_3s(msgs[0]), "错误文案要说明服务器可能限制 3 秒")

    def test_floor_and_above_pass(self):
        # 3.2 必须连 max 一起抬（3.2 > 默认 max 3.0 会撞既有的 min<=max 规则）——
        # 这正是文档里「把 min 调到 3.2+」时要一起做的事
        self.assertEqual(_issues(1.5), [])
        self.assertEqual(_issues(3.2, max_value=4.0), [])
        self.assertEqual(_issues(5, max_value=6.0), [])

    def test_no_upper_hard_limit_at_3(self):
        """3.0 不是硬限：2.0 这种「低于 3s」的合法配置必须放行。"""
        self.assertEqual(_issues(2.0), [])


class TestDefaultsAndDocsAgree(unittest.TestCase):
    def test_defaults_unchanged(self):
        self.assertEqual(DEFAULT_CONFIG["delays"]["min_send_interval"], 1.5)
        self.assertEqual(DEFAULT_CONFIG["delays"]["max_send_interval"], 3.0)

    def test_comment_mentions_3s_and_3_2(self):
        text = CONFIG_COMMENTS["delays.min_send_interval"]
        self.assertTrue(_mentions_3s(text), "配置注释要写明服务器可能限制 3 秒")
        self.assertIn("3.2", text, "配置注释要给出「把 min 调到 3.2+」的建议")

    def test_both_docs_mention_the_same_fact(self):
        for path in (DOC_RULES, DOC_CONFIG):
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertTrue(_mentions_3s(text), f"{os.path.basename(path)} 没记录 3 秒限制")
            self.assertIn("3.2", text, f"{os.path.basename(path)} 没给出 3.2+ 的建议")
            self.assertIn("1.5", text, f"{os.path.basename(path)} 没写默认值/下限 1.5")


class TestSwallowedMessageRetriesOnce(unittest.TestCase):
    """间隔偏小时消息被吞 → 判失败 → 重发一次 → 成功（证明兜底可用，不重写逻辑）。"""

    def setUp(self):
        self._d = tempfile.mkdtemp(prefix="p0_4f_")
        # 与 test_logger_levels_mock.LoggerTempCase 同样的快照/还原：
        # 只改 _initialized 会把 _logger/_log_dir/_current_level/_enabled 留给后面的
        # 用例，实测会让 test_report_analysis_branches 的 log_warning 被多调一次。
        self._snap = {k: getattr(logger, k) for k in _LOGGER_GLOBALS}
        logger._initialized = False
        logger.init_logger(log_dir=self._d, level=6, enabled=True)

    def tearDown(self):
        for h in list(getattr(logger._logger, "handlers", []) or []):
            h.close()
        for k, v in self._snap.items():
            setattr(logger, k, v)

    def _log(self):
        path = os.path.join(self._d, "latest.log")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()

    def test_retry_once_then_success(self):
        sends = []
        calls = {"n": 0}

        def send(cmd, player, meta):
            sends.append((cmd, player))
            return True

        def verify(cmd, player, meta):
            calls["n"] += 1
            if calls["n"] == 1:
                # 间隔 1.5s 时这一条低于服务器的 3s 下限 → 消息被吞、没有回显
                return (False, "没有找到回显（消息被吞）", None)
            return (True, None, None)

        system = AsyncVerifySystem(verify_fn=verify, poll_interval=0.01, task_timeout=0.5)
        ok, failed = run_async_round(
            system, [("cmd-甲", "甲")], send, max_retries=3, resend_on_retry=True
        )
        system.stop()

        self.assertEqual(len(sends), 2, f"被吞后应重发一次（共 2 次），实际 {sends}")
        self.assertEqual(len(ok), 1, "重发后应当成功")
        self.assertEqual(failed, [], "最终不应留下失败项")

        text = self._log()
        self.assertIn("run_async_round", text, "重试闭环应当留下日志")
        self.assertIn("甲", text, "日志里应能定位到该玩家")
        for line in text.splitlines():
            if "run_async_round" in line or "甲" in line:
                print("  " + line.strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
