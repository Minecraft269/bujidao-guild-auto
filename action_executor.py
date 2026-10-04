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

import logger
from actions import build_command
from config import console
from game_state import prepare_chat_state
from parsers import strip_chat_prefix
from window_input import activate_window


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

    def _send_with_gate(self, cmd):
        """带暂停门的串行发送:获取发送锁 → 暂停则阻塞等待 → 恢复后重置窗口 → 发送。
        所有模拟输入(键盘/剪贴板)必须经此方法,不存在绕过暂停/重置的发送路径。"""
        with self._send_lock:
            # ---- 暂停门:严格等到 _paused 稳定为 False(去抖 N 周期) ----
            # 用户原话"按了 u 但未长按,_paused 短暂 True 后立即恢复 False":
            # 原 sleep(0.2) 看一次就放行——现改为看到 True 后 sleep+再 check,共 N 周期
            # 持续 False 才放行(避免瞬时抖动误判)
            _DEBOUNCE_CYCLES = 3
            logged_waiting = False
            if self.stop_check and self.stop_check():
                # 看到 True 先打印,等 3 个 50ms 周期全部 False 才放行
                if not logged_waiting:
                    logger.log_debug("检测到暂停状态,发送线程严格阻塞等待稳定恢复 ...")
                    console("执行暂停,发送等待恢复 ...")
                    logged_waiting = True
                stable = 0
                while self.stop_check and self.stop_check() and stable < _DEBOUNCE_CYCLES:
                    time.sleep(0.05)
                    if not (self.stop_check and self.stop_check()):
                        stable += 1
                    else:
                        stable = 0
            # ---- 二次复查(防抖:用户按了 u 又立即松开时,原 while time.sleep(0.2)
            #      后看到 _paused 已经是 False,while 立即退出导致 send_chat_text 已执行)。
            #      现在:看到 _paused=False 后再等一个 poll 周期再看一次——稳定为 False 才发。
            #      用 while True + break 模式以便二次暂停时 continue 回顶部
            while True:
                time.sleep(0.2)  # 二次复查去抖间隔(与门内 while 一致)
                if not (self.stop_check and self.stop_check()):
                    break  # 稳定非暂停,准备发送
                logger.log_debug("发送前二次复查检测到持续暂停,继续阻塞等待 ...")
            # ---- 恢复后重置窗口状态(_reset_needed 由 main._toggle_pause 置位,
            #      reset_ui 内部消费该标志;无残留标志时 reset_ui 仍做一次轻量校验) ----
            self.reset_ui()
            logger.trace_code_location("ActionExecutor._send_with_gate", f"准备发送 cmd={cmd[:60]!r}")
            t_send = time.monotonic()
            self.input.send_chat_text(cmd)
            send_ms = int((time.monotonic() - t_send) * 1000)
            logger.trace_code_location("ActionExecutor._send_with_gate", f"send_chat_text 完成({send_ms}ms)")

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
            pyautogui.press("esc")   # 关闭可能残留的菜单/聊天栏
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
        经 _send_with_gate 发送:锁内串行 + 暂停门 + 恢复重置。"""
        prefix = self.cfg.get("commands", {}).get("gc_prefix", "/gc")
        self._ensure_window()
        self._send_with_gate(f"{prefix} {text}")

    def _find_success(self, action, player, lines):
        """根据操作类型检查成功回显。返回成功消息或 None。"""
        text = "\n".join(lines)
        if action == "promote" or action == "demote":
            # 匹配 "成功设置{player}的职位为{rank_name}!"
            # 由于 rank_name 未知，我们只检查是否包含 "成功设置" 和 player
            if f"成功设置{player}的职位为" in text:
                return True
        elif action == "kick":
            # 匹配 "{player}被{admin}踢出{guild_name}公会！"
            # 检查是否包含 f"{player}被" 和 "踢出" 和 "公会！"
            if f"{player}被" in text and "踢出" in text and "公会！" in text:
                return True
        return False

    def execute_action(self, action, player, retries=None):
        """执行单条动作命令，支持重试。返回 (ok, error_text|None)。"""
        logger.trace_code_location("execute_action.start", f"action={action} player={player}")
        if retries is None:
            retries = int(self.cfg.get("timeouts", {}).get("max_retries", 3))
        cmd = build_command(self.cfg, action, player)

        attempt = 0
        while attempt < retries:
            # ---- 暂停检查(锁外快查:暂停时直接阻塞等待,不占发送锁) ----
            if self.stop_check and self.stop_check():
                console("执行暂停，等待恢复...")
                while self.stop_check():
                    time.sleep(0.1)
                continue   # 不消耗重试次数;恢复后的窗口重置由 _send_with_gate 内 reset_ui 完成

            self._ensure_window()
            if self.verbose:
                print(f"  → 发送: {cmd} (尝试 {attempt+1}/{retries})")
            # 带暂停门的串行发送(锁内:暂停阻塞→恢复重置→发送)
            self._send_with_gate(cmd)
            logger.trace_code_location("execute_action.sent", f"cmd={cmd} attempt={attempt+1}")
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
                logger.log_warning(f"执行失败 [{action}] {player}: {error} (尝试 {attempt+1})")
                if attempt < retries - 1:
                    time.sleep(1.0)
                    attempt += 1
                    continue
                res = (False, error)
                logger.trace_return("execute_action", res)
                return res

            # 检查成功
            if self._find_success(action, player, lines):
                res = (True, None)
                logger.trace_return("execute_action", res)
                return res

            # 无错误无成功，重试
            logger.log_debug(f"执行无回显 [{action}] {player} (尝试 {attempt+1})")
            if attempt < retries - 1:
                time.sleep(1.0)
            attempt += 1

        res = (False, "重试耗尽")
        logger.trace_return("execute_action", res)
        return res

    def gc_broadcast_with_retry(self, text, retries=3, timeout=4.0):
        """发送 /gc 广播，验证是否在日志中回显。返回 (success, error_msg)。"""
        prefix = self.cfg.get("commands", {}).get("gc_prefix", "/gc")
        for attempt in range(retries):
            self.gc_broadcast(text)  # 发送
            lines = self.watcher.read_until(timeout=timeout)

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
                if attempt < retries - 1:
                    time.sleep(1.0)
                    continue
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

            if attempt < retries - 1:
                time.sleep(1.0)
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
