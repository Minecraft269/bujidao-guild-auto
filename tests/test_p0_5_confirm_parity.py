# -*- coding: utf-8 -*-
"""test_p0_5_confirm_parity.py —— R-32 收口:两侧对「无确认词」必须同口径(均为不放行)

启动校验(`schema.validate_config`)已把 **缺键 / null / 空串 / 空白串** 拦在启动阶段
(见 `tests/test_config_jsonc_guards.py::TestKickConfirmTextMustBeNonEmpty`)。

这里锁定**运行期兜底**:即使校验被绕过(内存里改 cfg、UI 传半截配置),
主流程 `stage_confirm_execution` 与续做 `run_pending` 都必须**一条踢出都不发**。
曾出现的 bug:`expected` 为空串时 `if ans != expected` 在用户直接回车
(`"" == ""`)时成立 → 不可逆的踢出被放行。
"""

import copy
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from guild_auto.application import pending as pending_mod
from guild_auto.application.runtime import Runtime
from guild_auto.application.stages import stage_confirm_execution
from guild_auto.config import DEFAULT_CONFIG
from guild_auto.infrastructure import logger

# 日志未初始化时补齐空实现(与控制台无关,只看判定)
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


# 四种"拿不到非空确认词"的配置形态
SHAPES = {
    "缺键": lambda c: c.pop("kick_confirm_text", None),
    "null": lambda c: c.__setitem__("kick_confirm_text", None),
    "空串": lambda c: c.__setitem__("kick_confirm_text", ""),
    "空白串": lambda c: c.__setitem__("kick_confirm_text", "   "),
}


def _cfg():
    c = copy.deepcopy(DEFAULT_CONFIG)
    c["log"] = {"enabled": False, "level": 4, "dir": "logs"}
    c["kick_confirm"] = True
    return c


def _rec():
    return {
        "player": "合成玩家甲",
        "rank": "entry",
        "weekly_total": 100,
        "actions": ["kick"],
        "executed": False,
    }


class TestConfirmParity(unittest.TestCase):
    """同一种配置形态,主流程与续做侧必须给出同一个结论。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p05_parity_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.path = os.path.join(self.tmp, "pending_actions.json")

    # ---- 主流程侧 ----
    def _main_flow(self, mutate):
        """返回 (execution_enabled, skipped_kicks, records, 追问确认词的次数)。"""
        cfg = _cfg()
        mutate(cfg)
        asked = {"text": 0}

        def _ask_text(_prompt):
            asked["text"] += 1
            return ""  # 用户直接回车

        rt = Runtime(
            cfg=cfg,
            watcher=None,
            human=None,
            executor=None,
            detector=None,
            hwnd=None,
            ask_confirm=lambda p, d=False: True,  # 总确认通过
            ask_text_fn=_ask_text,
            sleep=lambda s: None,
            stop_check=lambda: False,
        )
        records = [_rec()]
        enabled, skipped = stage_confirm_execution(rt, records, cfg)
        return enabled, skipped, records, asked["text"]

    # ---- 续做侧 ----
    def _resume(self, mutate):
        """返回 (是否一条动作都没发, execute_actions 调用次数)。"""
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "guild_name": "演示公会",
                    "items": [
                        {
                            "player": "合成玩家甲",
                            "rank": "entry",
                            "actions": ["kick"],
                            "trigger": None,
                            "weekly": 100,
                            "status": "pending",
                        }
                    ],
                },
                f,
                ensure_ascii=False,
            )
        cfg = {
            "switches": {"execution_enabled": True},
            "guild_name": "演示公会",
            "kick_confirm": True,
            "kick_confirm_text": "kick-confirm",
        }
        mutate(cfg)
        ex = mock.Mock()
        ex.execute_actions.return_value = []
        pending_mod.run_pending(
            cfg,
            mock.Mock(),
            mock.Mock(),
            None,
            ex,
            self.path,
            console_fn=mock.Mock(),
            input_fn=lambda *a, **k: "y",
            ask_text_fn=lambda *a, **k: "",  # 用户直接回车
        )
        return ex.execute_actions.call_count == 0, ex.execute_actions.call_count

    def test_each_shape_blocks_kick_on_both_sides(self):
        for name, mutate in SHAPES.items():
            with self.subTest(shape=name):
                enabled, main_skipped, records, asked_text = self._main_flow(mutate)
                self.assertTrue(enabled, f"[{name}] 总确认已通过,执行开关应保持打开")
                self.assertTrue(main_skipped, f"[{name}] 主流程无确认词却放行踢出")
                self.assertEqual(asked_text, 0, f"[{name}] 无确认词时不该再追问确认词")
                self.assertTrue(records[0]["executed"], f"[{name}] 跳过的踢出必须打标防补刀")

                resume_skipped, calls = self._resume(mutate)
                self.assertTrue(resume_skipped, f"[{name}] 续做侧无确认词却发了动作")
                self.assertEqual(calls, 0, f"[{name}] 续做侧仍调用了 execute_actions")

                self.assertEqual(
                    bool(main_skipped),
                    bool(resume_skipped),
                    f"[{name}] 两侧口径不一致:主流程={main_skipped} 续做={resume_skipped}",
                )

    def test_valid_word_still_executes(self):
        """对照:有合法确认词且逐字输入时主流程必须放行(证明上面的用例不是恒真)。"""
        cfg = _cfg()
        rt = Runtime(
            cfg=cfg,
            watcher=None,
            human=None,
            executor=None,
            detector=None,
            hwnd=None,
            ask_confirm=lambda p, d=False: True,
            ask_text_fn=lambda p: cfg["kick_confirm_text"],
            sleep=lambda s: None,
            stop_check=lambda: False,
        )
        records = [_rec()]
        enabled, skipped = stage_confirm_execution(rt, records, cfg)
        self.assertTrue(enabled)
        self.assertFalse(skipped, "确认词正确却被跳过")
        self.assertFalse(records[0]["executed"], "确认通过不该预标记为已执行")


if __name__ == "__main__":
    unittest.main(verbosity=2)
