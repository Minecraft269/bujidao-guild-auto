# -*- coding: utf-8 -*-
"""R-17 回归锁:TRACE 调用的参数字符串不得留未求值的占位符。

病灶形态(修复前 runtime.py:489 / stages.py:91):

    args=("player={logger.player_tag(player)}",)     # 少一个 f

结果是日志里出现**字面量** `player={logger.player_tag(player)}`,验证链无法
归因到人;而它又完全合法(Python 不报错),只有跑到 level=6 才看得出来。
所以用 AST 探针全仓扫描,而不是靠人眼 review。
"""

import ast
import os
import re
import unittest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")

# 未求值占位符的形态: `{name}` / `{obj.attr}` / `{f(x)}`
PLACEHOLDER = re.compile(r"\{[A-Za-z_][^{}]*\}")
TRACE_FUNCS = ("trace_call", "trace_return", "trace_exception", "trace_verify_stage")


def _iter_trace_calls(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "attr", None) or getattr(fn, "id", None) or ""
        if name in TRACE_FUNCS:
            yield node


def _literal_placeholders(call):
    """该调用里当成**普通字符串常量**传入的占位符(f-string 的片段不会命中)。"""
    for sub in ast.walk(call):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            m = PLACEHOLDER.search(sub.value)
            if m:
                yield sub.lineno, sub.value


class TestTraceArgsHaveNoLiteralPlaceholders(unittest.TestCase):
    def test_no_literal_placeholders_in_src(self):
        offenders = []
        for root, _dirs, files in os.walk(SRC):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as f:
                    tree = ast.parse(f.read(), path)
                for call in _iter_trace_calls(tree):
                    for lineno, text in _literal_placeholders(call):
                        offenders.append(f"{os.path.relpath(path, SRC)}:{lineno} → {text!r}")
        self.assertEqual(
            offenders,
            [],
            "TRACE 调用传入未求值占位符(缺 f 前缀),日志里会打成字面量:\n" + "\n".join(offenders),
        )

    def test_probe_would_catch_the_bug(self):
        """探针自检:把修复前的写法喂进来必须命中,否则本测试是摆设。"""
        bad = ast.parse('logger.trace_call("f", args=("player={logger.player_tag(p)}",))')
        found = [
            text for call in _iter_trace_calls(bad) for _ln, text in _literal_placeholders(call)
        ]
        self.assertEqual(len(found), 1)

        good = ast.parse('logger.trace_call("f", args=(f"player={logger.player_tag(p)}",))')
        self.assertEqual(
            [t for call in _iter_trace_calls(good) for _ln, t in _literal_placeholders(call)], []
        )


class TestTraceEndToEnd(unittest.TestCase):
    """真跑一次后台验证函数:落盘必须是 p-xxxxxx 短标识,不是字面量、不是真名。"""

    def setUp(self):
        import shutil
        import tempfile
        from unittest import mock

        from guild_auto.application.runtime import Runtime
        from guild_auto.infrastructure import logger

        self.tmp = tempfile.mkdtemp(prefix="trace_e2e_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        logger._initialized = False
        logger.init_logger(log_dir=self.tmp, level=6, enabled=True)
        self.logger = logger

        # 不跑 __init__(会碰硬件);只喂 _async_verify_fn 用到的那几个属性
        rt = Runtime.__new__(Runtime)
        rt.cfg = {"timeouts": {"response_wait": 0.01}}
        rt.watcher = mock.Mock()
        rt.watcher.wait_for_chat_block.return_value = None  # 直接走超时分支
        rt.is_paused = lambda: False
        self.rt = rt

    def test_trace_call_emits_tag_not_literal(self):
        import os

        self.rt._async_verify_fn("/guild member 合成玩家甲", "合成玩家甲", {})
        with open(os.path.join(self.tmp, "latest.log"), encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if "TRACE_CALL" in ln]
        self.assertTrue(lines, "后台验证没有输出 TRACE_CALL")
        joined = "\n".join(lines)
        self.assertNotIn("{logger.player_tag(player)}", joined, "仍是字面量(缺 f 前缀)")
        self.assertNotIn("合成玩家甲", joined, "TRACE_CALL 泄露了真实玩家名")
        self.assertIn(f"player={self.logger.player_tag('合成玩家甲')}", joined)


if __name__ == "__main__":
    unittest.main()
