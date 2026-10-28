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
    def _run(self, answers, executor=None, sw=None):
        cfg = {"switches": sw or {"execution_enabled": True}, "guild_name": "演示公会"}
        if executor is None:
            ex = mock.Mock()
            ex.execute_actions.return_value = []
        else:
            ex = executor  # 调用方已配置好返回值，不得覆盖
        answers = list(answers)
        console = mock.Mock()
        cont, rem = pending_mod.run_pending(
            cfg,
            mock.Mock(),
            mock.Mock(),
            None,
            ex,
            self.path,
            console_fn=console,
            input_fn=lambda *a, **k: answers.pop(0) if answers else "",
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
