# -*- coding: utf-8 -*-
"""
test_pending.py —— 待办持久化与断点续做
======================================
待办机制的失败代价比主流程任何一段都高:
  * 丢一条 → 玩家这周漏处理
  * 重放一条 kick → **不可逆**的错误操作

所以这里逐条锁定:
  1. 坏文件/坏玩家名不能让整个续做流程崩(丢弃 + 告警)
  2. 部分成功的动作只保留**未完成的后缀** —— 否则会重复执行已完成的那条
  3. 全部完成的项目必须从待办移除(不能下次再执行一次)
  4. 用户选择跳过时原样保留
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from guild_auto.application import pending as pending_mod


def _items(*players):
    return [
        {
            "player": p,
            "rank": "entry",
            "actions": ["kick"],
            "trigger": None,
            "weekly": 100,
            "status": "pending",
        }
        for p in players
    ]


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pending_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.path = os.path.join(self.tmp, "pending_actions.json")

    def _write(self, items, guild="演示公会"):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"guild_name": guild, "items": items}, f, ensure_ascii=False)


class TestPendingPath(unittest.TestCase):
    def test_uses_report_dir(self):
        self.assertEqual(
            pending_mod.pending_path_of({"report_dir": "/r"}),
            os.path.join("/r", "pending_actions.json"),
        )

    def test_defaults_to_reports(self):
        self.assertEqual(
            pending_mod.pending_path_of({}), os.path.join("reports", "pending_actions.json")
        )


class TestLoadPending(_Base):
    def test_missing_file_returns_empty(self):
        self.assertEqual(pending_mod.load_pending(self.path), [])

    def test_corrupt_json_returns_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        self.assertEqual(pending_mod.load_pending(self.path), [])

    def test_non_dict_root_returns_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([1, 2, 3], f)
        self.assertEqual(pending_mod.load_pending(self.path), [])

    def test_valid_items_loaded(self):
        self._write(_items("甲", "乙"))
        got = pending_mod.load_pending(self.path)
        self.assertEqual([i["player"] for i in got], ["甲", "乙"])

    def test_items_without_player_or_actions_are_dropped(self):
        self._write(
            [
                {"player": "甲", "actions": ["kick"]},
                {"player": "", "actions": ["kick"]},
                {"player": "乙", "actions": []},
                {"actions": ["kick"]},
                "不是字典",
            ]
        )
        got = pending_mod.load_pending(self.path)
        self.assertEqual([i["player"] for i in got], ["甲"])

    def test_illegal_player_name_is_dropped_and_warned(self):
        """非法玩家名会拼进 /guild kick 命令 —— 必须丢弃,不能让注入进游戏。

        用超长名字触发 validate_player_name 的上限(命令参数注入/文件名穿越)。
        """
        self._write(
            [{"player": "A" * 5000, "actions": ["kick"]}, {"player": "甲", "actions": ["kick"]}]
        )
        with (
            mock.patch.object(pending_mod.logger, "log_warning") as m_warn,
            mock.patch.object(pending_mod, "console") as m_console,
        ):
            got = pending_mod.load_pending(self.path)
        self.assertEqual([i["player"] for i in got], ["甲"])
        m_warn.assert_called()
        m_console.assert_called()


class TestSavePending(_Base):
    def test_roundtrip(self):
        pending_mod.save_pending(self.path, "演示公会", _items("甲"))
        got = pending_mod.load_pending(self.path)
        self.assertEqual([i["player"] for i in got], ["甲"])

    def test_empty_items_still_writes_valid_file(self):
        pending_mod.save_pending(self.path, "演示公会", [])
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["items"], [])


class TestRunPending(_Base):
    def _run(self, answers, executor=None, sw=None, kick_text="kick-confirm"):
        cfg = {
            "switches": sw or {"execution_enabled": True},
            "guild_name": "演示公会",
            "kick_confirm": True,
            "kick_confirm_text": "kick-confirm",
        }
        if executor is None:
            ex = mock.Mock()
            ex.execute_actions.return_value = []
        else:
            ex = executor  # 调用方已配置好返回值，不得覆盖
        answers = list(answers)
        console = mock.Mock()

        def _ask_text(*a, **k):
            if isinstance(kick_text, Exception):
                raise kick_text
            return kick_text

        cont, rem = pending_mod.run_pending(
            cfg,
            mock.Mock(),
            mock.Mock(),
            None,
            ex,
            self.path,
            console_fn=console,
            input_fn=lambda *a, **k: answers.pop(0) if answers else "",
            ask_text_fn=_ask_text,
        )
        return cont, rem, ex, console

    def test_empty_returns_continue(self):
        cont, rem, _, _ = self._run([])
        self.assertTrue(cont)
        self.assertEqual(rem, [])

    def test_user_skips_keeps_all(self):
        self._write(_items("甲"))
        cont, rem, ex, _ = self._run(["n"])
        self.assertFalse(cont)
        self.assertEqual(len(rem), 1)
        ex.execute_actions.assert_not_called()

    def test_all_success_removes_from_pending(self):
        self._write(_items("甲"))
        ex = mock.Mock()
        ex.execute_actions.return_value = [("kick", True, None)]
        cont, rem, _, _ = self._run(["y", ""], executor=ex)
        self.assertFalse(cont, "第二问回车=直接退出,应为 False")
        self.assertEqual(rem, [])
        self.assertEqual(
            pending_mod.load_pending(self.path),
            [],
            "全部完成却仍留在待办 → 下次会重复执行不可逆操作",
        )

    def test_partial_failure_keeps_remaining_suffix(self):
        """部分成功:只保留未完成的后缀,已完成的那条不能再出现。"""
        self._write(
            [{"player": "甲", "rank": "entry", "actions": ["promote", "kick"], "status": "pending"}]
        )
        ex = mock.Mock()
        ex.execute_actions.return_value = [("promote", True, None), ("kick", False, "超时")]
        cont, rem, _, _ = self._run(["y", ""], executor=ex)
        self.assertFalse(cont)
        self.assertEqual(
            rem[0]["actions"], ["kick"], f"已完成的 promote 被保留了: {rem[0]['actions']}"
        )
        self.assertEqual(rem[0]["status"], "failed")
        self.assertIn("kick", rem[0]["error"])

    def test_dry_run_does_not_execute(self):
        self._write(_items("甲"))
        cont, rem, ex, _ = self._run(["n"], sw={"execution_enabled": False})
        ex.execute_actions.assert_not_called()
        self.assertEqual(len(rem), 1)

    def test_disabled_switch_blocks_resume_of_that_action(self):
        """续做不得绕过本次关闭的功能开关。

        否则「这次关掉踢出」会变成「下次启动时补踢一条不可逆指令」——
        CRITICAL 级修复点，必须有回归测试锁住。
        """
        self._write(_items("甲"))  # actions=["kick"]
        ex = mock.Mock()
        _, rem, _, console = self._run(
            ["y", ""], executor=ex, sw={"execution_enabled": True, "kick_enabled": False}
        )
        ex.execute_actions.assert_not_called()
        self.assertEqual(len(rem), 1, "被开关拦下的项必须保留待下次")
        self.assertEqual(rem[0]["actions"], ["kick"])
        self.assertIn("开关已关闭", rem[0]["error"])
        self.assertTrue(
            any("开关已关闭" in str(c) for c in console.call_args_list),
            f"没有向用户说明被谁拦下: {console.call_args_list}",
        )

    def test_continue_answer(self):
        self._write(_items("甲"))
        ex = mock.Mock()
        ex.execute_actions.return_value = [("kick", True, None)]
        cont, _, _, _ = self._run(["y", "c"], executor=ex)
        self.assertTrue(cont, "输入 c 应表示继续新的统计操作")

    def test_eof_on_first_question_means_skip(self):
        """stdin 关闭(EOF)时按"跳过"处理,不能把待办清空。"""
        self._write(_items("甲"))
        cfg = {"switches": {"execution_enabled": True}, "guild_name": "G"}
        ex = mock.Mock()

        def _raise(*a, **k):
            raise EOFError

        cont, rem = pending_mod.run_pending(
            cfg,
            mock.Mock(),
            mock.Mock(),
            None,
            ex,
            self.path,
            console_fn=mock.Mock(),
            input_fn=_raise,
        )
        ex.execute_actions.assert_not_called()
        self.assertEqual(len(rem), 1)
        self.assertFalse(cont)

    # ---- R-11:续做路径的 kick 二次确认 ----

    def test_resume_kick_with_correct_word_executes(self):
        self._write(_items("甲"))
        ex = mock.Mock()
        ex.execute_actions.return_value = [("kick", True, None)]
        _, rem, ex, _ = self._run(["y", ""], executor=ex, kick_text="kick-confirm")
        ex.execute_actions.assert_called_once()
        self.assertEqual(rem, [])

    def test_resume_kick_wrong_word_skips_kick_entirely(self):
        """不逐字匹配确认词 → 一条 kick 都不发,且不再补做(不写回待办)。"""
        self._write(_items("甲"))
        ex = mock.Mock()
        with mock.patch.object(pending_mod.logger, "log_warning") as m_warn:
            _, rem, ex, console = self._run(["y", "", ""], executor=ex, kick_text="y")
        ex.execute_actions.assert_not_called()
        self.assertEqual(rem, [], "用户明确跳过踢出后仍留在待办 → 下次会再踢")
        self.assertTrue(
            any("踢出" in str(c) for c in console.call_args_list),
            f"没有向用户说明踢出被跳过: {console.call_args_list}",
        )
        m_warn.assert_called()

    def test_resume_kick_missing_confirm_word_fails_safe(self):
        """配置缺确认词时不得放行(空确认词+回车=真踢)。"""
        self._write(_items("甲"))
        cfg = {
            "switches": {"execution_enabled": True},
            "guild_name": "G",
            "kick_confirm": True,
            "kick_confirm_text": "",
        }
        ex = mock.Mock()
        cont, rem = pending_mod.run_pending(
            cfg,
            mock.Mock(),
            mock.Mock(),
            None,
            ex,
            self.path,
            console_fn=mock.Mock(),
            input_fn=lambda *a, **k: "y",
            ask_text_fn=lambda *a, **k: "",
        )
        ex.execute_actions.assert_not_called()
        self.assertEqual(rem, [])

    def test_resume_kick_eof_on_confirm_skips_kick(self):
        self._write(_items("甲"))
        ex = mock.Mock()
        self._run(["y", "", ""], executor=ex, kick_text=EOFError())
        ex.execute_actions.assert_not_called()

    def test_resume_skipped_kick_keeps_promote_in_same_item(self):
        """混合动作列表:踢出未确认只摘 kick,同条记录的升职照常执行。"""
        self._write(
            [{"player": "甲", "rank": "entry", "actions": ["promote", "kick"], "status": "pending"}]
        )
        ex = mock.Mock()
        ex.execute_actions.return_value = [("promote", True, None)]
        _, rem, ex, _ = self._run(["y", ""], executor=ex, kick_text="nope")
        ex.execute_actions.assert_called_once()
        self.assertEqual(ex.execute_actions.call_args[0][1], ["promote"])
        self.assertEqual(rem, [])


class TestMergePending(_Base):
    def test_same_player_same_actions_kept_once(self):
        """R-10:跨轮重复登记必须合并(否则续做把同一指令发两遍)。"""
        old = [{"player": "甲", "actions": ["kick"], "status": "failed"}]
        new = [{"player": "甲", "actions": ["kick"], "status": "failed"}]
        merged = pending_mod.merge_pending(old, new)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["player"], "甲")

    def test_later_status_wins(self):
        old = [{"player": "甲", "actions": ["kick"], "error": "旧"}]
        new = [{"player": "甲", "actions": ["kick"], "error": "新"}]
        self.assertEqual(pending_mod.merge_pending(old, new)[0]["error"], "新")

    def test_different_actions_not_collapsed(self):
        """promote×2 是两次真实操作,不能被去重成一条。"""
        items = [
            {"player": "甲", "actions": ["promote"]},
            {"player": "甲", "actions": ["promote", "promote"]},
        ]
        self.assertEqual(len(pending_mod.merge_pending([], items)), 2)

    def test_different_players_kept(self):
        items = [{"player": "甲", "actions": ["kick"]}, {"player": "乙", "actions": ["kick"]}]
        self.assertEqual(len(pending_mod.merge_pending([], items)), 2)


class TestPendingItemsFromRecords(_Base):
    """R-09 写入侧契约:只存未完成后缀,已成功的不得再入待办。"""

    def _rec(self, **kw):
        base = {
            "player": "甲",
            "rank": "entry",
            "weekly_total": 100,
            "actions": ["promote", "promote"],
            "error": "执行失败(promote): 超时",
            "remaining_actions": ["promote"],
        }
        base.update(kw)
        return base

    def test_only_unfinished_suffix_is_saved(self):
        items = pending_mod.pending_items_from_records([self._rec()])
        self.assertEqual(items[0]["actions"], ["promote"], "已成功的动作被写进了待办")
        self.assertEqual(items[0]["status"], "failed")

    def test_fully_succeeded_record_not_saved(self):
        rec = self._rec(error=None, remaining_actions=[])
        self.assertEqual(pending_mod.pending_items_from_records([rec]), [])

    def test_record_without_remaining_key_keeps_all_actions(self):
        """没进执行层(dry-run)的记录没有 remaining_actions → 全部动作仍未做。"""
        rec = self._rec(remaining_actions=None, error="未执行(dry-run)")
        del rec["remaining_actions"]
        self.assertEqual(len(pending_mod.pending_items_from_records([rec])), 1)

    def test_gated_records_not_saved(self):
        for flag in ("blocked_by_switch", "skipped_by_confirm", "skipped_by_mode"):
            self.assertEqual(
                pending_mod.pending_items_from_records([self._rec(**{flag: True})]), [], flag
            )

    def test_empty_remaining_actions_skips_record(self):
        self.assertEqual(
            pending_mod.pending_items_from_records([self._rec(remaining_actions=[])]), []
        )


class TestAsyncTaskConversion(_Base):
    """R-12 / R-13:动作取 meta.action;先 stop 再取补做队列。"""

    class _Sys:
        def __init__(self, queue):
            self._queue = queue
            self.calls = []

        def stop(self, timeout=5.0):
            self.calls.append("stop")

        def get_retry_queue(self):
            self.calls.append("get_retry_queue")
            return self._queue

        def get_stranded(self):
            self.calls.append("get_stranded")
            return []

    def test_split_by_meta_action(self):
        kick = {"player": "甲", "command": "/guild kick 甲", "meta": {"action": "kick"}}
        gc = {"player": "甲", "command": "/gc 通知", "meta": {"msg": "通知"}}
        good, bad = pending_mod.split_async_tasks([kick, gc])
        self.assertEqual(good, [kick])
        self.assertEqual(bad, [gc])

    def test_command_whole_sentence_is_not_used_as_action(self):
        """回归:此前用 command.split(',') 当 actions,续做端认不出 → 条目永久卡住。"""
        items, bad = pending_mod.pending_items_from_async_system(
            self._Sys([{"player": "甲", "command": "/guild kick 甲", "meta": {"action": "kick"}}])
        )
        self.assertEqual(items[0]["actions"], ["kick"])
        self.assertEqual(bad, 0)

    def test_unresumable_counted(self):
        items, bad = pending_mod.pending_items_from_async_system(
            self._Sys([{"player": "甲", "command": "/gc x", "meta": {}}])
        )
        self.assertEqual(items, [])
        self.assertEqual(bad, 1)

    def test_stop_before_read_order(self):
        sys_ = self._Sys([])
        pending_mod.pending_items_from_async_system(sys_)
        self.assertEqual(sys_.calls[0], "stop", f"先取队列后停 → 队列恒空: {sys_.calls}")

    def test_stranded_converted_and_deduped_against_retry(self):
        class _Sys2(self._Sys):
            def get_stranded(self):
                return [
                    {"player": "甲", "command": "/guild demote 甲", "meta": {"action": "demote"}},
                    {"player": "乙", "command": "/guild kick 乙", "meta": {"action": "kick"}},
                ]

        items, bad = pending_mod.pending_items_from_async_system(
            _Sys2([{"player": "甲", "command": "/guild kick 甲", "meta": {"action": "kick"}}])
        )
        players = [i["player"] for i in items]
        self.assertEqual(players, ["甲", "乙"], "同一玩家既在补做队列又在途 → 只留一条")
        self.assertEqual(bad, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
