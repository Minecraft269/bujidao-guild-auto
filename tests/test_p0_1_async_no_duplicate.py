# -*- coding: utf-8 -*-
"""
test_p0_1_async_no_duplicate.py —— P0-1 验收:异步模式不得重复执行破坏性命令
==========================================================================
每条用例都是**最小复现脚本**:直接 `pytest tests/test_p0_1_async_no_duplicate.py -v`
即可。每个 docstring 写明「修复前 → 修复后」的可观察差异,即同一条断言在修复前失败。

覆盖核对表第十三节条目:
  R-01 异步执行共用日志游标 → 后发任务回显被前一条读走(假失败 → 重发升/降/踢)
  R-04 成功回显只取窗口首条消息 → 面板前有聊天即判失败
  R-05 wait_all 超时固定 task_timeout*2 → 不随任务数增长
  R-06 局部 AsyncVerifySystem 中断时无人 stop → 后台线程继续发送
  R-07 踢出「窗口内唯一一条踢出成功」在异步并发下不成立
  R-08 同窗口内别人的错误回显把本次已成功判成失败
  R-03 含 kick 的混合动作列表绕过二次确认
  验收#2 异步一轮(含失败重试)中,同一玩家同一动作最多发送一次
"""

import json
import time
import unittest
from unittest import mock

from guild_auto.application import stages as stages_mod
from guild_auto.config import DEFAULT_CONFIG
from guild_auto.concurrency.async_system import AsyncVerifySystem, run_async_round
from guild_auto.infrastructure.action_executor import ActionExecutor

PANEL_START = "--------------------公会---------------------"
PANEL_END = "--------------------------------------------"
_TS = "[148月2026 21:55:41.046] [Render thread/INFO] "


def _panel(*body_lines):
    """真实形态的公会面板块(整块是一条物理行 + 内部字面 \\n)。"""
    return [
        _TS
        + "[net.minecraft.client.gui.components.ChatComponent/]: [CHAT] "
        + "\\n".join([PANEL_START, *body_lines, PANEL_END])
        + " [C]"
    ]


def _chat(text):
    """公屏聊天行(不是面板块)。"""
    return (
        _TS
        + "[net.minecraft.client.gui.components.ChatComponent/]: [CHAT] "
        + f"公会 > 某人: {text}"
    )


def _sys_chat(text):
    """服务端系统回显行(无玩家前缀),如「踢出成功」「玩家不存在」。"""
    return _TS + "[net.minecraft.client.gui.components.ChatComponent/]: [CHAT] " + text


def _cfg(**over):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["log"] = {"enabled": False, "level": 4, "dir": "logs"}
    cfg["timeouts"]["error_check_window"] = 0.2
    cfg["timeouts"]["max_retries"] = 2
    cfg["switches"]["kick_confirm"] = True
    cfg["kick_confirm_text"] = "kick-confirm"
    cfg.update(over)
    return cfg


class _CursorWatcher:
    """模拟真实 LogWatcher 的**共享游标**:每次 read_until 返回下一批行,读过就没了。

    这正是 R-01 的机制:谁先读谁拿走。用 list-of-batches 精确控制"哪一批被谁读走"。
    """

    def __init__(self, batches):
        self._batches = list(batches)
        self.reads = 0

    def read_until(self, timeout=None):
        self.reads += 1
        if self._batches:
            return list(self._batches.pop(0))
        return []

    def report_parse_error(self, exc, lines):  # 兼容 _guild_block_lines 的诊断钩子
        return None


def _executor(watcher, cfg=None):
    return ActionExecutor(
        cfg=cfg or _cfg(),
        human_input=mock.Mock(),
        watcher=watcher,
        hwnd=None,
        detector=None,
        stop_check=lambda: False,
    )


class _FakeRT:
    """stage_* 用的最小 Runtime(零真实硬件)。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.async_enabled = True
        self.human = mock.Mock()
        self.executor = mock.Mock()
        self.executor.send_gated.return_value = True
        self.executor.verify_action.return_value = (True, None)
        self.executor.gc_broadcast.return_value = True
        self.detector = None
        self.hwnd = None
        self.confirm_context = None
        self.asked = []
        self.asked_text = []

    def pause_gate(self):
        return None

    def is_paused(self):
        return False

    def reset_window_state_after_pause(self, hwnd=None, detector=None):
        return False

    def ask(self, prompt, default=True):
        self.asked.append(prompt)
        return True

    def ask_text(self, prompt):
        self.asked_text.append(prompt)
        return "不是确认词"


def _record(player="甲", actions=None, weekly=100, rank="entry"):
    return {
        "player": player,
        "rank": rank,
        "weekly_total": weekly,
        "joined_at": "x",
        "last_online": "y",
        "daily": [],
        "actions": list(actions or []),
    }


# ---------------------------------------------------------------------------
# R-04 / R-01 / R-07 / R-08:回显归属
# ---------------------------------------------------------------------------
class TestEchoAttribution(unittest.TestCase):
    def test_r04_panel_after_chat_is_still_found(self):
        """R-04:窗口内先到一条普通聊天、后到面板。

        修复前:只对 messages[0] 取面板 → 面板被丢掉 → 判失败 → 重发升/降/踢。
        修复后:扫描窗口内全部消息的面板块 → 判成功。
        """
        ex = _executor(_CursorWatcher([]))
        lines = [_chat("随便说句话")] + _panel("成功设置演示甲的职位为管理员!")
        self.assertTrue(ex._guild_block_lines(lines), "面板在第二条消息时被丢掉了(R-04)")

    def test_r01_second_task_echo_is_not_swallowed(self):
        """R-01:第一条验证的窗口里已经带回了第二条的回显(共享游标)。

        修复前:两个任务各读一个窗口,甲的窗口把乙的回显一起读走 →
        乙读不到 → 假失败 → 重发(升职/踢出被重复执行)。
        修复后:一轮共用一本账本,乙从账本里认领自己的回显。
        """
        both = _panel("成功设置演示甲的职位为管理员!") + _panel("成功设置演示乙的职位为管理员!")
        ex = _executor(_CursorWatcher([both]))  # 第一次读就把两条都拿走了
        ex.begin_verify_round(total_tasks=2)
        self.assertEqual(ex.verify_action("promote", "演示甲", timeout=0.2), (True, None))
        self.assertEqual(
            ex.verify_action("promote", "演示乙", timeout=0.2),
            (True, None),
            "乙的回显被甲的窗口吞掉了(R-01)",
        )
        ex.end_verify_round()

    def test_r01_echo_is_claimed_only_once(self):
        """同一行回显只能认领一次:promote×2 只回来一条 → 只算一级。

        修复前:同一面板会被后面的任务再认领一次(重复计数)。
        修复后:账本按条认领,第二条必须等自己的回显。
        """
        ex = _executor(_CursorWatcher([_panel("成功设置演示甲的职位为管理员!")]))
        ex.begin_verify_round(total_tasks=2)
        self.assertEqual(ex.verify_action("promote", "演示甲", timeout=0.1), (True, None))
        ok, err = ex.verify_action("promote", "演示甲", timeout=0.1)
        self.assertFalse(ok, "同一条回显被认领了两次(等级会被算高)")
        self.assertIn("未收到", err)
        ex.end_verify_round()

    def test_r07_two_unnamed_kick_echoes_are_attributed_fifo(self):
        """R-07:异步下窗口内有 2 条「踢出成功」且都不带玩家名。

        修复前:「窗口内唯一一条」才归因,2 条 → 一律判失败 → 重发踢人。
        修复后:按先进先出各认领一条,两条都算成功(且各自只认领一次)。
        """
        lines = [_sys_chat("踢出成功"), _sys_chat("踢出成功")]
        ex = _executor(_CursorWatcher([lines]))
        ex.begin_verify_round(total_tasks=2)
        # 系统回显形态:strip_chat_prefix 后整行就是「踢出成功」
        self.assertEqual(ex.verify_action("kick", "演示甲", timeout=0.2), (True, None))
        self.assertEqual(ex.verify_action("kick", "演示乙", timeout=0.2), (True, None))
        ok, _err = ex.verify_action("kick", "演示丙", timeout=0.1)
        self.assertFalse(ok, "只有 2 条回显却归因给了 3 个踢出任务")
        ex.end_verify_round()

    def test_r08_foreign_error_does_not_fail_successful_action(self):
        """R-08:同一窗口里既有自己的成功面板、又有别人命令的错误回显。

        修复前:先扫错误 → 命中「玩家不存在」直接判失败(与成功面板矛盾)。
        修复后:先认成功;错误只在"本轮只剩这一条"时才归因。
        """
        lines = _panel("成功设置演示甲的职位为管理员!") + [_chat("玩家不存在")]
        ex = _executor(_CursorWatcher([lines]))
        ex.begin_verify_round(total_tasks=2)
        self.assertEqual(
            ex.verify_action("promote", "演示甲", timeout=0.2, pending=2), (True, None)
        )
        ex.end_verify_round()


# ---------------------------------------------------------------------------
# 验收#2:一轮内同一玩家同一动作最多发送一次
# ---------------------------------------------------------------------------
class TestSendAtMostOnce(unittest.TestCase):
    def test_retry_never_resends(self):
        """失败重试只重新验证,**不再发送**。

        修复前:第二轮 submit_many 会把失败命令再发一次 → 每条发 2 次
        (升职升两级 / 踢人踢两次)。
        修复后:第二轮 reverify 只把任务重新排进验证队列,send_fn 调用次数不变。
        """
        sends = []
        verify_calls = {}

        def send(cmd, player, meta):
            sends.append((cmd, player))
            return True

        def verify(cmd, player, meta):
            n = verify_calls.get(player, 0) + 1
            verify_calls[player] = n
            return (n > 1, None if n > 1 else "第一次没拿到回显", None)

        system = AsyncVerifySystem(verify_fn=verify, poll_interval=0.01, task_timeout=0.5)
        ok, failed = run_async_round(
            system,
            [("cmd-甲", "甲"), ("cmd-乙", "乙")],
            send,
            max_retries=3,
            resend_on_retry=False,
        )
        system.stop()
        self.assertEqual(len(sends), 2, f"每条命令应只发一次,实际 {sends}")
        self.assertEqual(len(ok), 2, "第二轮重新验证后应成功")
        self.assertEqual(failed, [])

    def test_stage_execute_uses_send_once_policy(self):
        """执行阶段必须传 resend_on_retry=False(查询阶段保留重发语义)。"""
        cfg = _cfg()
        rt = _FakeRT(cfg)
        seen = {}

        def fake_round(system, commands, send_fn, max_retries=3, resend_on_retry=True):
            seen["resend"] = resend_on_retry
            seen["n"] = len(commands)
            return [], []

        with mock.patch("guild_auto.concurrency.async_system.run_async_round", fake_round):
            stages_mod.stage_execute_async(rt, [_record(actions=["kick"])], cfg)
        self.assertFalse(seen["resend"], "执行阶段重试不得重发(否则重复踢人)")
        self.assertEqual(seen["n"], 1)

    def test_reverify_ignores_unknown_or_non_failed_tasks(self):
        """reverify 只重排 FAILED 任务;未知/已在途的任务原样跳过,返回 0。"""
        system = AsyncVerifySystem(verify_fn=lambda *a: (True, None, None), task_timeout=0.2)
        self.assertEqual(system.reverify([999]), 0, "未知 task_id 不该被重新排队")
        tid = system.submit("cmd", "甲", {})
        # 未发送(PENDING)的任务不是 FAILED,也不该被 reverify 动
        self.assertEqual(system.reverify([tid]), 0)
        system.stop()

    def test_send_without_send_fn_marks_sent(self):
        """未注入 send_fn(单测/降级)时只记账不真发,任务仍能被验证线程收尾。"""
        system = AsyncVerifySystem(verify_fn=None, poll_interval=0.01, task_timeout=0.2)
        system.start()
        system.submit("cmd", "甲", {})
        self.assertTrue(system.wait_all(timeout=2.0))
        self.assertEqual([s["state"] for s in system.get_completed()], ["ok"])
        system.stop()


# ---------------------------------------------------------------------------
# R-05:wait_all 超时随任务数线性增长
# ---------------------------------------------------------------------------
class TestWaitAllTimeout(unittest.TestCase):
    def test_formula_is_linear(self):
        """公式:timeout = (max(1, in_flight) + 1) * task_timeout。

        修复前固定 task_timeout*2,与在途任务数无关。
        """
        self.assertEqual(AsyncVerifySystem.implicit_timeout(10.0, 1), 20.0)
        self.assertEqual(AsyncVerifySystem.implicit_timeout(10.0, 5), 60.0)
        self.assertEqual(AsyncVerifySystem.implicit_timeout(10.0, 200), 2010.0)
        # 线性:每多一条在途任务,上限恰好增加一个 task_timeout
        base = AsyncVerifySystem.implicit_timeout(2.0, 3)
        nxt = AsyncVerifySystem.implicit_timeout(2.0, 4)
        self.assertAlmostEqual(nxt - base, 2.0)

    def test_five_tasks_no_longer_time_out(self):
        """实测:5 条任务、每条验证 0.15s(共 0.75s)。

        修复前上限 = 0.2*2 = 0.4s → 超时,后台随后成功的任务被记"最终失败"。
        修复后上限 = (5+1)*0.2 = 1.2s → 全部成功。
        """

        def verify(cmd, player, meta):
            time.sleep(0.15)
            return True, None, None

        system = AsyncVerifySystem(verify_fn=verify, poll_interval=0.01, task_timeout=0.2)
        ok, failed = run_async_round(
            system,
            [(f"cmd{i}", f"P{i}") for i in range(5)],
            lambda c, p, m: True,
            max_retries=1,
            resend_on_retry=False,
        )
        system.stop()
        self.assertEqual(len(ok), 5, f"应有 5 条成功,实际 ok={len(ok)} failed={len(failed)}")
        self.assertEqual(failed, [])


# ---------------------------------------------------------------------------
# R-06:中断时必须停掉局部系统
# ---------------------------------------------------------------------------
class TestInterruptStopsSystem(unittest.TestCase):
    def test_stage_execute_stops_local_system_on_interrupt(self):
        """KeyboardInterrupt 逃出阶段函数时,局部 AsyncVerifySystem 必须已 stop。

        修复前:没有 try/finally → 后台 sender/verifier 线程继续跑,继续发命令,
        而这些命令既不进报告也不进待办(下次启动还会从待办再执行一次)。
        修复后:finally 里 stop(),线程退出、在途任务进补做队列。
        """
        cfg = _cfg()
        rt = _FakeRT(cfg)
        seen = {}

        def boom(system, commands, send_fn, **kw):
            seen["system"] = system
            system.start()
            raise KeyboardInterrupt

        with mock.patch("guild_auto.concurrency.async_system.run_async_round", boom):
            with self.assertRaises(KeyboardInterrupt):
                stages_mod.stage_execute_async(rt, [_record(actions=["promote"])], cfg)

        system = seen["system"]
        self.assertTrue(system.is_stopped(), "中断后后台系统未停止(R-06)")
        self.assertFalse(system.is_alive(), "中断后后台线程仍在运行(R-06)")

    def test_stage_query_stops_local_system_on_interrupt(self):
        cfg = _cfg()
        seen = {}
        rt = mock.Mock()
        rt.async_enabled = True
        rt.watcher = _CursorWatcher([])
        rt.human = mock.Mock()
        rt.executor = mock.Mock()
        rt.detector = None

        def boom(system, commands, send_fn, **kw):
            seen["system"] = system
            system.start()
            raise KeyboardInterrupt

        with mock.patch("guild_auto.concurrency.async_system.run_async_round", boom):
            with self.assertRaises(KeyboardInterrupt):
                stages_mod.stage_query_async(rt, [("entry", "甲")], cfg)
        self.assertTrue(seen["system"].is_stopped(), "查询阶段中断后未 stop(R-06)")


# ---------------------------------------------------------------------------
# R-03:含 kick 的混合动作列表必须走二次确认
# ---------------------------------------------------------------------------
class TestMixedKickNeedsSecondConfirm(unittest.TestCase):
    def test_mixed_list_is_confirmed_and_kick_removed_on_mismatch(self):
        """["demote","kick"] 必须弹二次确认;确认词不对时只摘掉 kick。

        修复前:只按 actions[0] 找 kick → 混合列表 ask_text 从未被调用 →
        踢出直接执行(不可逆)。
        修复后:二次确认被触发;错误确认词下 kick 被摘除,demote 保留。
        """
        cfg = _cfg()
        rt = _FakeRT(cfg)
        rec = _record(actions=["demote", "kick"])
        enabled, skipped = stages_mod.stage_confirm_execution(rt, [rec], cfg)
        self.assertTrue(enabled)
        self.assertTrue(skipped, "含 kick 的混合列表没有走二次确认(R-03)")
        self.assertEqual(len(rt.asked_text), 1, "二次确认提示未被触发")
        self.assertNotIn("kick", rec["actions"], "未经确认的踢出仍在动作列表里")
        self.assertIn("demote", rec["actions"], "已确认的降职被误删")
        self.assertTrue(rec.get("kick_skipped"))

    def test_mixed_list_keeps_kick_with_correct_word(self):
        cfg = _cfg()
        rt = _FakeRT(cfg)
        rt.ask_text = lambda prompt: "kick-confirm"
        rec = _record(actions=["demote", "kick"])
        enabled, skipped = stages_mod.stage_confirm_execution(rt, [rec], cfg)
        self.assertTrue(enabled)
        self.assertFalse(skipped)
        self.assertEqual(rec["actions"], ["demote", "kick"])

    def test_pure_kick_still_skipped_whole_record(self):
        """纯 kick 的行为不变:跳过时整条标 executed(不得下次补踢)。"""
        cfg = _cfg()
        rt = _FakeRT(cfg)
        rec = _record(actions=["kick"])
        _enabled, skipped = stages_mod.stage_confirm_execution(rt, [rec], cfg)
        self.assertTrue(skipped)
        self.assertTrue(rec["executed"])
        self.assertTrue(rec["skipped_by_confirm"])
        self.assertEqual(rec["actions"], [])


# ---------------------------------------------------------------------------
# R-02:通知回显的判定口径(与串行 gc_broadcast_with_retry 一致)
# ---------------------------------------------------------------------------
class TestBroadcastNotifyVerify(unittest.TestCase):
    def test_verify_finds_plain_chat_echo(self):
        """修复前:等公会的面板块 → /gc 的普通聊天回显永远拿不到块 → 恒判失败。

        修复后:窗口内出现含广播正文的 [CHAT] 行即成功(与串行同一口径)。
        """
        rt = mock.Mock()
        rt.watcher.read_until.return_value = [_sys_chat("公告正文"), _chat("闲聊")]
        verify = stages_mod._broadcast_verify(rt, 0.1)
        self.assertEqual(verify("/gc 公告正文", "summary", {"msg": "公告正文"}), (True, None, None))

    def test_verify_reports_error_pattern(self):
        rt = mock.Mock()
        rt.executor.error_patterns = ["权限不足"]
        rt.watcher.read_until.return_value = [_sys_chat("权限不足")]
        ok, err, _data = stages_mod._broadcast_verify(rt, 0.1)("/gc x", "s", {"msg": "x"})
        self.assertFalse(ok)
        self.assertEqual(err, "权限不足")

    def test_verify_timeout_without_echo(self):
        rt = mock.Mock()
        rt.executor.error_patterns = []
        rt.watcher.read_until.return_value = []
        ok, err, _data = stages_mod._broadcast_verify(rt, 0.1)("/gc x", "s", {"msg": "x"})
        self.assertFalse(ok)
        self.assertIn("未在日志中回显", err)

    def test_send_fn_goes_through_executor_gate(self):
        """发送器只发不验,并走 executor.gc_broadcast(统一门禁)。"""
        rt = mock.Mock()
        rt.executor.gc_broadcast.return_value = True
        send = stages_mod._broadcast_send_fn(rt)
        self.assertTrue(send("/gc 正文", "summary", {"msg": "正文"}))
        rt.executor.gc_broadcast.assert_called_once_with("正文")
        self.assertFalse(send("/gc 正文", "summary", {}), "meta 丢正文时不得凭空发送")

    def test_new_notify_system_runs_and_stops(self):
        """真实构造的通知系统:提交 → 验证 → stop 后线程退出。"""
        rt = mock.Mock()
        rt.watcher.read_until.return_value = [_sys_chat("正文")]
        system = stages_mod._new_notify_system(rt, _cfg())
        try:
            system.submit("/gc 正文", "summary", {"msg": "正文"})
            self.assertTrue(system.wait_all(timeout=3.0))
        finally:
            system.stop()
        self.assertFalse(system.is_alive())


# ---------------------------------------------------------------------------
# 账本辅助路径(R-08 单人归因 / 缓存 / 门禁返回值)
# ---------------------------------------------------------------------------
class TestRoundHelpers(unittest.TestCase):
    def test_r08_error_attributed_when_single_pending(self):
        """只剩一条待判定时,错误回显才允许归因(无歧义)。"""
        ex = _executor(_CursorWatcher([[_sys_chat("玩家不存在")]]))
        ex.begin_verify_round(total_tasks=1)
        ok, err = ex.verify_action("kick", "演示甲", timeout=0.1, pending=1)
        self.assertFalse(ok)
        self.assertEqual(err, "玩家不存在")
        ex.end_verify_round()

    def test_round_snapshot_is_cached_between_reads(self):
        ex = _executor(_CursorWatcher([]))
        ex.begin_verify_round(1)
        first = ex._round_snapshot()
        second = ex._round_snapshot()
        self.assertEqual(first, second)
        ex.end_verify_round()

    def test_send_gated_returns_human_result(self):
        """send_gated 透传 human.send_chat_text 的结果(False=暂停取消,未发出)。"""
        ex = _executor(_CursorWatcher([]))
        ex.input.send_chat_text.return_value = False
        self.assertFalse(ex.send_gated("/guild kick 演示甲"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
