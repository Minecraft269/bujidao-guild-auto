# -*- coding: utf-8 -*-
"""
test_actions_logic.py —— 决策/文案/命令边界补齐 + 硬件层 mock 覆盖
==================================================================
对应硬性验收标准:
  * 核心层 ≥95%:补齐 actions.py 的纯逻辑缺口
    (new_rank_after 未知等级兜底 / 等级链封顶封底 / build_command 未知动作 /
     build_notification 边界)
  * 硬件交互层 mock 覆盖 + 单独报告:
    ActionExecutor 的发送(暂停门/重置)、日志错误回显检测、成功回显检测、重试、
    广播 —— 全部用 mock 隔离 pyautogui 与日志文件,真实设备调用不计入分母

【mock 策略声明】
  * human_input(HumanInput) → MagicMock:send_chat_text 只记录调用,不碰剪贴板/键盘
  * watcher(LogWatcher)      → MagicMock:read_until 返回预置行,不读真实 latest.log
  * hwnd=None                → 跳过窗口激活(ctypes 调用)
  * detector=None            → 跳过 OCR 截图
  * time.sleep               → patch 为空操作,测试秒级完成且无真实等待
  这样硬件交互层的分支逻辑与异常路径都能被确定性验证,且不产生真实副作用。
"""

import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch


from guild_auto.infrastructure import logger  # noqa: E402
from guild_auto.domain.decisions import (  # noqa: E402
    RANK_ORDER,
    build_command,
    build_notification,
    new_rank_after,
)
from guild_auto.infrastructure.action_executor import ActionExecutor  # noqa: E402
from guild_auto.config import DEFAULT_CONFIG  # noqa: E402

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

CFG = DEFAULT_CONFIG


class TestNewRankAfterUnknownRank(unittest.TestCase):
    """未知等级的安全兜底(补 actions.py 99-108 缺口)。

    业务风险:配置里 rank_names 写错导致玩家等级键不在链上时,
    绝不能抛异常中断整个清人流程,更不能算出错误的新等级。
    """

    def test_unknown_rank_no_action_returns_unchanged(self):
        self.assertEqual(new_rank_after("unknown_rank", []), "unknown_rank")

    def test_unknown_rank_promote_returns_unchanged(self):
        """未知等级 + 升职:不得越界猜等级,原样返回。"""
        self.assertEqual(new_rank_after("unknown_rank", ["promote"]), "unknown_rank")

    def test_unknown_rank_promote_multiple_returns_unchanged(self):
        self.assertEqual(new_rank_after("bogus", ["promote"] * 3), "bogus")

    def test_unknown_rank_demote_returns_unchanged(self):
        self.assertEqual(new_rank_after("unknown_rank", ["demote"]), "unknown_rank")

    def test_empty_rank_demote_returns_unchanged(self):
        self.assertEqual(new_rank_after("", ["demote"]), "")

    def test_none_rank_returns_none(self):
        self.assertIsNone(new_rank_after(None, ["promote"]))

    def test_unknown_action_kind_keeps_rank(self):
        """未知的动作名(如配置写错)不得改变等级。"""
        self.assertEqual(new_rank_after("mid", ["unknown_action"]), "mid")


class TestNewRankAfterChainBounds(unittest.TestCase):
    """等级链封顶/封底(业务规则 §1:入门→低→中→高)。"""

    def test_chain_order_is_entry_low_mid_high(self):
        """等级链顺序是业务真源的一部分。"""
        self.assertEqual(RANK_ORDER, ["entry", "low", "mid", "high"])

    def test_promote_one_step_up(self):
        for i in range(len(RANK_ORDER) - 1):
            self.assertEqual(
                new_rank_after(RANK_ORDER[i], ["promote"]),
                RANK_ORDER[i + 1],
                f"{RANK_ORDER[i]} 升一级应变 {RANK_ORDER[i + 1]}",
            )

    def test_demote_one_step_down(self):
        for i in range(1, len(RANK_ORDER)):
            self.assertEqual(
                new_rank_after(RANK_ORDER[i], ["demote"]),
                RANK_ORDER[i - 1],
                f"{RANK_ORDER[i]} 降一级应变 {RANK_ORDER[i - 1]}",
            )

    def test_promote_beyond_top_capped(self):
        """超出链顶封顶,不越界(补 line 104 封顶分支)。"""
        self.assertEqual(new_rank_after("high", ["promote"]), "high")
        self.assertEqual(new_rank_after("mid", ["promote"] * 5), "high")

    def test_demote_below_bottom_floored(self):
        """低于链底封底,不越界(补 line 96 封底分支)。"""
        self.assertEqual(new_rank_after("entry", ["demote"]), "entry")


class TestBuildCommand(unittest.TestCase):
    """命令格式(业务规则 §2,零改动)。"""

    def test_player_with_space_in_command(self):
        """含空格的玩家名必须原样嵌入命令(真实样本存在"带空格 名字")。"""
        self.assertEqual(
            build_command(CFG, "promote", "带空格 演示名"), "/guild promote 带空格 演示名"
        )

    def test_query_command_not_via_build_command(self):
        """查询命令(/guild member)不经 build_command —— 它只管升/降/踢三类动作。

        这是真实契约:调用方(main)自行拼查询命令,build_command 见到未知动作
        应当抛错而非"猜"出一条指令 —— 猜错会把查询发成破坏性操作。"""
        with self.assertRaises(ValueError):
            build_command(CFG, "member", "演示甲")

    def test_promote_command(self):
        self.assertEqual(build_command(CFG, "promote", "演示甲"), "/guild promote 演示甲")

    def test_demote_command(self):
        self.assertEqual(build_command(CFG, "demote", "演示甲"), "/guild demote 演示甲")

    def test_kick_command_includes_reason(self):
        """踢出必须带原因文本(业务规则 §2)。"""
        self.assertEqual(
            build_command(CFG, "kick", "演示甲"), "/guild kick 演示甲 未完成一周最低标准"
        )

    def test_kick_reason_configurable(self):
        """踢出原因可配置。"""
        cfg = {
            "commands": {"guild_kick": "/guild kick {player} {reason}", "kick_reason": "自定义原因"}
        }
        self.assertEqual(build_command(cfg, "kick", "甲"), "/guild kick 甲 自定义原因")

    def test_unknown_action_raises(self):
        """未知动作必须抛 ValueError(补 line 146 缺口)——静默忽略会导致漏发指令。"""
        with self.assertRaises(ValueError) as cm:
            build_command(CFG, "teleport", "甲")
        self.assertIn("teleport", str(cm.exception))

    def test_player_with_space_in_command(self):
        """含空格的玩家名必须原样嵌入命令(真实样本存在"带空格 名字")。"""
        self.assertEqual(
            build_command(CFG, "promote", "带空格 演示名"), "/guild promote 带空格 演示名"
        )


class TestBuildNotificationEdge(unittest.TestCase):
    """通知文案边界(业务规则 §3,零改动)。"""

    def test_no_action_uses_none_template(self):
        msg = build_notification("演示甲", 100, [], CFG)
        self.assertIn("执行操作:无", msg)
        self.assertIn("演示甲", msg)
        self.assertIn("100", msg)

    def test_promote_count_defaults_to_actions_length(self):
        """{count} 默认取 actions 长度(原脚本 generate_notification 用 len(actions))。"""
        msg = build_notification("演示甲", 30000, ["promote", "promote"], CFG)
        self.assertIn("升职(2次)", msg)

    def test_promote_count_can_be_overridden(self):
        """显式传 count 时以传入值为准。"""
        msg = build_notification("演示甲", 30000, ["promote"], CFG, count=5)
        self.assertIn("升职(5次)", msg)

    def test_promote_three_times(self):
        msg = build_notification("演示甲", 40000, ["promote"] * 3, CFG)
        self.assertIn("升职(3次)", msg)

    def test_demote_text(self):
        msg = build_notification("演示甲", 100, ["demote"], CFG)
        self.assertIn("执行操作:降职", msg)
        self.assertIn("若有异议请去群里找执行此操作的管理员", msg)

    def test_kick_text(self):
        msg = build_notification("演示甲", 100, ["kick"], CFG)
        self.assertIn("执行操作:踢出", msg)
        self.assertIn("未完成这周最低标准", msg)
        self.assertIn("若有异议请去群里寻找执行管理员", msg)

    def test_guild_qq_placeholder_substituted(self):
        """群号占位符应被替换进文案。"""
        msg = build_notification("演示甲", 100, [], CFG)
        self.assertIn(str(CFG.get("guild_qq", "")), msg)

    def test_mixed_actions_uses_first_kind(self):
        """动作列表以第一个为准。"""
        msg = build_notification("演示甲", 100, ["demote", "kick"], CFG)
        self.assertIn("执行操作:降职", msg)

    def test_weekly_zero_rendered(self):
        """周贡献为 0 时也必须正确渲染(不能因 falsy 而走错分支)。"""
        msg = build_notification("演示甲", 0, [], CFG)
        self.assertIn("这周贡献为:0", msg)


PANEL_START = "--------------------公会---------------------"
PANEL_END = "--------------------------------------------"


def _panel(*body_lines):
    """把回显行包成**真实形态**的公会的会面板。

    为什么需要:executor._find_success 只认面板块内的 [CHAT] 内容
    (独立审查加固:公屏任意聊天不能当成功证据,否则可被伪造成"已踢出")。
    所以测试夹具也必须是真实形态 —— 裸 "[CHAT] 成功设置..." 不是
    服务端下发的样子,用它测通过只能证明"裸行能被识别",而那正是
    要防住的伪造路径。
    """
    return [
        f"[148月2026 21:55:41.046] [Render thread/INFO] "
        f"[net.minecraft.client.gui.components.ChatComponent/]: [CHAT] "
        + "\\n".join([PANEL_START, *body_lines, PANEL_END])
        + " [C]"
    ]


def _make_executor(read_until_lines=None, stop_check=None, detector=None, error_patterns=None):
    """构造一个全 mock 的 ActionExecutor(硬件层测试夹具)。

    绝不触碰真实键盘/剪贴板/日志文件:
      * human.send_chat_text → MagicMock(只记录)
      * watcher.read_until   → 返回预置行
      * hwnd=None            → 跳过窗口激活
      * detector=None        → 跳过 OCR
    """
    cfg = dict(CFG)
    cfg["error_patterns"] = list(
        error_patterns if error_patterns is not None else ["权限不足", "玩家不存在"]
    )
    ex = ActionExecutor(
        cfg=cfg,
        human_input=MagicMock(),
        watcher=MagicMock(),
        hwnd=None,
        detector=detector,
        stop_check=stop_check,
    )
    ex.watcher.read_until.return_value = list(read_until_lines or [])
    return ex


class TestActionExecutorSuccessDetection(unittest.TestCase):
    """成功回显检测(硬件层,mock 隔离)。"""

    @patch("time.sleep", return_value=None)
    def test_promote_success_detected(self, _s):
        """日志含"成功设置X的职位为Y!" → 判定成功。"""
        ex = _make_executor(_panel("成功设置演示甲的职位为管理员!"))
        ok, err = ex.execute_action("promote", "演示甲", retries=1)
        self.assertTrue(ok, f"升职成功回显未被识别:{err}")
        self.assertIsNone(err)

    @patch("time.sleep", return_value=None)
    def test_demote_success_detected(self, _s):
        ex = _make_executor(_panel("成功设置演示甲的职位为高活跃!"))
        ok, err = ex.execute_action("demote", "演示甲", retries=1)
        self.assertTrue(ok, f"降职成功回显未被识别:{err}")

    @patch("time.sleep", return_value=None)
    def test_kick_success_detected(self, _s):
        """踢出成功回显格式:{player}被{admin}踢出{guild}公会!"""
        ex = _make_executor(_panel("演示甲被管理员踢出演示公会公会！"))
        ok, err = ex.execute_action("kick", "演示甲", retries=1)
        self.assertTrue(ok, f"踢出成功回显未被识别:{err}")

    @patch("time.sleep", return_value=None)
    def test_other_player_success_not_matched(self, _s):
        """别人的成功回显不得误判为本玩家成功(防串号导致漏发重试)。"""
        ex = _make_executor(_panel("成功设置另一个人X的职位为管理员!"))
        ok, _ = ex.execute_action("promote", "演示甲", retries=1)
        self.assertFalse(ok, "把别人的回显误判为本玩家成功")

    @patch("time.sleep", return_value=None)
    def test_kick_other_player_not_matched(self, _s):
        ex = _make_executor(_panel("另一个人被管理员踢出演示公会公会！"))
        ok, _ = ex.execute_action("kick", "演示甲", retries=1)
        self.assertFalse(ok, "把别人的踢出回显误判为本玩家成功")

    @patch("time.sleep", return_value=None)
    def test_kick_echo_without_name_attributed_by_time_window(self, _s):
        """真实回显只有「踢出成功」、不含玩家名(plan 5.2)。串行单踢时窗口内唯一一条可归因;
        若只按名匹配,这种回显会被永远判失败并触发无意义重发。"""
        ex = _make_executor(["[CHAT] 踢出成功"])
        ok, err = ex.execute_action("kick", "演示甲", retries=1)
        self.assertTrue(ok, f"不含玩家名的「踢出成功」应能按时间窗归因:{err}")

    @patch("time.sleep", return_value=None)
    def test_kick_echo_forged_by_player_chat_rejected(self, _s):
        """玩家在频道里打「名字: 踢出成功」不得被判成功 —— 裸子串匹配可被任意聊天伪造
        (独立复查找出:时间窗兜底最初扫的是原始 lines)。"""
        ex = _make_executor(["[CHAT] <随机玩家> 随机玩家: 踢出成功"])
        ok, _ = ex.execute_action("kick", "演示甲", retries=1)
        self.assertFalse(ok, "聊天伪造的「踢出成功」不得当成功证据")

    @patch("time.sleep", return_value=None)
    def test_two_kick_echoes_are_not_attributable(self, _s):
        """窗口内出现两条「踢出成功」→ 无法归因,必须判失败(不能把别人的踢出算到本次)。"""
        ex = _make_executor(["[CHAT] 踢出成功", "[CHAT] 踢出成功"])
        ok, _ = ex.execute_action("kick", "演示甲", retries=1)
        self.assertFalse(ok, "两条踢出回显不得归因到单一玩家")

    @patch("time.sleep", return_value=None)
    def test_public_chat_forgery_not_accepted(self, _s):
        """**公屏伪造不得当成成功回显** —— 独立审查 high 级发现。

        攻击:任何玩家在公屏/公会频道打一句
            "演示甲被管理员踢出演示公会公会！"
        没有面板块包裹。旧实现只做裸子串匹配,会把它当成功证据
        → 命令实际没发出去却被记为已执行 → **漏踢**,
        而报告会说"已执行"(假成功比假失败危险)。

        _find_success 现在只认 _guild_block_lines 取出的面板块内容。
        """
        forged = "[148月2026 21:55:41.046] [Render thread/INFO] [Chat/]: [CHAT] 随机玩家: 演示甲被管理员踢出演示公会公会！"
        ex = _make_executor([forged])
        ok, _ = ex.execute_action("kick", "演示甲", retries=1)
        self.assertFalse(ok, "公屏伪造行被当成了踢出成功证据")

    @patch("time.sleep", return_value=None)
    def test_public_chat_forgery_not_accepted_for_promote(self, _s):
        """升职同理:公屏复读"成功设置..."不得当成功。"""
        forged = "[148月2026 21:55:41.046] [Render thread/INFO] [Chat/]: [CHAT] 昨天 成功设置演示甲的职位为管理员!"
        ex = _make_executor([forged])
        ok, _ = ex.execute_action("promote", "演示甲", retries=1)
        self.assertFalse(ok, "公屏伪造行被当成了升职成功证据")

    @patch("time.sleep", return_value=None)
    def test_real_panel_still_wins_over_forgery(self, _s):
        """真实面板与伪造行同时存在时,以面板为准(识别成功)。"""
        real = _panel("演示甲被管理员踢出演示公会公会！")[0]
        forged = "[148月2026 21:55:41.046] [Render thread/INFO] [Chat/]: [CHAT] 另一句闲聊"
        ex = _make_executor([real, forged])
        ok, _ = ex.execute_action("kick", "演示甲", retries=1)
        self.assertTrue(ok, "真实面板回显应被正常识别(不该被伪造行干扰)")


class TestActionExecutorErrorDetection(unittest.TestCase):
    """错误回显检测与重试(硬件层,mock 隔离)。"""

    @patch("time.sleep", return_value=None)
    def test_error_pattern_detected(self, _s):
        """日志含配置的错误模式 → 判定失败并返回错误文本。"""
        ex = _make_executor(["[CHAT] 权限不足,无法执行该操作"])
        ok, err = ex.execute_action("promote", "演示甲", retries=1)
        self.assertFalse(ok)
        self.assertEqual(err, "权限不足")

    @patch("time.sleep", return_value=None)
    def test_error_triggers_retry(self, _s):
        """有错误回显时应重试到耗尽。"""
        ex = _make_executor(["[CHAT] 玩家不存在"])
        ok, err = ex.execute_action("promote", "演示甲", retries=3)
        self.assertFalse(ok)
        self.assertEqual(err, "玩家不存在")
        self.assertEqual(
            ex.input.send_chat_text.call_count,
            3,
            f"应重试 3 次,实际 {ex.input.send_chat_text.call_count} 次",
        )

    @patch("time.sleep", return_value=None)
    def test_no_echo_retries_then_exhausts(self, _s):
        """无任何回显 → 重试到耗尽,返回"重试耗尽"。"""
        ex = _make_executor([])
        ok, err = ex.execute_action("promote", "演示甲", retries=2)
        self.assertFalse(ok)
        self.assertEqual(err, "重试耗尽")
        self.assertEqual(ex.input.send_chat_text.call_count, 2)

    @patch("time.sleep", return_value=None)
    def test_error_then_success_stops_early(self, _s):
        """先失败后成功:应提前结束,不再重试。"""
        w = MagicMock()
        w.read_until.side_effect = [
            ["[CHAT] 权限不足"],
            ["[CHAT] 权限不足"],
            _panel("成功设置演示甲的职位为管理员!"),
        ]
        cfg = dict(CFG)
        cfg["error_patterns"] = ["权限不足"]
        ex = ActionExecutor(
            cfg=cfg, human_input=MagicMock(), watcher=w, hwnd=None, detector=None, stop_check=None
        )
        ok, err = ex.execute_action("promote", "演示甲", retries=5)
        self.assertTrue(ok, f"重试后成功应判定成功:{err}")
        self.assertEqual(ex.input.send_chat_text.call_count, 3, "成功后不应继续重试")

    @patch("time.sleep", return_value=None)
    def test_non_chat_lines_ignored(self, _s):
        """非 [CHAT] 行不参与错误/成功判定。"""
        ex = _make_executor(["[Render thread/INFO] 一些无关日志"])
        ok, _ = ex.execute_action("promote", "演示甲", retries=1)
        self.assertFalse(ok, "非聊天行被误判为成功")


class TestActionExecutorPauseGate(unittest.TestCase):
    """暂停门:暂停期间不得发出任何指令(硬件层,mock 隔离)。"""

    @patch("time.sleep", return_value=None)
    def test_resume_after_pause_sends(self, _s):
        """暂停解除后应正常发送。"""
        paused = {"v": False}
        ex = _make_executor(_panel("成功设置演示甲的职位为管理员!"), stop_check=lambda: paused["v"])
        ok, err = ex.execute_action("promote", "演示甲", retries=1)
        self.assertTrue(ok, f"暂停解除后应能发送:{err}")
        self.assertEqual(ex.input.send_chat_text.call_count, 1)

    @patch("time.sleep", return_value=None)
    def test_stop_check_none_means_never_paused(self, _s):
        """stop_check=None → 视为永不暂停,正常流程。"""
        ex = _make_executor(_panel("成功设置演示甲的职位为管理员!"), stop_check=None)
        ok, _ = ex.execute_action("promote", "演示甲", retries=1)
        self.assertTrue(ok)

    def test_pause_gate_blocks_while_paused(self):
        """暂停期间 _send_with_gate 必须阻塞在门内,不得发出指令。

        用后台线程驱动:主线程侧保持 stop_check=True,验证 0.3s 内零发送,
        随后解除暂停,验证指令才被发出。这是"暂停时绝不误操作公会"的直接证据。
        """
        paused = {"v": True}
        ex = _make_executor([], stop_check=lambda: paused["v"])

        def driver():
            with patch("time.sleep", return_value=None):
                ex._send_with_gate("/guild promote 演示甲")

        t = threading.Thread(target=driver, daemon=True)
        t.start()
        # 门内去抖至少 3 个 50ms 周期 + 二次复查 0.2s;给足时间仍应被暂停拦住
        threading.Event().wait(0.3)
        self.assertEqual(
            ex.input.send_chat_text.call_count, 0, "暂停期间仍发出了指令 —— 会误操作公会"
        )
        paused["v"] = False
        t.join(timeout=3)
        self.assertFalse(t.is_alive(), "解除暂停后发送门未放行")
        self.assertEqual(ex.input.send_chat_text.call_count, 1, "解除暂停后应恰好发出 1 条")


class TestActionExecutorGcBroadcast(unittest.TestCase):
    """/gc 广播的发送与回显验证(硬件层,mock 隔离)。"""

    @patch("time.sleep", return_value=None)
    def test_broadcast_success_when_text_echoed(self, _s):
        """日志回显广播内容 → 成功。"""
        text = "演示甲 这周贡献为:100 执行操作:无"
        ex = _make_executor([f"[CHAT] 公会 > {text}"])
        ok, err = ex.gc_broadcast_with_retry(text, retries=1, timeout=1)
        self.assertTrue(ok, f"广播回显未被识别:{err}")

    @patch("time.sleep", return_value=None)
    def test_broadcast_fails_when_not_echoed(self, _s):
        """无回显 → 失败(重试耗尽)。"""
        ex = _make_executor([])
        ok, err = ex.gc_broadcast_with_retry("测试广播内容", retries=2, timeout=1)
        self.assertFalse(ok)
        self.assertIn("未在日志中回显", err)

    @patch("time.sleep", return_value=None)
    def test_broadcast_detects_error_pattern(self, _s):
        """广播遇到错误回显 → 失败并带错误文本。"""
        ex = _make_executor(["[CHAT] 你没有权限发言"], error_patterns=["没有权限"])
        ok, err = ex.gc_broadcast_with_retry("测试", retries=1, timeout=1)
        self.assertFalse(ok)
        self.assertEqual(err, "没有权限")


class TestActionExecutorExecuteActionsSequence(unittest.TestCase):
    """一组动作的执行序列(升职×2 应发 2 条;失败即停)。"""

    @patch("time.sleep", return_value=None)
    def test_two_promotes_send_twice(self, _s):
        ex = _make_executor(_panel("成功设置演示甲的职位为管理员!"))
        results = ex.execute_actions("演示甲", ["promote", "promote"])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(ok for _a, ok, _e in results))
        self.assertEqual(ex.input.send_chat_text.call_count, 2, "两次升职应发两条指令")

    @patch("time.sleep", return_value=None)
    def test_stops_after_first_failure(self, _s):
        """第一条失败后必须停(已踢出则后续升职无意义)。"""
        ex = _make_executor(["[CHAT] 权限不足"], error_patterns=["权限不足"])
        results = ex.execute_actions("演示甲", ["demote", "promote"])
        self.assertEqual(len(results), 1, f"失败后未停止,结果={results}")
        self.assertFalse(results[0][1])

    @patch("time.sleep", return_value=None)
    def test_empty_actions_returns_empty(self, _s):
        ex = _make_executor([])
        self.assertEqual(ex.execute_actions("演示甲", []), [])


class TestActionExecutorSendGateSerializes(unittest.TestCase):
    """发送互斥锁:多线程下模拟输入必须串行(硬件层)。"""

    def test_send_lock_serializes_concurrent_sends(self):
        """并发发送必须经同一把锁,否则剪贴板/键盘会串(粘贴错内容)。"""
        ex = _make_executor([])
        concurrent = []
        inside = {"n": 0, "max": 0}
        guard = threading.Lock()

        def fake_send(cmd):
            with guard:
                inside["n"] += 1
                inside["max"] = max(inside["max"], inside["n"])
            concurrent.append(cmd)
            with guard:
                inside["n"] -= 1

        ex.input.send_chat_text.side_effect = fake_send
        threads = [
            threading.Thread(target=lambda n=i: ex._send_with_gate(f"/cmd {n}")) for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(len(concurrent), 5, f"应发出 5 条,实际 {len(concurrent)}")
        self.assertEqual(
            inside["max"], 1, f"发送未串行化,最大并发={inside['max']}(剪贴板/键盘会串)"
        )


class TestZeroHardcoding(unittest.TestCase):
    """守卫 CLAUDE.md 第 1 条:业务数据零硬编码。

    为什么需要这类测试:硬编码文案/命令格式会在配置被用户修改后
    悄悄覆盖他的设置,"文案不得改动"这条红线就失效了。
    这类回归**看不出症状**(输出照样是对的,只是用的不是用户配置),
    必须用测试锁死。

    做法:故意从配置里删掉键,若代码返回了结果而不是抛错,
    说明它在用硬编码兜底。
    """

    def test_missing_template_raises_not_falls_back(self):
        """缺 notify_templates 时必须抛 KeyError,不能用硬编码文案兜底。"""
        for kind, actions in (
            ("none", []),
            ("kick", ["kick"]),
            ("demote", ["demote"]),
            ("promote", ["promote"]),
        ):
            with self.subTest(template=kind):
                cfg = {"notify_templates": {}, "guild_qq": "G"}
                with self.assertRaises(KeyError):
                    build_notification("演示甲", 100, actions, cfg)

    def test_missing_command_raises_not_falls_back(self):
        """缺 commands 时必须抛 KeyError,不能拼一条硬编码命令。"""
        for action in ("promote", "demote", "kick"):
            with self.subTest(action=action):
                with self.assertRaises(KeyError):
                    build_command({"commands": {}}, action, "演示甲")

    def test_config_edit_is_actually_used(self):
        """正向验证:改配置必须改变输出(否则说明代码没在读配置)。"""
        cfg = {
            "notify_templates": {"none": "自定义无操作:{player}/{weekly}"},
            "guild_qq": "G",
        }
        msg = build_notification("演示甲", 42, [], cfg)
        self.assertEqual(
            msg, "自定义无操作:演示甲/42", "改配置后输出没变 —— 代码没读配置或用了硬编码"
        )

    def test_command_edit_is_actually_used(self):
        """正向验证:改命令格式配置必须改变输出。"""
        cfg = {"commands": {"guild_promote": "/gp {player}"}}
        self.assertEqual(build_command(cfg, "promote", "演示甲"), "/gp 演示甲")


if __name__ == "__main__":
    unittest.main(verbosity=2)
