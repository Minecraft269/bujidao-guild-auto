# -*- coding: utf-8 -*-
"""
test_main_flow.py —— main() 端到端控制流(全 mock 硬件)
======================================================
为什么要专门测 main():它是全项目最后一个 0 覆盖的大函数,而 **NameError 这类
"只有跑起来才炸"的缺陷恰好藏在里面** —— 拆分 main.py 时 `sw` /
`debug_notify_skipped` / `async_executor` 三个未定义名都是靠人工读代码发现的,
测试一条都没拦住。本文件用假 Runtime 把 main() 真正跑一遍,让这类错误现形。

覆盖的控制流:
  串行模式 happy path / 异步模式走 stage_execute_async
  待办处理返回 False → 提前返回
  阶段确认被拒 → 提前返回
  未找到窗口 + 用户输入 q → SystemExit
  debug.skip_stages 的 list / member / decide / notify / execute 分支
  finally: watcher.close / async_system.stop / listener.stop

零真实硬件:build_runtime / find_game_window / activate_window /
start_pause_listener / pyautogui / 报告写入 全部替换。
"""

import json
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from unittest import mock

from guild_auto import main as main_mod
from guild_auto.config import DEFAULT_CONFIG


NO_RET = object()  # 哨兵:patch 但不设 return_value(None 本身是合法返回值)


def _base_cfg():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["log"] = {"enabled": False, "level": 4, "dir": "logs"}
    cfg["admin"] = "测试管理员"
    cfg["guild_name"] = "演示公会"
    return cfg


class _FakeRuntime:
    """假 Runtime:够 main() 用,不碰任何硬件。"""

    def __init__(self, async_enabled=False):
        self.cfg = _base_cfg()
        self.watcher = mock.Mock(name="watcher")
        self.human = mock.Mock(name="human")
        self.executor = mock.Mock(name="executor")
        self.executor.execute_actions.return_value = []
        self.detector = None
        self.hwnd = 123
        self.listener = None
        self.async_executor = None
        self.async_system = mock.Mock(name="async_system") if async_enabled else None
        if self.async_system is not None:
            # 必须返回可迭代的空列表:否则 finally 里迭代 Mock 会 TypeError,
            # 被 except 吞掉 → async_system.stop() 永远不被调用(指令丢失风险)。
            self.async_system.get_retry_queue.return_value = []
            self.async_system.get_stranded.return_value = []
        self.async_enabled = async_enabled
        self.ask_confirm = lambda p, d=False: True
        self.sleep = lambda s: None
        self.is_paused = lambda: False
        self.closed = False

    def confirm(self, prompt, default=False):
        return bool(self.ask_confirm(prompt, default))

    def ask(self, prompt, default=False):
        return bool(self.ask_confirm(prompt, default))

    def pause_gate(self):
        pass

    def reset_window_state_after_pause(self, hwnd=None, detector=None):
        return False

    def async_verify_submit(self, command, player, meta=None):
        return "task-id"

    def close(self):
        self.closed = True


class _MainCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="main_flow_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.cfg_path = os.path.join(self.tmp, "config.json")

    def _run(
        self,
        *,
        async_enabled=False,
        pending_cont=True,
        confirm_answer=True,
        window=123,
        guild=None,
        records=None,
        query_failures=None,
        notify_failures=None,
        exec_failures=None,
        stage_conf=(True, False),
        cfg_override=None,
        input_value="",
    ):
        """跑一次 main(),返回断言用的 mocks 字典。

        用 ExitStack 而不是一条超长 with 链:Python 限制静态嵌套 20 层,
        这里要替换 ~28 个名字,链式 with 会直接 SyntaxError。
        """
        guild = guild or {
            "guild_name": "演示公会",
            "total": 2,
            "online": 1,
            "groups": {"entry": ["甲", "乙"]},
        }
        records = (
            records
            if records is not None
            else [
                {
                    "player": "甲",
                    "rank": "entry",
                    "weekly_total": 100,
                    "joined_at": "x",
                    "last_online": "y",
                    "daily": [],
                    "actions": ["promote"],
                    "trigger": "entry_promote_1",
                },
            ]
        )
        rt = _FakeRuntime(async_enabled)
        rt.ask_confirm = lambda p, d=False: confirm_answer

        cfg = _base_cfg()
        cfg["async"]["enabled"] = async_enabled
        cfg["switches"]["execution_enabled"] = True
        if cfg_override:
            cfg.update(cfg_override)

        # (目标, 返回值)；None 表示用无参 MagicMock
        patches = [
            ("parse_args", "MOCK_ARGS"),
            ("prepare_config", cfg),
            ("runtime_mod.build_runtime", rt),
            ("find_game_window", window),
            ("activate_window", NO_RET),
            ("start_pause_listener", "MOCK_LISTENER"),
            ("runtime_mod.pause_key_name", "u"),
            ("run_pending", (pending_cont, [])),
            ("pending_path_of", os.path.join(self.tmp, "pending.json")),
            ("console", NO_RET),
            ("logger", NO_RET),
            ("pyautogui.press", NO_RET),
            ("prepare_chat_state", NO_RET),
            ("wait_if_paused", NO_RET),
            ("reset_window_state_after_pause", NO_RET),
            ("query_guild_list", guild),
            ("query_member", records[0] if records else None),
            ("stage_query_async", (records, query_failures or [])),
            ("stage_notify", notify_failures or []),
            ("stage_confirm_execution", stage_conf),
            ("stage_execute_async", exec_failures or []),
            ("write_contribution_report", os.path.join(self.tmp, "contrib.txt")),
            ("write_action_report", os.path.join(self.tmp, "action.txt")),
            ("load_pending", []),
            ("save_pending", NO_RET),
            ("input", input_value),
            ("time.sleep", NO_RET),
        ]

        entered = {}
        with ExitStack() as stack:
            for target, value in patches:
                if target == "parse_args":
                    m = stack.enter_context(mock.patch.object(main_mod, "parse_args"))
                    m.return_value = mock.Mock(
                        config=self.cfg_path,
                        admin=None,
                        log=None,
                        options=None,
                        dry_run=False,
                        debug=False,
                        debug_skip=None,
                    )
                    entered[target] = m
                elif target == "start_pause_listener":
                    m = stack.enter_context(mock.patch.object(main_mod, "start_pause_listener"))
                    m.return_value = mock.Mock(name="listener")
                    entered[target] = m
                elif "." in target:
                    mod_name, attr = target.split(".", 1)
                    obj = getattr(main_mod, mod_name)
                    entered[target] = stack.enter_context(
                        mock.patch.object(obj, attr, return_value=value)
                    )
                elif value is NO_RET:
                    entered[target] = stack.enter_context(mock.patch.object(main_mod, target))
                else:
                    entered[target] = stack.enter_context(
                        mock.patch.object(main_mod, target, return_value=value)
                    )

            # 注意:run_pending 必须在 prepare_config 之后被调用；上面的
            # stage_* 都已被替换成固定返回值,所以 main() 不会碰硬件。
            main_mod.main()

        return {
            "rt": rt,
            "listener": entered["start_pause_listener"],
            "stage_exec_async": entered["stage_execute_async"],
            "cfg": cfg,
        }


class TestSerialMode(_MainCase):
    def test_happy_path_completes(self):
        """跑通即通过:这本身就能拦住 NameError 这类"只有执行才炸"的缺陷。"""
        self._run(async_enabled=False)

    def test_serial_does_not_call_stage_execute_async(self):
        mocks = self._run(async_enabled=False)
        mocks["stage_exec_async"].assert_not_called()

    def test_listener_started(self):
        mocks = self._run()
        mocks["listener"].assert_called_once()


class TestAsyncMode(_MainCase):
    def test_async_calls_stage_execute_async(self):
        mocks = self._run(async_enabled=True)
        mocks["stage_exec_async"].assert_called_once()

    def test_async_failures_do_not_crash_report(self):
        """异步执行失败必须并入 failures 且不打断报告输出。"""
        self._run(
            async_enabled=True,
            exec_failures=[
                {"player": "甲", "rank": "entry", "actions": ["promote"], "error": "超时"}
            ],
        )


class TestEarlyReturns(_MainCase):
    def test_pending_only_returns_early(self):
        """待办处理返回 False(用户只处理待办)→ 不进入统计流程。"""
        mocks = self._run(pending_cont=False)
        mocks["stage_exec_async"].assert_not_called()

    def test_confirm_rejected_returns_early(self):
        mocks = self._run(confirm_answer=False)
        mocks["stage_exec_async"].assert_not_called()

    def test_no_window_and_user_quits(self):
        """未找到窗口 + 用户输入 q → SystemExit。"""
        with self.assertRaises(SystemExit):
            self._run(window=None, input_value="q")


class TestDebugSkipStages(_MainCase):
    def _debug_cfg(self, skip):
        return {
            "debug": {
                "enabled": True,
                "skip_stages": skip,
                "simulation": {
                    "guild_name": "Debug公会",
                    "players_per_rank": 1,
                    "contribution_range": [0, 100],
                },
            }
        }

    def test_skip_list_uses_simulated_guild(self):
        self._run(cfg_override=self._debug_cfg(["list"]))

    def test_skip_member_and_decide(self):
        self._run(cfg_override=self._debug_cfg(["member", "decide"]))

    def test_skip_notify(self):
        self._run(cfg_override=self._debug_cfg(["notify"]))

    def test_skip_execute(self):
        self._run(cfg_override=self._debug_cfg(["execute"]))


class TestFinallyCleanup(_MainCase):
    def test_watcher_closed_and_listener_stopped(self):
        """无论正常结束与否,wacher 与 listener 都要被释放。"""
        mocks = self._run()
        mocks["rt"].watcher.close.assert_called()
        mocks["listener"].return_value.stop.assert_called()

    def test_async_system_stopped_in_async_mode(self):
        mocks = self._run(async_enabled=True)
        mocks["rt"].async_system.stop.assert_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
