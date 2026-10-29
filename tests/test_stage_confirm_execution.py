# -*- coding: utf-8 -*-
"""
test_stage_confirm_execution.py —— 执行前确认与 kick 二次确认
==========================================================
对应硬性验收第 9 条:
    每个阶段推进前等待用户确认后再进行(流程已明确:启动前回车、执行操作前确认、
    kick 二次确认、退出前回车;其余阶段边界也需设置确认点,粒度统一设计并可通过
    配置开关控制)

以及流程规格:
    执行阶段:执行前需用户确认(kick 需二次确认)

本测试锁定三条不可违反的行为:
  1. 用户拒绝执行 → execution_enabled 变 False,一条命令都不能发
  2. kick 必须二次确认;未确认 → 全部踢出被跳过,且被打标
     (不可逆操作不得因忘了标记而被执行循环补刀)
  3. 无可执行项时不问用户(不拿无意义提示打扰人)
"""

import os
import shutil
import sys
import tempfile
import unittest


from guild_auto.infrastructure import logger  # noqa: E402
from guild_auto.application.stages import stage_confirm_execution  # noqa: E402
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


def _cfg():
    from guild_auto.config import DEFAULT_CONFIG
    import json

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["log"] = {"enabled": False, "level": 4, "dir": "logs"}
    return cfg


def _rt(answer=True, log_answers=None, kick_text="kick-confirm"):
    """构造 Runtime;ask_confirm 固定返回 answer,ask_text 固定返回 kick_text。"""

    def _ask(p, d=False):
        if log_answers is not None:
            log_answers.append(p)
        return answer

    return Runtime(
        cfg=_cfg(),
        watcher=None,
        human=None,
        executor=None,
        detector=None,
        hwnd=None,
        ask_confirm=_ask,
        ask_text_fn=_ask_text(log_answers, kick_text),
        sleep=lambda s: None,
        stop_check=lambda: False,
    )


def _ask_text(log_answers, text="kick-confirm"):
    """ask_text_fn:记录提问并把固定文本作为"用户输入"返回。"""

    def _impl(prompt):
        if log_answers is not None:
            log_answers.append(prompt)
        return text

    return _impl


def _rt_seq(answers):
    """构造按序列依次返回答案的 Runtime(用于两道确认)。

    序列里 bool 给总确认(ask),str 给 kick 二次确认(ask_text)。
    """
    it = iter(answers)
    return Runtime(
        cfg=_cfg(),
        watcher=None,
        human=None,
        executor=None,
        detector=None,
        hwnd=None,
        ask_confirm=lambda p, d=False: next(it),
        ask_text_fn=lambda p: next(it),
        sleep=lambda s: None,
        stop_check=lambda: False,
    )


def _rec(player, actions):
    return {
        "player": player,
        "rank": "entry",
        "weekly_total": 100,
        "actions": actions,
        "executed": False,
    }


class TestNoActionableNoAsk(unittest.TestCase):
    """无可执行项 → 不问用户。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, True))

    def test_no_actionable_returns_enabled_without_asking(self):
        asked = []
        rt = _rt(answer=True, log_answers=asked)
        enabled, skipped = stage_confirm_execution(rt, [_rec("甲", [])], _cfg())
        self.assertTrue(enabled)
        self.assertFalse(skipped)
        self.assertEqual(asked, [], f"无可执行项却向用户提问: {asked}")

    def test_empty_records(self):
        asked = []
        rt = _rt(answer=True, log_answers=asked)
        enabled, skipped = stage_confirm_execution(rt, [], _cfg())
        self.assertTrue(enabled)
        self.assertEqual(asked, [])


class TestUserDeclineBlocksExecution(unittest.TestCase):
    """验收第9条核心:用户拒绝 → 不得执行任何命令。"""

    def test_decline_disables_execution(self):
        rt = _rt(answer=False)
        enabled, _ = stage_confirm_execution(
            rt, [_rec("甲", ["promote"]), _rec("乙", ["demote"])], _cfg()
        )
        self.assertFalse(enabled, "用户拒绝执行却仍返回 enabled=True —— 命令会被发出去")

    def test_decline_skips_kick_question(self):
        """总确认被拒后不该再问 kick 二次确认(已决定不执行)。"""
        asked = []
        rt = _rt(answer=False, log_answers=asked)
        stage_confirm_execution(rt, [_rec("甲", ["kick"])], _cfg())
        self.assertEqual(len(asked), 1, f"被拒后仍追问: {asked}")

    def test_decline_leaves_records_untouched(self):
        """拒绝执行不得改动记录(executed/error 保持原状,报告据实呈现)。"""
        rt = _rt(answer=False)
        records = [_rec("甲", ["promote"])]
        stage_confirm_execution(rt, records, _cfg())
        self.assertFalse(records[0]["executed"])
        self.assertIsNone(records[0].get("error"))


class TestKickDoubleConfirm(unittest.TestCase):
    """kick 二次确认:不可逆操作,未确认必须跳过全部。"""

    def test_kick_requires_explicit_confirm(self):
        """总确认通过但 kick 二次确认未过 → skipped_kicks=True。"""
        rt = _rt_seq([True, "nope"])
        enabled, skipped = stage_confirm_execution(rt, [_rec("甲", ["kick"])], _cfg())
        self.assertTrue(enabled, "总确认已通过")
        self.assertTrue(skipped, "kick 未被二次确认却放行 —— 不可逆操作会误踢")

    def test_skipped_kicks_marked_not_executed(self):
        """被跳过的 kick 必须打标,否则执行循环可能补刀(不可逆)。"""
        rt = _rt_seq([True, "nope"])
        records = [_rec("甲", ["kick"])]
        _, skipped = stage_confirm_execution(rt, records, _cfg())
        self.assertTrue(skipped)
        self.assertTrue(records[0]["executed"], "跳过的 kick 未标记,可能被补刀")
        self.assertIn(
            "跳过", records[0].get("error", ""), f"未写明跳过原因: {records[0].get('error')!r}"
        )
        # 没踢就没踢,等级保持原样
        self.assertEqual(records[0]["new_rank"], "entry")

    def test_kick_confirm_ok_allows_execution(self):
        """两道确认都过 → 不跳过,记录不被预标记。"""
        rt = _rt_seq([True, "kick-confirm"])
        records = [_rec("甲", ["kick"])]
        enabled, skipped = stage_confirm_execution(rt, records, _cfg())
        self.assertTrue(enabled)
        self.assertFalse(skipped)
        self.assertFalse(records[0]["executed"], "确认通过却不该预标记 executed")


class TestNonKickActions(unittest.TestCase):
    """升/降只需一道确认;kick_confirm 可配置关闭。"""

    def test_promote_only_needs_single_confirm(self):
        asked = []
        rt = _rt(answer=True, log_answers=asked)
        enabled, skipped = stage_confirm_execution(rt, [_rec("甲", ["promote"])], _cfg())
        self.assertTrue(enabled)
        self.assertFalse(skipped)
        self.assertEqual(len(asked), 1, f"升职只需一次确认,实际问了 {len(asked)} 次")

    def test_mixed_actions_two_questions(self):
        """升/降 + kick 混合 → 总确认 + kick 二次确认共两次。"""
        asked = []
        rt = _rt(answer=True, log_answers=asked)
        stage_confirm_execution(rt, [_rec("甲", ["promote"]), _rec("乙", ["kick"])], _cfg())
        self.assertEqual(len(asked), 2, f"应有 2 次确认,实际 {len(asked)}: {asked}")

    def test_kick_confirm_disabled_by_config(self):
        """kick_confirm=False 时不再二次确认(配置开关控制)。"""
        cfg = _cfg()
        cfg["kick_confirm"] = False
        asked = []
        rt = _rt(answer=True, log_answers=asked)
        enabled, skipped = stage_confirm_execution(rt, [_rec("甲", ["kick"])], cfg)
        self.assertTrue(enabled)
        self.assertFalse(skipped)
        self.assertEqual(len(asked), 1, "kick_confirm=False 时不该追问")

    def test_habitual_y_does_not_confirm_kick(self):
        """只认逐字确认词:单敲 y(习惯动作)绝不能被当成确认。

        回归锁定:曾用布尔化 ask()(只认 y/yes),导致"输入 kick-confirm 被跳过、
        敲 y 反而真踢"——不可逆操作上的语义反转。
        """
        rt = _rt_seq([True, "y"])
        _, skipped = stage_confirm_execution(rt, [_rec("甲", ["kick"])], _cfg())
        self.assertTrue(skipped, "单敲 y 不是确认词,必须跳过踢出")

    def test_confirm_text_is_case_sensitive(self):
        rt = _rt_seq([True, "KICK-CONFIRM"])
        _, skipped = stage_confirm_execution(rt, [_rec("甲", ["kick"])], _cfg())
        self.assertTrue(skipped, "确认词大小写敏感")

    def test_confirm_text_comes_from_config(self):
        """确认词由配置驱动,换一个词也应生效(零硬编码)。"""
        cfg = _cfg()
        cfg["kick_confirm_text"] = "确认踢出"
        rt = _rt_seq([True, "确认踢出"])
        _, skipped = stage_confirm_execution(rt, [_rec("甲", ["kick"])], cfg)
        self.assertFalse(skipped, "配置的确认词必须被接受")


if __name__ == "__main__":
    unittest.main(verbosity=2)
