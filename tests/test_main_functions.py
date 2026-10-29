# -*- coding: utf-8 -*-
"""
test_main_functions.py —— main.py 的查询原语与配置准备
====================================================
main.py 是最后的覆盖率洼地(15%)。其中一半是**可以纯 mock 测**的函数:
  auto_detect_admin / send_and_capture / query_guild_list / query_member /
  parse_args / prepare_config / _pause_until_resume
另一半(main() 主体)另见 test_main_flow.py。

这些函数承载的正是"发命令 → 等回显 → 解析"的主干,历来是 bug 高发区
(latest.log 里 30 次重发 /guild list 的死循环就出自这里)。测试锁定的行为:
  * 重试上限必须真的收敛(不许无限重发)
  * 暂停中断不消耗重试次数
  * 伪造面板(公会名不符 / 成员数不符)必须被拒
  * 屏蔽词玩家在发命令前就判定失败,不浪费重试配额
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from guild_auto import main as main_mod
from guild_auto.config import DEFAULT_CONFIG


def _cfg(**overrides):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["log"] = {"enabled": False, "level": 4, "dir": "logs"}
    cfg.update(overrides)
    return cfg


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="main_fn_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        for p in (
            mock.patch.object(main_mod, "console", lambda *a, **k: None),
            mock.patch.object(main_mod.time, "sleep", lambda *a: None),
        ):
            p.start()
            self.addCleanup(p.stop)


class TestAutoDetectAdmin(_Base):
    def test_configured_admin_wins(self):
        """配置里已填 admin 时直接返回,不读日志。"""
        cfg = _cfg(admin="已配置管理员")
        with mock.patch.object(
            main_mod, "detect_encoding", side_effect=AssertionError("不该读日志")
        ):
            self.assertEqual(main_mod.auto_detect_admin(cfg), "已配置管理员")

    def test_detects_from_log_username_line(self):
        path = os.path.join(self.tmp, "latest.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write("[10:00:00] [main/INFO]: Starting minecraft\n")
            f.write("[10:00:01] [main/INFO]: --username, TestAdmin123\n")
        cfg = _cfg(admin="", game_log=path)
        self.assertEqual(main_mod.auto_detect_admin(cfg), "TestAdmin123")

    def test_fullwidth_comma_variant(self):
        """真实日志里逗号有全角变体,必须都能识别。

        显式指定 log_encoding:编码自动检测对"只有一行"的样本采不到足够证据
        (它有自己的一组测试),这里要锁的是正则本身。
        """
        path = os.path.join(self.tmp, "latest.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write("--username， 中文管理员\n")
        cfg = _cfg(admin="", game_log=path, log_encoding="utf-8")
        self.assertEqual(main_mod.auto_detect_admin(cfg), "中文管理员")

    def test_missing_log_returns_empty(self):
        cfg = _cfg(admin="", game_log=os.path.join(self.tmp, "nope.log"))
        self.assertEqual(main_mod.auto_detect_admin(cfg), "")

    def test_log_without_username_returns_empty(self):
        path = os.path.join(self.tmp, "latest.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write("普通启动日志\n")
        self.assertEqual(main_mod.auto_detect_admin(_cfg(admin="", game_log=path)), "")


class TestSendAndCapture(_Base):
    """send_and_capture 的重试与中断语义。"""

    def _args(self, **kw):
        return dict(
            watcher=mock.Mock(),
            human=mock.Mock(),
            hwnd=None,
            cmd="/guild list",
            timeout=1.0,
            verbose=False,
            detector=None,
            try_wait=0.1,
            try_times=kw.pop("try_times", 2),
            first_command=kw.pop("first_command", False),
            stop_check=kw.pop("stop_check", None),
        )

    def test_first_command_probe_success(self):
        """first_command 试探成功:只粘贴+回车,不走开栏路径。"""
        a = self._args(first_command=True)
        a["human"].paste_and_enter.return_value = True
        a["watcher"].wait_for_chat_block.return_value = ["面板"]
        block, interrupted = main_mod.send_and_capture(**a)
        self.assertEqual(block, ["面板"])
        self.assertFalse(interrupted)
        a["human"].send_chat_text.assert_not_called()

    def test_first_command_probe_fails_then_standard_path(self):
        """试探无响应 → 回落到标准开栏路径。"""
        a = self._args(first_command=True)
        a["human"].paste_and_enter.return_value = True
        a["watcher"].wait_for_chat_block.side_effect = [None, ["面板"]]
        block, interrupted = main_mod.send_and_capture(**a)
        self.assertEqual(block, ["面板"])
        a["human"].send_chat_text.assert_called()

    def test_standard_retry_succeeds_on_second_attempt(self):
        a = self._args()
        a["watcher"].wait_for_chat_block.side_effect = [None, ["面板"]]
        block, interrupted = main_mod.send_and_capture(**a)
        self.assertEqual(block, ["面板"])
        self.assertFalse(interrupted)

    def test_all_attempts_fail_enters_pause(self):
        """全部重试失败 → 进入暂停等待人工介入(不能静默返回)。"""
        a = self._args()
        a["watcher"].wait_for_chat_block.return_value = None
        with mock.patch.object(main_mod, "_pause_until_resume") as m_pause:
            block, interrupted = main_mod.send_and_capture(**a)
        self.assertIsNone(block)
        m_pause.assert_called_once()

    def test_stop_check_interrupts_immediately(self):
        """stop_check 返回 True → 立即返回 interrupted=True。"""
        a = self._args(stop_check=lambda: True)
        block, interrupted = main_mod.send_and_capture(**a)
        self.assertIsNone(block)
        self.assertTrue(interrupted)


class TestQueryGuildList(_Base):
    def test_masked_name_sends_when_policy_is_query_attempt(self):
        """blocked_name_policy=query_attempt 时串行路径也必须真的去查(验收 17)。"""
        cfg = _cfg()
        cfg["blocked_name_policy"] = "query_attempt"
        with mock.patch.object(main_mod, "send_and_capture", return_value=(None, False)) as m_send:
            main_mod.query_member(cfg, mock.Mock(), mock.Mock(), None, "*" * 8)
        m_send.assert_called()

    def test_success_returns_info(self):
        cfg = _cfg(guild_name="演示公会")
        with (
            mock.patch.object(main_mod, "send_and_capture", return_value=(["面板"], False)),
            mock.patch.object(
                main_mod,
                "parse_guild_list",
                return_value={
                    "guild_name": "演示公会",
                    "total": 3,
                    "online": 1,
                    "groups": {"entry": ["甲", "乙", "丙"]},
                },
            ),
        ):
            info = main_mod.query_guild_list(cfg, mock.Mock(), mock.Mock(), None)
        self.assertEqual(info["guild_name"], "演示公会")

    def test_guild_name_mismatch_is_rejected(self):
        """公会名不符 → 视为伪造/串服,重试耗尽后抛错。"""
        cfg = _cfg(guild_name="期望公会")
        with (
            mock.patch.object(main_mod, "send_and_capture", return_value=(["面板"], False)),
            mock.patch.object(
                main_mod,
                "parse_guild_list",
                return_value={
                    "guild_name": "别的公会",
                    "total": 3,
                    "online": 0,
                    "groups": {"entry": ["甲"]},
                },
            ),
        ):
            with self.assertRaises(RuntimeError) as cm:
                main_mod.query_guild_list(cfg, mock.Mock(), mock.Mock(), None)
        self.assertIn("公会名不匹配", str(cm.exception))

    def test_member_count_mismatch_is_rejected(self):
        """面板声明总数与实解析人数差太多 → 疑似伪造。"""
        cfg = _cfg(guild_name="演示公会", skip_ranks=[])
        with (
            mock.patch.object(main_mod, "send_and_capture", return_value=(["面板"], False)),
            mock.patch.object(
                main_mod,
                "parse_guild_list",
                return_value={
                    "guild_name": "演示公会",
                    "total": 50,
                    "online": 0,
                    "groups": {"entry": ["甲"]},
                },
            ),
        ):
            with self.assertRaises(RuntimeError) as cm:
                main_mod.query_guild_list(cfg, mock.Mock(), mock.Mock(), None)
        self.assertIn("成员数不一致", str(cm.exception))

    def test_timeout_exhausts_retries(self):
        cfg = _cfg(
            timeouts={"max_retries": 2, "response_wait": 0.1, "try_wait": 0.1, "try_times": 1}
        )
        with mock.patch.object(main_mod, "send_and_capture", return_value=(None, False)):
            with self.assertRaises(RuntimeError) as cm:
                main_mod.query_guild_list(cfg, mock.Mock(), mock.Mock(), None)
        self.assertIn("等待响应超时", str(cm.exception))

    def test_interrupt_does_not_consume_retry(self):
        """暂停中断不消耗重试次数 —— 否则暂停一次就少一次机会。"""
        cfg = _cfg(
            guild_name="演示公会",
            timeouts={"max_retries": 2, "response_wait": 0.1, "try_wait": 0.1, "try_times": 1},
        )
        calls = {"n": 0}

        def fake_send(*a, **k):
            calls["n"] += 1
            if calls["n"] <= 2:
                return None, True  # 前两次都是"中断"
            return ["面板"], False

        with (
            mock.patch.object(main_mod, "send_and_capture", side_effect=fake_send),
            mock.patch.object(
                main_mod,
                "parse_guild_list",
                return_value={
                    "guild_name": "演示公会",
                    "total": 1,
                    "online": 0,
                    "groups": {"entry": ["甲"]},
                },
            ),
        ):
            info = main_mod.query_guild_list(cfg, mock.Mock(), mock.Mock(), None)
        self.assertEqual(info["guild_name"], "演示公会")
        self.assertGreaterEqual(calls["n"], 3, "中断后应继续重试,不能提前放弃")


class TestQueryMember(_Base):
    def test_masked_name_fails_before_sending(self):
        """屏蔽词玩家:身份不可读,发命令前就失败 —— 不浪费重试配额。"""
        with mock.patch.object(main_mod, "send_and_capture") as m_send:
            result = main_mod.query_member(_cfg(), mock.Mock(), mock.Mock(), None, "*" * 8)
        self.assertIsNone(result)
        m_send.assert_not_called()

    def test_success_returns_info(self):
        with (
            mock.patch.object(main_mod, "send_and_capture", return_value=(["面板"], False)),
            mock.patch.object(
                main_mod,
                "parse_guild_member",
                return_value={
                    "player": "甲",
                    "weekly_total": 100,
                    "joined_at": "x",
                    "last_online": "y",
                    "daily": [],
                },
            ),
        ):
            info = main_mod.query_member(_cfg(), mock.Mock(), mock.Mock(), None, "甲")
        self.assertEqual(info["weekly_total"], 100)

    def test_timeout_returns_none(self):
        cfg = _cfg(
            timeouts={"max_retries": 2, "response_wait": 0.1, "try_wait": 0.1, "try_times": 1}
        )
        with mock.patch.object(main_mod, "send_and_capture", return_value=(None, False)):
            self.assertIsNone(main_mod.query_member(cfg, mock.Mock(), mock.Mock(), None, "甲"))

    def test_parse_error_retries_then_gives_up(self):
        cfg = _cfg(
            timeouts={"max_retries": 2, "response_wait": 0.1, "try_wait": 0.1, "try_times": 1}
        )
        with (
            mock.patch.object(main_mod, "send_and_capture", return_value=(["面板"], False)),
            mock.patch.object(
                main_mod, "parse_guild_member", side_effect=main_mod.ParseError("坏面板")
            ),
        ):
            self.assertIsNone(main_mod.query_member(cfg, mock.Mock(), mock.Mock(), None, "甲"))


class TestParseArgs(_Base):
    def test_defaults(self):
        with mock.patch.object(sys, "argv", ["prog"]):
            args = main_mod.parse_args()
        self.assertEqual(args.config, main_mod.DEFAULT_CONFIG_FILE)
        self.assertIsNone(args.admin)
        self.assertFalse(args.dry_run)

    def test_all_flags(self):
        argv = [
            "prog",
            "--config",
            "c.json",
            "--admin",
            "A",
            "--log",
            "l",
            "--options",
            "o",
            "--dry-run",
            "--debug",
            "--debug-skip",
            "list,member",
        ]
        with mock.patch.object(sys, "argv", argv):
            args = main_mod.parse_args()
        self.assertEqual(args.config, "c.json")
        self.assertEqual(args.admin, "A")
        self.assertEqual(args.log, "l")
        self.assertEqual(args.options, "o")
        self.assertTrue(args.dry_run)
        self.assertTrue(args.debug)
        self.assertEqual(args.debug_skip, "list,member")


class TestPrepareConfig(_Base):
    def _args(self, path, **kw):
        d = dict(
            config=path,
            admin=None,
            log=None,
            options=None,
            dry_run=False,
            debug=False,
            debug_skip=None,
        )
        d.update(kw)
        return mock.Mock(**d)

    def test_existing_config_loads(self):
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_cfg(), f, ensure_ascii=False)
        exit_fn = mock.Mock()
        cfg = main_mod.prepare_config(self._args(path), console_fn=mock.Mock(), exit_fn=exit_fn)
        exit_fn.assert_not_called()
        self.assertEqual(cfg["guild_qq"], DEFAULT_CONFIG["guild_qq"])

    def test_missing_config_creates_and_exits(self):
        path = os.path.join(self.tmp, "brand_new.json")
        exit_fn = mock.Mock()
        with mock.patch.object(main_mod, "input", return_value=""):
            main_mod.prepare_config(self._args(path), console_fn=mock.Mock(), exit_fn=exit_fn)
        exit_fn.assert_called_once_with(0)
        self.assertTrue(os.path.exists(path), "首次运行必须生成配置文件")

    def test_cli_overrides_applied(self):
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_cfg(), f, ensure_ascii=False)
        cfg = main_mod.prepare_config(
            self._args(
                path,
                admin="CLI管理员",
                log="/cli/log",
                options="/cli/opt",
                dry_run=True,
                debug=True,
                debug_skip="list,member",
            ),
            console_fn=mock.Mock(),
            exit_fn=mock.Mock(),
        )
        self.assertEqual(cfg["admin"], "CLI管理员")
        self.assertEqual(cfg["game_log"], "/cli/log")
        self.assertEqual(cfg["game_options"], "/cli/opt")
        self.assertFalse(cfg["switches"]["execution_enabled"], "dry-run 必须关掉执行")
        self.assertTrue(cfg["debug"]["enabled"])
        self.assertEqual(cfg["debug"]["skip_stages"], ["list", "member"])

    def test_validation_error_calls_exit_1(self):
        path = os.path.join(self.tmp, "config.json")
        bad = _cfg()
        # ⚠ 不能把 thresholds 整段置空:load_config 会与 DEFAULT_CONFIG 深合并,
        # 空 dict 会被默认值补回来。必须写一个"合并后仍然非法"的值 ——
        # 字符串混进数值项正是用户手改 JSON 时最常见的错误。
        bad["thresholds"]["high_demote"] = "30000"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(bad, f, ensure_ascii=False)
        exit_fn = mock.Mock()
        main_mod.prepare_config(self._args(path), console_fn=mock.Mock(), exit_fn=exit_fn)
        exit_fn.assert_called_once_with(1)


class TestPauseUntilResume(_Base):
    def test_debug_mode_exits_immediately(self):
        with (
            mock.patch.object(main_mod.runtime_mod, "set_paused") as m_set,
            mock.patch.object(main_mod, "wait_if_paused") as m_wait,
        ):
            with self.assertRaises(SystemExit):
                main_mod._pause_until_resume(_cfg(debug={"enabled": True, "skip_stages": []}))
        m_set.assert_called_once_with(True)
        m_wait.assert_not_called()

    def test_normal_mode_blocks(self):
        with (
            mock.patch.object(main_mod.runtime_mod, "set_paused") as m_set,
            mock.patch.object(main_mod, "wait_if_paused") as m_wait,
        ):
            main_mod._pause_until_resume(None)
        m_set.assert_called_once_with(True)
        m_wait.assert_called_once()

    def test_no_cfg_still_works(self):
        """不传 cfg 也不能崩(原实现裸读 cfg 是未定义名)。"""
        with (
            mock.patch.object(main_mod.runtime_mod, "set_paused"),
            mock.patch.object(main_mod, "wait_if_paused"),
        ):
            main_mod._pause_until_resume()


if __name__ == "__main__":
    unittest.main(verbosity=2)
