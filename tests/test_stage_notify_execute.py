# -*- coding: utf-8 -*-
"""
test_stage_notify_execute.py —— 通知阶段与异步执行阶段
====================================================
stages.py 的两段此前几乎没测到(37%):
  * stage_notify        —— summary / per_player 两种模式 × 串行 / 异步
  * stage_execute_async —— G002 迁移进来的执行阶段(与查询同一套 B→D→E 闭环)

锁定的行为:
  1. 通知开关关闭 / 无记录 / debug 跳过 → 一条广播都不发
  2. 串行失败必须进 failures(报告要能看见),异步只提交不阻塞
  3. 执行阶段:用户确认里被跳过的 kick **不得再执行**(不可逆)
  4. 执行阶段:部分成功只按成功条数推进 new_rank
  5. 执行阶段:失败必须进 failures 且 record 打 error
"""

import json
import unittest
from unittest import mock

from guild_auto.application import stages as stages_mod
from guild_auto.config import DEFAULT_CONFIG
from guild_auto.domain.decisions import new_rank_after


def _cfg():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["log"] = {"enabled": False, "level": 4, "dir": "logs"}
    cfg["notify_templates"]["summary"] = "升{promote}降{demote}踢{kick}联系{admin}"
    return cfg


class _FakeRT:
    def __init__(self, async_enabled=False):
        self.async_enabled = async_enabled
        self.human = mock.Mock()
        self.executor = mock.Mock()
        self.detector = None
        self.hwnd = 123
        self.pause_gate_calls = 0
        self.submitted = []
        # 异步通知会在本阶段内 wait_all + get_failed 收回结果:
        # 默认返回"全部成功",个别用例可改写 return_value 模拟失败。
        self.async_system = mock.Mock()
        self.async_system.get_failed.return_value = []

    def pause_gate(self):
        self.pause_gate_calls += 1

    def is_paused(self):
        return False

    def reset_window_state_after_pause(self, hwnd=None, detector=None):
        return False

    def async_verify_submit(self, command, player, meta=None, **kw):
        # stage_notify 用 msg=... 关键字传文案，这里必须接受额外关键字
        m = dict(meta or {})
        m.update(kw)
        self.submitted.append((command, player, m))
        return f"task-{player}"


def _record(player="甲", actions=None, weekly=100, rank="entry"):
    return {
        "player": player,
        "rank": rank,
        "weekly_total": weekly,
        "joined_at": "x",
        "last_online": "y",
        "daily": [],
        "actions": actions if actions is not None else [],
    }


class TestStageNotify(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()

    def test_no_records_sends_nothing(self):
        rt = _FakeRT()
        failures = stages_mod.stage_notify(rt, [], self.cfg)
        self.assertEqual(failures, [])
        self.assertEqual(len(rt.submitted), 0, "不该提交任何广播")

    def test_notify_disabled_sends_nothing(self):
        cfg = _cfg()
        cfg["switches"]["notify_enabled"] = False
        rt = _FakeRT()
        self.assertEqual(stages_mod.stage_notify(rt, [_record(actions=["kick"])], cfg), [])
        self.assertEqual(len(rt.submitted), 0, "不该提交任何广播")

    def test_debug_skip_sends_nothing(self):
        """Debug 模拟数据绝不能发进真实公会频道。"""
        rt = _FakeRT()
        self.assertEqual(
            stages_mod.stage_notify(
                rt, [_record(actions=["kick"])], self.cfg, debug_notify_skipped=True
            ),
            [],
        )
        self.assertEqual(len(rt.submitted), 0, "不该提交任何广播")

    def test_summary_serial_success(self):
        rt = _FakeRT()
        rt.executor.gc_broadcast_with_retry.return_value = (True, None)
        failures = stages_mod.stage_notify(rt, [_record(actions=["promote"])], self.cfg)
        self.assertEqual(failures, [])
        rt.executor.gc_broadcast_with_retry.assert_called_once()

    def test_summary_serial_failure_recorded(self):
        rt = _FakeRT()
        rt.executor.gc_broadcast_with_retry.return_value = (False, "超时")
        failures = stages_mod.stage_notify(rt, [_record(actions=["promote"])], self.cfg)
        self.assertEqual(len(failures), 1)
        self.assertIn("超时", failures[0]["error"])

    def test_summary_async_submits_without_blocking(self):
        rt = _FakeRT(async_enabled=True)
        stages_mod.stage_notify(rt, [_record(actions=["kick"])], self.cfg)
        self.assertEqual(len(rt.submitted), 1, "异步模式应提交且只提交一条")
        self.assertEqual(len(rt.submitted), 1)
        rt.executor.gc_broadcast_with_retry.assert_not_called()

    def test_per_player_sends_one_per_record(self):
        cfg = _cfg()
        cfg["switches"]["notify_mode"] = "per_player"
        rt = _FakeRT(async_enabled=True)
        recs = [_record("甲", ["kick"]), _record("乙", []), _record("丙", ["promote"])]
        stages_mod.stage_notify(rt, recs, cfg)
        self.assertEqual(
            len(rt.submitted), 3, "per_player 模式必须每个查询成功的玩家都发一条(含无操作)"
        )

    def test_async_notify_failure_is_collected_not_lost(self):
        """异步通知必须在本阶段收回结果:失败要进 failures,不能静默漏到进程结束。

        回归锁定:曾经只 submit 不 wait/get_failed,notify_failures 恒为空,
        报告里「通知 成功/失败」是假的(验收 14:等验证完成再进下一步)。
        """
        rt = _FakeRT(async_enabled=True)
        rt.async_system.get_failed.return_value = [
            {"task_id": "task-summary", "state": "failed", "error": "广播超时"}
        ]
        failures = stages_mod.stage_notify(rt, [_record(actions=["kick"])], self.cfg)
        self.assertEqual(len(failures), 1, f"通知失败未计入: {failures}")
        self.assertEqual(failures[0]["scope"], "summary")
        self.assertIn("广播超时", failures[0]["error"])


class TestStageExecuteAsync(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()
        self.cfg["timeouts"]["error_check_window"] = 0.1
        self.cfg["timeouts"]["max_retries"] = 1

    def test_nothing_to_do(self):
        rt = _FakeRT(async_enabled=True)
        self.assertEqual(stages_mod.stage_execute_async(rt, [], self.cfg), [])

    def test_already_executed_records_are_skipped(self):
        """kick 被用户跳过时会被标 executed=True —— 绝不能再提交一次(不可逆)。"""
        rt = _FakeRT(async_enabled=True)
        rec = _record("甲", ["kick"])
        rec["executed"] = True
        self.assertEqual(stages_mod.stage_execute_async(rt, [rec], self.cfg), [])
        rt.executor.verify_action.assert_not_called()

    def test_all_success_marks_records(self):
        rt = _FakeRT(async_enabled=True)
        rt.executor.verify_action.return_value = (True, None)
        rec = _record("甲", ["promote"])
        failures = stages_mod.stage_execute_async(rt, [rec], self.cfg)
        self.assertEqual(failures, [])
        self.assertTrue(rec["executed"])
        self.assertIsNone(rec["error"])
        self.assertEqual(rec["new_rank"], new_rank_after("entry", ["promote"]))

    def test_failure_is_recorded_and_rank_unchanged(self):
        rt = _FakeRT(async_enabled=True)
        rt.executor.verify_action.return_value = (False, "超时")
        rec = _record("甲", ["kick"])
        failures = stages_mod.stage_execute_async(rt, [rec], self.cfg)
        self.assertEqual(len(failures), 1)
        self.assertTrue(rec["executed"])
        self.assertIn("超时", rec["error"])
        self.assertEqual(rec["new_rank"], "entry")

    def test_partial_success_advances_rank_by_ok_count(self):
        """promote×2 只成功 1 条 → 只升 1 级。"""
        rt = _FakeRT(async_enabled=True)
        calls = iter([(True, None), (False, "超时")])
        rt.executor.verify_action.side_effect = lambda a, p: next(calls)
        rec = _record("甲", ["promote", "promote"])
        stages_mod.stage_execute_async(rt, [rec], self.cfg)
        self.assertEqual(rec["new_rank"], new_rank_after("entry", ["promote"]))

    def test_switch_gate_blocks_kick_in_async_mode(self):
        """功能开关优先于任何判定结果（含自定义规则命中）。

        异步路径此前漏了开关检查 —— 关掉 kick 仍会踢人。
        """
        cfg = json.loads(json.dumps(self.cfg))
        cfg["switches"]["kick_enabled"] = False
        rt = _FakeRT(async_enabled=True)
        rec = _record("甲", ["kick"])
        failures = stages_mod.stage_execute_async(rt, [rec], cfg)
        rt.executor.verify_action.assert_not_called()
        self.assertEqual(rt.human.send_chat_text.call_count, 0, "关闭的开关不得发出指令")
        self.assertTrue(rec["executed"])
        self.assertIn("踢出开关已关闭", rec["error"])
        self.assertEqual(failures, [], "被开关拦下不算执行失败")

    def test_switch_gate_keeps_enabled_actions(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["switches"]["promote_enabled"] = False
        rt = _FakeRT(async_enabled=True)
        rt.executor.verify_action.return_value = (True, None)
        rec = _record("甲", ["promote", "demote"])
        stages_mod.stage_execute_async(rt, [rec], cfg)
        self.assertEqual(rec["actions"], ["demote"], "只应保留开关开启的动作")

    def test_sender_uses_human_not_executor_in_main_thread(self):
        """主线程只发:发送走 human.send_chat_text;验证者只读日志,不再调 execute_action。"""
        rt = _FakeRT(async_enabled=True)
        rt.executor.verify_action.return_value = (True, None)
        rec = _record("甲", ["promote"])
        stages_mod.stage_execute_async(rt, [rec], self.cfg)
        rt.human.send_chat_text.assert_called()
        rt.human.wait_between_commands.assert_called()

    def test_each_command_is_sent_exactly_once(self):
        """异步模式每条命令只发一次:验证者不得重复发送。

        回归锁定:曾经 verify 调 executor.execute_action,而它内部会
        build_command + _send_with_gate 再发一遍 —— promote×2 实际升 4 级、
        踢出重复踢(不可逆)。验证者必须走 verify_action(只读日志)。
        """
        rt = _FakeRT(async_enabled=True)
        rt.executor.verify_action.return_value = (True, None)
        rec = _record("甲", ["promote", "promote"])
        stages_mod.stage_execute_async(rt, [rec], self.cfg)
        self.assertEqual(rt.human.send_chat_text.call_count, 2, "两条动作应各发送一次,不能翻倍")
        self.assertEqual(rt.executor.verify_action.call_count, 2)
        rt.executor.execute_action.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
