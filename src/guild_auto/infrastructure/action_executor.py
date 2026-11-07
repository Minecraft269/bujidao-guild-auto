# -*- coding: utf-8 -*-
"""
action_executor.py —— 硬件执行层(ActionExecutor)
==============================================
从 actions.py 迁出的**发送与执行**逻辑。actions.py 只保留
「决策 + 文案 + 命令格式」三件纯逻辑(可脱离游戏独立单元测试),
本模块承担全部真实设备交互:pyautogui 键盘/剪贴板、窗口激活、OCR 菜单处理、
日志回显验证与重试。

本次迁移为**纯机械搬移,零逻辑改动** —— 逐字保留原 actions.py 的实现,
以免悄悄改变已验证的发送/暂停/重试语义。测试用 monkeypatch 目标相应改为
本模块(action_executor.activate_window / .prepare_chat_state)。
"""

import threading
import time

import pyautogui

from guild_auto.infrastructure import logger
from guild_auto.domain.decisions import build_command
from guild_auto.config import console
from guild_auto.infrastructure.game_state import prepare_chat_state
from guild_auto.domain.parsers import strip_chat_prefix
from guild_auto.infrastructure.window_input import activate_window


class ActionExecutor:
    """执行 /gc 广播与升/降/踢命令,通过日志钩子检测错误回显。"""

    def __init__(self, cfg, human_input, watcher, hwnd=None, detector=None, stop_check=None):
        self.cfg = cfg
        self.input = human_input
        self.watcher = watcher
        self.hwnd = hwnd
        self.detector = detector
        self.stop_check = stop_check
        self.verbose = cfg.get("switches", {}).get("verbose_log", True)
        self.error_patterns = cfg.get("error_patterns", [])
        # 错误回显检测窗口(秒):发送命令后收集日志的时长;慢服务器可调大
        self.error_check_window = float(cfg.get("timeouts", {}).get("error_check_window", 4.0))
        # 发送互斥锁:模拟键盘/剪贴板是全局设备,多线程(异步队列)并发调用时
        # 必须串行化;暂停检查也在锁内完成,保证暂停期间任何线程都发不出命令,
        # 且恢复后先重置窗口状态再发送(见 _send_with_gate)
        self._send_lock = threading.Lock()
        # ---- 异步一轮的"回显账本"(R-01 / R-07 / R-08) ----
        # 异步执行时所有任务共用同一条日志游标:谁先读谁拿走,后发任务的回显
        # 会被前一个任务的窗口吞掉 → 假失败 → 重发升/降/踢。
        # 这里把"读日志"改成一轮一次的**增量读取**,并把每条回显按玩家名分配给
        # 任务,且同一行回显只认领一次。串行路径不启用(行为零变化)。
        self._round_active = False
        self._round_lines = []
        self._round_claims = {}
        self._round_total = 0
        self._round_judged = 0
        self._round_cache = None  # (行数, 面板块文本, 行快照) —— 增量解析缓存
        self._round_lock = threading.Lock()

    def _send_with_gate(self, cmd):
        """带暂停门的串行发送:获取发送锁 → 暂停则阻塞等待 → 恢复后重置窗口 → 发送。
        所有模拟输入(键盘/剪贴板)必须经此方法,不存在绕过暂停/重置的发送路径。"""
        with self._send_lock:
            # ---- 暂停门:严格等到 _paused 稳定为 False(去抖 N 周期) ----
            # 用户原话"按了 u 但未长按,_paused 短暂 True 后立即恢复 False":
            # 原 sleep(0.2) 看一次就放行——现改为看到 True 后 sleep+再 check,共 N 周期
            # 持续 False 才放行(避免瞬时抖动误判)
            _DEBOUNCE_CYCLES = 3
            gate_t0 = time.monotonic()
            paused_on_entry = bool(self.stop_check and self.stop_check())
            debounce_cycles = 0
            if paused_on_entry:
                # 看到 True 先打印,等 3 个 50ms 周期全部 False 才放行
                logger.log_info("SEND_GATE 暂停门进入 | 发送线程阻塞等待恢复")
                console("执行暂停,发送等待恢复 ...")
                stable = 0
                while self.stop_check and self.stop_check() and stable < _DEBOUNCE_CYCLES:
                    time.sleep(0.05)
                    if not (self.stop_check and self.stop_check()):
                        stable += 1
                    else:
                        stable = 0
                        debounce_cycles += 1
            # ---- 二次复查(防抖:用户按了 u 又立即松开时,原 while time.sleep(0.2)
            #      后看到 _paused 已经是 False,while 立即退出导致 send_chat_text 已执行)。
            #      现在:看到 _paused=False 后再等一个 poll 周期再看一次——稳定为 False 才发。
            #      用 while True + break 模式以便二次暂停时 continue 回顶部
            # 暂停期间每轮都会进这里:debug 必须节流,否则暂停几分钟日志被刷屏。
            recheck_rounds = 0
            while True:
                time.sleep(0.2)  # 二次复查去抖间隔(与门内 while 一致)
                if not (self.stop_check and self.stop_check()):
                    break  # 稳定非暂停,准备发送
                recheck_rounds += 1
                if recheck_rounds == 1 or recheck_rounds % 20 == 0:
                    logger.log_debug(
                        f"SEND_GATE 等待恢复中 | 已等待约 {time.monotonic() - gate_t0:.1f}s"
                        f" | 复查轮次={recheck_rounds}"
                    )
            # ---- 恢复后重置窗口状态(_reset_needed 由 main._toggle_pause 置位,
            #      reset_ui 内部消费该标志;无残留标志时 reset_ui 仍做一次轻量校验) ----
            self.reset_ui()
            logger.trace_code_location(
                "ActionExecutor._send_with_gate", f"准备发送 cmd={cmd[:60]!r}"
            )
            t_send = time.monotonic()
            sent = self.input.send_chat_text(cmd)
            send_ms = int((time.monotonic() - t_send) * 1000)
            if sent is False:
                # stop_check 打断时 send_chat_text 返回 False:命令实际未发出,
                # 原实现照样记"完成",日志里看不出丢命令。
                logger.log_warning(
                    f"发送未执行 | cmd={cmd[:40]!r} | send_chat_text=False | reason=stop_check 中断"
                )
            logger.trace_code_location(
                "ActionExecutor._send_with_gate", f"send_chat_text 完成({send_ms}ms)"
            )
            if paused_on_entry:
                logger.log_info(
                    f"SEND_GATE 暂停门退出 | waited_s={time.monotonic() - gate_t0:.2f}"
                    f" | debounce_cycles={debounce_cycles} | reset_ui=True"
                )
            return sent

    def send_gated(self, cmd):
        """异步发送走与串行**同一条**门禁(R-14):发送锁 + 暂停门 + 恢复后重置窗口。

        异步三条发送路径此前直接调 human.send_chat_text,绕过了窗口激活/聊天栏准备/
        发送锁,与串行不一致,也会让两条流水线同时抢键盘。
        返回值与 human.send_chat_text 一致:False=明确没发出去(暂停取消)。
        """
        return self._send_with_gate(cmd)

    # ------------------------------------------------------------------
    # 异步一轮的回显账本(R-01 共用游标 / R-07 无名踢出归因 / R-08 错误归属)
    # ------------------------------------------------------------------
    def begin_verify_round(self, total_tasks=0):
        """开始异步一轮:清空账本。串行路径不调用 → 行为完全不变。"""
        with self._round_lock:
            self._round_active = True
            self._round_lines = []
            self._round_claims = {"promote": {}, "demote": {}, "kick_named": {}, "kick_unnamed": 0}
            self._round_total = int(total_tasks)
            self._round_judged = 0
            self._round_cache = None
        logger.log_trace(
            f"[TRACE][action_executor.begin_verify_round] total_tasks={int(total_tasks)}"
        )

    def end_verify_round(self):
        with self._round_lock:
            self._round_active = False
            self._round_lines = []
            self._round_claims = {}
            self._round_cache = None

    def _round_read_more(self, timeout):
        """增量读日志并累积进账本。返回本次新增行数。"""
        lines = self.watcher.read_until(timeout=timeout) or []
        if not lines:
            return 0
        with self._round_lock:
            self._round_lines.extend(lines)
            # 账本只保留最近 5000 行:长时间运行不涨内存
            if len(self._round_lines) > 5000:
                del self._round_lines[: len(self._round_lines) - 5000]
        return len(lines)

    def _round_claim(self, bucket, key, available):
        """从账本认领一条回显:available=窗口内可见条数,已认领过的不再分配。

        这是"同一行回显只能被认领一次"的落点:没有它,一个面板会被多个任务同时
        记成功,`promote×2` 只回来一条也会被算成两级。
        """
        with self._round_lock:
            used = self._round_claims.get(bucket, {}).get(key, 0)
            if available <= used:
                return False
            self._round_claims[bucket][key] = used + 1
            return True

    def _round_claim_unnamed_kick(self):
        with self._round_lock:
            used = self._round_claims.get("kick_unnamed", 0)
            total = self._round_claims.get("_unnamed_seen", 0)
            if total <= used:
                return False
            self._round_claims["kick_unnamed"] = used + 1
            return True

    def _judge_round(self, action, player):
        """在账本上判定一条动作:只认**自己名下**的回显,并认领它。"""
        text, lines = self._round_snapshot()
        if action in ("promote", "demote"):
            return self._round_claim(action, player, text.count(f"成功设置{player}的职位为"))
        if action == "kick":
            named = sum(
                1
                for ln in text.splitlines()
                if f"{player}被" in ln and "踢出" in ln and "公会！" in ln
            )
            if self._round_claim("kick_named", player, named):
                return True
            # 无名回显(只有「踢出成功」):串行下靠"窗口内唯一一条"归因,
            # 异步下多条在途会永远判失败。改为按先进先出分配 —— 每条无名回显
            # 只分给一个还没拿到具名回显的踢出任务。
            unnamed = sum(
                1
                for ln in lines
                if "踢出成功" in ln
                and strip_chat_prefix(ln).strip().rstrip("！!").strip() == "踢出成功"
            )
            with self._round_lock:
                self._round_claims["_unnamed_seen"] = unnamed
            return self._round_claim_unnamed_kick()
        return False

    def _round_snapshot(self):
        """账本快照(带增量缓存):行数没变就直接复用上次的面板解析结果。

        验证线程每 200ms 判一次,不缓存的话每轮都要重解析整本账 ——
        200 人一轮时是 O(行数×任务数) 的空转。
        """
        with self._round_lock:
            n = len(self._round_lines)
            if self._round_cache is not None and self._round_cache[0] == n:
                return self._round_cache[1], self._round_cache[2]
            lines = list(self._round_lines)
        text = "\n".join(self._guild_block_lines(lines, watcher=self.watcher))
        with self._round_lock:
            self._round_cache = (n, text, lines)
        return text, lines

    def _round_error(self, pending):
        """错误回显的归属判定(R-08)。

        窗口里可能混入**别人命令**的错误回显(「玩家不存在」「权限不足」这类短词
        不带玩家名),一律判失败会把已成功的操作记成失败并触发重发。
        只有"本轮只剩这一条待判定"时错误才无歧义,才允许据此判失败;
        否则忽略并留痕,由窗口超时给出失败结论。
        """
        with self._round_lock:
            remaining = max(1, self._round_total - self._round_judged)
        effective = remaining if pending is None else max(1, int(pending))
        if effective > 1:
            return None
        _text, lines = self._round_snapshot()
        for line in lines:
            if "[CHAT]" in line:
                err = self._find_error(strip_chat_prefix(line))
                if err:
                    return err
        return None

    def _verify_action_in_round(self, action, player, timeout, pending):
        """异步一轮内的验证:增量读账本 → 判定 → 认领,直到本条有结论或超时。"""
        window = self.error_check_window if timeout is None else float(timeout)
        deadline = time.monotonic() + window
        while True:
            remaining = deadline - time.monotonic()
            step = 0.2 if remaining > 0.2 else max(0.01, remaining)
            self._round_read_more(timeout=step)
            if self._judge_round(action, player):
                with self._round_lock:
                    self._round_judged += 1
                logger.log_trace(
                    f"[TRACE][action_executor._verify_action_in_round] ok"
                    f" action={action} player_tag={logger.player_tag(player)}"
                )
                return True, None
            err = self._round_error(pending)
            if err:
                with self._round_lock:
                    self._round_judged += 1
                logger.log_warning(
                    f"[异步-验证] 错误回显归因到本次操作 action={action}"
                    f" player={player} error={err}"
                )
                return False, err
            if remaining <= 0:
                with self._round_lock:
                    self._round_judged += 1
                return False, "未收到成功回显"
            time.sleep(0.05)

    def reset_ui(self):
        """发送前界面保障:激活游戏窗口;若暂停恢复标志置位则按 Esc 清残留并 OCR 处理菜单。
        幂等:_reset_needed 未置位时不做多余动作(仅激活窗口)。"""
        if self.hwnd:
            activate_window(self.hwnd)
        # 消费 main 的暂停恢复标志(main.reset_window_state_after_pause 同款逻辑;
        # 经回调注入避免循环导入——由 main 在构造后 set_reset_hook 注入)
        consume = getattr(self, "_consume_reset_flag", None)
        need_reset = bool(consume()) if callable(consume) else False
        if need_reset:
            pyautogui.press("esc")  # 关闭可能残留的菜单/聊天栏
            time.sleep(0.2)
            if self.detector is not None:
                prepare_chat_state(self.detector, console)
            time.sleep(0.3)
            console("  (重置界面状态：激活窗口，关闭覆盖层，处理菜单)")

    def set_reset_hook(self, consume_fn):
        """注入暂停恢复标志的消费函数(main._reset_needed 的读取+复位)。"""
        self._consume_reset_flag = consume_fn

    def _ensure_window(self):
        """发送前重新激活游戏窗口并处理菜单界面(每次发送前执行,含重试/暂停恢复)。"""
        if self.hwnd is not None:
            activate_window(self.hwnd)
        if self.detector is not None:
            prepare_chat_state(self.detector, console)

    def _find_error(self, text):
        for pat in self.error_patterns:
            if pat and pat in text:
                return pat
        return None

    def gc_broadcast(self, text):
        """通过 /gc 在游戏内广播(查询完成后、执行操作前发送)。
        经 _send_with_gate 发送:锁内串行 + 暂停门 + 恢复重置。
        返回发送结果(False=明确未发出),供异步通知的 send_fn 判定。"""
        prefix = self.cfg.get("commands", {}).get("gc_prefix", "/gc")
        self._ensure_window()
        return self._send_with_gate(f"{prefix} {text}")

    @staticmethod
    def _guild_block_lines(lines, watcher=None):
        """只取**公会的会面板**内的 [CHAT] 正文。

        为什么必须过滤载体:真实回显由服务端以面板块下发 —— 两条分隔线
        (--------------------公会---------------------)夹着中间内容。而任意玩家
        在公屏/公会频道打一句同样的字,没有这层包裹。若不区分载体,
        任何人打一句 "甲被管理员踢出演示公会！" 就能让脚本把没踢掉的
        玩家记成已成功(假成功 → 漏踢,而报告会说"已执行")。

        实现直接复用 log_watcher.extract_chat_block —— 它已处理网易版
        "整块面板是一条物理行 + 内部字面 \\n" 的形态。自己再写一份解析
        必然漏掉某个真实形态(我第一版就是这么写错的:漏了 \\n 还原,
        导致所有真实面板都取不到内容)。
        """
        try:
            from guild_auto.infrastructure.log_watcher import extract_chat_block, split_messages

            messages = split_messages(list(lines))
            # 一个窗口里可能先到普通聊天、后到面板(R-04);也可能同时到多个面板。
            # 只取 messages[0] 会把面板整体丢掉 → 假失败 → 重发破坏性命令。
            out = []
            for msg in messages:
                block = extract_chat_block(msg)
                if block:
                    out.extend(block)
        except Exception as e:
            # 解析层炸掉会让所有动作都被判成"未收到成功回显",报告与真实结果不符;
            # 默认级别下必须能看到是解析失败而不是动作失败。
            logger.log_warning(
                f"公会面板解析失败 | lines={len(lines) if lines is not None else 0}"
                f" | {e!r} | 返回=空块"
            )
            # 解析失败把整块 + 上下文行无条件写 ERROR(默认级别可见);
            # 诊断日志自身失败不得影响业务结果。
            try:
                report = getattr(watcher, "report_parse_error", None)
                if callable(report):
                    report(e, lines)
            except Exception:
                pass
            return []
        logger.log_trace(
            f"[TRACE][action_executor._guild_block_lines] items={len(lines)}"
            f" block_lines={len(out)}"
            f" has_chat={any('[CHAT]' in ln for ln in lines)}"
        )
        logger.log_debug(
            f"公会面板解析结果 | 块行数={len(out)}"
            f" | 含[CHAT]={any('[CHAT]' in ln for ln in lines)}"
            f" | 首行={out[0][:60] if out else ''!r}"
        )
        return out

    def _find_success(self, action, player, lines):
        """根据操作类型检查成功回显。返回 True/False。

        只认**公会的会面板**内的回显(见 _guild_block_lines)——
        公屏任意聊天行不能当成功证据,否则可被伪造成"已踢出/已降职"。
        """
        text = "\n".join(self._guild_block_lines(lines, watcher=self.watcher))
        logger.log_trace(
            f"[TRACE][action_executor._find_success] action={action}"
            f" lines={len(lines)} guild_block_len={len(text)} target=<snip>"
        )
        if action == "promote" or action == "demote":
            # 匹配 "成功设置{player}的职位为{rank_name}!"
            # 由于 rank_name 未知，我们只检查是否包含 "成功设置" 和 player
            if f"成功设置{player}的职位为" in text:
                logger.log_trace(
                    "[TRACE][action_executor._find_success] rule=promote_demote_text"
                    " hit=true reason=含成功设置+目标"
                )
                return True
        elif action == "kick":
            # 强证据:公会面板内 "{player}被{admin}踢出{guild_name}公会！"(含玩家名)
            if f"{player}被" in text and "踢出" in text and "公会！" in text:
                logger.log_trace(
                    "[TRACE][action_executor._find_success] rule=kick_named"
                    " hit=true reason=含目标+踢出+公会"
                )
                return True
            # 兜底(规格 5.2 ③ 时间窗):真实回显常常只有「踢出成功」、**不含玩家名**,
            # 按名匹配会永远判失败。串行执行下同一时刻只有一条踢出在途,因此窗口内
            # 恰好一条「踢出成功」可归因于本次操作;两条及以上属于无法归因 ——
            # 宁可判失败交人工确认,也不能把别人的踢出算到本次头上。
            echoes = []
            for ln in lines:
                if "踢出成功" not in ln:
                    continue
                # 只接受**系统回显**(整行以它开头)。玩家在频道里打一句
                # "某人: 踢出成功" 属于伪造,不能当成功证据 —— 与上面"只认公会面板"
                # 同一个原则(独立复查找出:裸子串匹配可被任意聊天行伪造)。
                if strip_chat_prefix(ln).strip().rstrip("！!").strip() == "踢出成功":
                    echoes.append(ln)
            if len(echoes) == 1:
                logger.log_warning(
                    "踢出回显不含玩家名,按时间窗归因(串行单踢,窗口内唯一一条):" + str(player)
                )
                logger.log_trace(
                    "[TRACE][action_executor._find_success] rule=kick_time_window"
                    " hit=true reason=窗口内唯一一条踢出成功 echoes=1"
                )
                return True
            if len(echoes) > 1:
                logger.log_warning(
                    "窗口内出现 "
                    + str(len(echoes))
                    + " 条「踢出成功」,无法归因到 "
                    + str(player)
                    + ",判失败待人工确认"
                )
                logger.log_trace(
                    f"[TRACE][action_executor._find_success] rule=kick_time_window"
                    f" hit=false reason=无法归因 echoes={len(echoes)}"
                )
            return False
        logger.log_trace(
            f"[TRACE][action_executor._find_success] rule=none hit=false"
            f" reason=无匹配 action={action}"
        )
        return False

    def verify_action(self, action, player, timeout=None, pending=None):
        """**只读日志验证**已发送的动作,不发送任何指令。

        为什么必须有这个原语:异步模式下主线程已经用 send_only 把命令发出去,
        后台验证者若再调 `execute_action`,就会**把同一条命令发第二遍**
        (实测 promote×2 真升 4 级、踢出重复踢)。execute_action 是
        "发送+等待+判定"的串行语义,不能复用于"只判定"。

        pending: 本轮还剩几条待判定(仅异步一轮用)。用于判断错误回显能否归因
        (R-08):别人命令的错误回显不该把本次已成功的操作判成失败。
        """
        if self._round_active:
            return self._verify_action_in_round(action, player, timeout, pending)
        logger.trace_call(
            "action_executor.verify_action",
            kwargs={
                "action": action,
                "timeout": self.error_check_window if timeout is None else timeout,
            },
        )
        lines = self.watcher.read_until(
            timeout=self.error_check_window if timeout is None else timeout
        )
        logger.log_trace(
            f"[TRACE][action_executor.verify_action] step=read_log"
            f" action={action} lines={len(lines)}"
            f" window_s={self.error_check_window if timeout is None else timeout}"
        )
        logger.trace_code_location("verify_action.received", f"lines={len(lines)}")
        # 先判成功、再判错误(R-08):成功面板是强证据,同窗口里别人的错误回显
        # (「玩家不存在」这类不带玩家名的短词)不该把它翻成失败并触发重发。
        if self._find_success(action, player, lines):
            logger.trace_return("action_executor.verify_action", {"ok": True})
            return True, None
        for line in lines:
            if "[CHAT]" in line:
                content = strip_chat_prefix(line)
                err = self._find_error(content)
                if err:
                    logger.trace_return(
                        "action_executor.verify_action",
                        {"ok": False, "error": logger.sanitize_text(err, 120)},
                    )
                    return False, err
        logger.trace_return(
            "action_executor.verify_action", {"ok": False, "error": "未收到成功回显"}
        )
        return False, "未收到成功回显"

    def execute_action(self, action, player, retries=None):
        """执行单条动作命令，支持重试。返回 (ok, error_text|None)。"""
        logger.trace_call(
            "action_executor.execute_action",
            kwargs={"action": action, "retries": retries},
        )
        logger.trace_code_location("execute_action.start", f"action={action} player={player}")
        if retries is None:
            retries = int(self.cfg.get("timeouts", {}).get("max_retries", 3))
        cmd = build_command(self.cfg, action, player)
        logger.log_trace(
            f"[TRACE][action_executor.execute_action] step=build_cmd"
            f" action={action} cmd_len={len(cmd)} retries={retries} target=<snip>"
        )

        attempt = 0
        while attempt < retries:
            # ---- 暂停检查(锁外快查:暂停时直接阻塞等待,不占发送锁) ----
            if self.stop_check and self.stop_check():
                console("执行暂停，等待恢复...")
                while self.stop_check():
                    time.sleep(0.1)
                continue  # 不消耗重试次数;恢复后的窗口重置由 _send_with_gate 内 reset_ui 完成

            self._ensure_window()
            if self.verbose:
                # print 在无控制台(GUI/打包 sys.stdout=None)时不落盘,发送记录会缺失;
                # console() 同时写控制台与日志文件。
                console(
                    f"  → 发送: {cmd} (尝试 {attempt + 1}/{retries})"
                    f" | action={action} | player={player}"
                )
            # 带暂停门的串行发送(锁内:暂停阻塞→恢复重置→发送)
            self._send_with_gate(cmd)
            logger.trace_code_location("execute_action.sent", f"cmd={cmd} attempt={attempt + 1}")
            lines = self.watcher.read_until(timeout=self.error_check_window)
            logger.trace_code_location("execute_action.received", f"lines={len(lines)}")

            # 检查错误
            error = None
            for line in lines:
                if "[CHAT]" in line:
                    content = strip_chat_prefix(line)
                    err = self._find_error(content)
                    if err:
                        error = err
                        break
            if error is not None:
                logger.log_warning(f"执行失败 [{action}] {player}: {error} (尝试 {attempt + 1})")
                if attempt < retries - 1:
                    time.sleep(1.0)
                    attempt += 1
                    continue
                res = (False, error)
                logger.trace_return(
                    "action_executor.execute_action",
                    {
                        "ok": False,
                        "error": logger.sanitize_text(error, 120),
                        "attempts": attempt + 1,
                    },
                )
                return res

            # 检查成功
            if self._find_success(action, player, lines):
                res = (True, None)
                logger.trace_return(
                    "action_executor.execute_action",
                    {"ok": True, "attempts": attempt + 1},
                )
                return res

            # 无错误无成功，重试
            logger.log_debug(f"执行无回显 [{action}] {player} (尝试 {attempt + 1})")
            if attempt < retries - 1:
                time.sleep(1.0)
            attempt += 1

        res = (False, "重试耗尽")
        logger.trace_return(
            "action_executor.execute_action",
            {"ok": False, "error": "重试耗尽", "attempts": attempt},
        )
        return res

    def gc_broadcast_with_retry(self, text, retries=3, timeout=4.0):
        """发送 /gc 广播，验证是否在日志中回显。返回 (success, error_msg)。"""
        prefix = self.cfg.get("commands", {}).get("gc_prefix", "/gc")
        for attempt in range(retries):
            logger.log_debug(f"[BROADCAST] 尝试 {attempt + 1}/{retries} | text_len={len(text)}")
            self.gc_broadcast(text)  # 发送
            lines = self.watcher.read_until(timeout=timeout)
            logger.log_debug(f"[BROADCAST] 回显 {len(lines)} 行 (尝试 {attempt + 1}/{retries})")

            # 检查是否出现错误
            error = None
            for line in lines:
                if "[CHAT]" in line:
                    content = strip_chat_prefix(line)
                    err = self._find_error(content)
                    if err:
                        error = err
                        break
            if error is not None:
                logger.log_warning(
                    f"[BROADCAST] 错误命中,重试 | reason=error_pattern"
                    f" | attempt={attempt + 1}/{retries} | error={error}"
                )
                if attempt < retries - 1:
                    time.sleep(1.0)
                    continue
                logger.log_warning(f"[BROADCAST] 最终失败 | error={error!r} | retries={retries}")
                return False, error

            # 检查是否出现广播内容（去除前缀的文本是否出现在日志中）
            # 注意：日志中广播内容可能带有 "公会 >" 等前缀，我们使用 strip_chat_prefix 后检查
            found = False
            for line in lines:
                if "[CHAT]" in line:
                    content = strip_chat_prefix(line)
                    # 忽略可能的前缀（如 "公会 >某玩家: "），直接检查 text 是否在 content 中
                    if text in content:
                        found = True
                        break
            if found:
                return True, None

            logger.log_warning(
                f"[BROADCAST] 未回显,重试 | reason=未回显 | attempt={attempt + 1}/{retries}"
            )
            if attempt < retries - 1:
                time.sleep(1.0)
        logger.log_warning(f"[BROADCAST] 最终失败 | error='广播未在日志中回显' | retries={retries}")
        return False, "广播未在日志中回显"

    def execute_actions(self, player, actions):
        """执行一组动作;返回 [(action, ok, error)] 列表。"""
        results = []
        for action in actions:
            ok, err = self.execute_action(action, player)
            results.append((action, ok, err))
            if not ok:
                break  # 失败即停(如已踢出,后续动作无意义)
            self.input.wait_between_commands()
        return results
