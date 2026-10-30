# -*- coding: utf-8 -*-
"""
runtime.py —— 编排层依赖容器
============================
Runtime 是 main.py 能从"不可测"变"可测"的关键:把所有与外界交互的东西
(watcher / human / executor / detector / hwnd / 用户确认 / 休眠)收进一个容器,
并允许整体注入替换。

阶段函数(stages.py)只拿 Runtime 当参数,不自己 import 硬件模块 ——
于是每个阶段都能用假 Runtime 单独驱动,不必真实按键/截图/读日志。

拆出本文件的动机:main.py 曾接近 2000 行,"暂停机制 / 阶段编排 / 依赖容器"
混在一起。按关注点拆分后每个模块可单独读、单独测。
"""

import threading
import time

from guild_auto.infrastructure.action_executor import ActionExecutor
from guild_auto.config import console
from guild_auto.infrastructure.game_state import GameStateDetector
from guild_auto.infrastructure.log_watcher import LogWatcher
from guild_auto.infrastructure.window_input import HumanInput, find_game_window

from guild_auto.infrastructure import logger

# ---------------------------------------------------------------------------
# 暂停状态(单一所有者)
# ---------------------------------------------------------------------------
# pynput 监听线程写、主线程与后台线程读。必须在模块级 —— 监听线程拿不到
# Runtime 实例。**只通过 Runtime.is_paused / pause_gate 访问**, 不要在别处裸读,
# 否则主线程与后台会读到不同步的快照。
_paused = False
_pause_lock = threading.Lock()
_reset_needed = False  # 暂停恢复后需要重置界面
_reset_lock = threading.Lock()  # 保护重置标志
_pause_key = None
_pause_key_name = "?"
_force_stop = False  # Ctrl+C 标志:sig handler 置位,阻塞调用据此中断

# 暂停事件:主线程后台链路在 wait/IO 阻塞时也会被 _toggle_pause 置位,
# wait_if_paused / wait_for_chat_block 等通过 _pause_event.wait() 立即响应。
# 修复"pynput on_press 里 raise KeyboardInterrupt 会被 pynput 静默吞掉"的根因:
# pynput 监听线程对回调抛出的异常只 log 不重抛,导致主线程根本收不到 KeyboardInterrupt。
# 现在 on_press 仅设事件,主线程的阻塞调用通过 event.wait() 主动感知。
_pause_event = threading.Event()

# 暂停门/暂停等待的日志上下文:调用方在进入阻塞前写入"哪个阶段/哪条命令"。
# 用线程局部而非全局 —— 后台 sender 线程与主线程会各自卡在同一道门上,
# 共用一个变量会互相覆盖,日志就定位不到真正的调用方。
_pause_ctx = threading.local()
# 暂停门是否已记录过"开始阻塞":逐条发送/重试会反复调 pause_gate,
# 只在状态变化那一刻记一次,避免刷屏。
_pause_gate_reported = False


def set_pause_context(stage="?", command="?"):
    """记录接下来的暂停阻塞属于哪个阶段/哪条命令(仅用于日志定位)。"""
    _pause_ctx.stage = stage
    _pause_ctx.command = command


def pause_context():
    """取当前线程上次写入的暂停上下文;未设置时返回 ('?', '?')。"""
    return getattr(_pause_ctx, "stage", "?"), getattr(_pause_ctx, "command", "?")


# ---------------------------------------------------------------------------
# 自动暂停(US-009):UI 模式执行中检测到用户键鼠活动
# ---------------------------------------------------------------------------
# 与暂停状态一样,这里的状态必须只有一份:监听线程(pynput)写、主流程读。
# 判定条件全部由 runtime 持有,监听回调只问"现在该不该暂停",不自己拼条件 ——
# 否则"确认等待时不触发 / 模拟输入不触发"这些边界会散落在回调里,越改越乱。
_auto_pause_lock = threading.Lock()
_auto_pause_mode = False  # 仅 operation_mode="ui" 且 pause.auto_on_user_input=true
_auto_pause_grace_s = 0.3  # 模拟输入后的忽略窗口(秒),来自 pause.user_input_grace_ms
_auto_pause_guard = False  # 有进行中的阶段任务时为 True;确认等待/空闲/报告输出为 False
_auto_paused = False  # 本次暂停是否由自动暂停触发(用于恢复计数)
_input_ignore_until = 0.0  # 模拟输入忽略截止时刻(monotonic 秒)
_rescan_required = False  # 恢复后必须重新全扫(UI 模式)
_auto_pause_count = 0
_auto_pause_resume_count = 0


def configure_auto_pause(*, operation_mode, enabled, grace_ms):
    """按配置启用/关闭自动暂停。command 模式永远关闭(与用户开关无关)。"""
    global _auto_pause_mode, _auto_pause_grace_s
    with _auto_pause_lock:
        _auto_pause_mode = operation_mode == "ui" and bool(enabled)
        _auto_pause_grace_s = max(0, int(grace_ms)) / 1000.0


def auto_pause_enabled():
    """自动暂停是否生效(UI 模式 + 配置开关)。监听器据此决定是否挂鼠标钩子。"""
    return _auto_pause_mode


def set_auto_pause_guard(value):
    """标记"是否有进行中的阶段任务"。等待确认/空闲/输出报告时必须为 False。"""
    global _auto_pause_guard
    with _auto_pause_lock:
        _auto_pause_guard = bool(value)


def auto_pause_guard():
    """当前是否处于执行中(自动暂停的触发前提之一)。"""
    return _auto_pause_guard


def note_simulated_input():
    """脚本自己模拟输入前调用:打开忽略窗口,使监听器不把自己的动作当用户输入。"""
    global _input_ignore_until
    with _auto_pause_lock:
        _input_ignore_until = time.monotonic() + _auto_pause_grace_s


def input_is_simulated():
    """当前时刻是否落在脚本模拟输入的忽略窗口内。"""
    with _auto_pause_lock:
        return time.monotonic() < _input_ignore_until


def request_auto_pause():
    """检查是否应自动暂停;满足全部条件时置暂停状态并返回 True。

    条件(缺一不可):UI 模式且开关打开、有进行中的阶段任务、当前未暂停、
    不是脚本自己模拟的输入。**暂停键本身由 pause._on_press 提前分流**,
    不会走到这里。
    """
    global _auto_pause_count, _auto_paused, _rescan_required
    with _auto_pause_lock:
        if not _auto_pause_mode or not _auto_pause_guard:
            return False
        if _paused or _force_stop:
            return False
        if time.monotonic() < _input_ignore_until:
            return False
        _auto_paused = True
        _auto_pause_count += 1
        _rescan_required = True
    set_paused(True)
    set_reset_needed(True)
    _pause_event.set()
    return True


def note_resume():
    """暂停键恢复后调用:窗口必须重置;UI 模式还要重新全扫确认界面。

    任何暂停(手动或自动)在 UI 模式下恢复后都不能直接接着扫 ——
    暂停期间界面可能已被用户操作改变,必须重来一遍。
    """
    global _auto_paused, _auto_pause_resume_count, _rescan_required
    set_reset_needed(True)
    with _auto_pause_lock:
        if _auto_paused:
            _auto_pause_resume_count += 1
            _auto_paused = False
        if _auto_pause_mode:
            _rescan_required = True


def consume_rescan_flag():
    """读取并清除"恢复后需重新全扫"标志(UI 采集的门禁使用)。"""
    global _rescan_required
    with _auto_pause_lock:
        value = _rescan_required
        _rescan_required = False
        return value


def auto_pause_stats():
    """自动暂停触发/恢复计数(写入第三份报告第六节)。"""
    with _auto_pause_lock:
        return {"triggers": _auto_pause_count, "resumes": _auto_pause_resume_count}


def set_reset_needed(value):
    """设置"暂停恢复后需重置窗口"标志(由 _toggle_pause 调用)。"""
    global _reset_needed
    with _reset_lock:
        _reset_needed = bool(value)


def consume_reset_flag():
    """读取并清除重置标志(返回原值)。发送前调用一次。"""
    global _reset_needed
    with _reset_lock:
        v = _reset_needed
        _reset_needed = False
        return v


def is_paused():
    """当前是否处于暂停(供 log_watcher 等不能拿 Runtime 的模块查询)。"""
    return _paused


def is_force_stop():
    """当前是否收到 Ctrl+C。"""
    return _force_stop


def set_paused(value):
    """置位/复位暂停标志(由 pause.py 的 _toggle_pause / _signal_handler 调用)。

    ⚠ 为什么不许在别的模块里写 `global _paused`:
    main.py 曾用 `from runtime import _paused` re-export。`from ... import 变量`
    绑定的是**值的副本**,而 `global _paused` 写的是 main 自己的副本 ——
    runtime._paused 永远是 False,于是 rg.is_paused() 永远 False,
    Runtime.pause_gate() 永不阻塞、run_pending 的暂停门形同虚设,
    **暂停键彻底失效且无声**。状态的唯一写入口必须是本函数。
    """
    global _paused
    _paused = bool(value)


def set_force_stop(value):
    """置位/复位 Ctrl+C 标志(理由同 set_paused:禁止别处 `global _force_stop`)。"""
    global _force_stop
    _force_stop = bool(value)


def pause_key_name():
    """当前暂停键的显示名(未启动监听时为 '?')。"""
    return _pause_key_name


def pause_key():
    """当前暂停键的 pynput 键对象(未启动监听时为 None)。"""
    return _pause_key


def set_pause_key_cfg(pause_key_cfg):
    """按配置解析并存下暂停键(单字符走 KeyCode,否则查 keyboard.Key)。

    返回 (key_obj, key_name)。
    """
    global _pause_key, _pause_key_name
    from pynput import keyboard

    _pause_key_name = str(pause_key_cfg)
    if len(_pause_key_name) == 1:
        _pause_key = keyboard.KeyCode.from_char(_pause_key_name)
    else:
        _pause_key = getattr(keyboard.Key, _pause_key_name, keyboard.KeyCode.from_char("u"))
    return _pause_key, _pause_key_name


def reset_state():
    """复位全部暂停相关状态(Ctrl+C 菜单选择"继续运行"后重启主流程)。

    不复位的话 _paused=True 残留,重启后第一个 wait_if_paused / _send_with_gate
    会立即卡在暂停门,表现为"选了继续但脚本不动"。
    """
    global _paused, _force_stop, _reset_needed
    global _auto_pause_mode, _auto_pause_grace_s
    global _auto_pause_guard, _auto_paused, _input_ignore_until, _rescan_required
    global _auto_pause_count, _auto_pause_resume_count
    with _pause_lock:
        _paused = False
    with _reset_lock:
        _reset_needed = False
    _force_stop = False
    _pause_event.clear()
    # 自动暂停的运行时状态同样要清:重启后残留 guard=True 会让"空闲"误触发暂停;
    # mode/grace 也清空 —— 监听器重启时 start_pause_listener(cfg) 会重新配置,
    # 而"没传 cfg 的监听器"(测试/旧调用点)不该继承上一轮的 UI 模式。
    with _auto_pause_lock:
        _auto_pause_mode = False
        _auto_pause_grace_s = 0.0
        _auto_pause_guard = False
        _auto_paused = False
        _input_ignore_until = 0.0
        _rescan_required = False
        _auto_pause_count = 0
        _auto_pause_resume_count = 0


class Runtime:
    """编排层依赖容器 —— main.py 从"不可测"变"可测"的关键。

    背景:旧 main() 在函数体内直接调 LogWatcher / GameStateDetector /
    find_game_window / start_pause_listener / input(),硬件依赖与流程编排缠死,
    导致 main.py 覆盖率只有 8%,而它占了项目总体覆盖率缺口的 95% ——
    总体门禁(≥85%)无论如何都过不去。

    做法:把**所有与外界交互的东西**收进这个容器,并允许整体注入替换:
      * watcher     日志读取(真实读 latest.log)
      * human       键鼠输入(真实按键/剪贴板)
      * executor    命令执行与回显验证
      * detector    OCR 界面检测
      * hwnd        游戏窗口句柄
      * ask_confirm 用户确认(默认 input();测试注入返回 True/False 的可调用)
      * sleep       休眠(默认 time.sleep;测试注入 no-op 让用例秒级完成)

    阶段函数只拿 Runtime 当参数,不再自己 import 硬件模块 ——
    于是每个阶段都能用假 Runtime 单独驱动。

    注意:confirm() 返回 False 时,调用方必须**放弃后续操作直接返回**,
    不得继续发送任何命令。这是"每个阶段推进前等用户确认"的执行点。
    """

    def __init__(
        self,
        cfg,
        watcher=None,
        human=None,
        executor=None,
        detector=None,
        hwnd=None,
        ask_confirm=None,
        ask_text_fn=None,
        sleep=None,
        listener=None,
        async_executor=None,
        async_enabled=False,
        stop_check=None,
    ):
        self.cfg = cfg
        self.watcher = watcher
        self.human = human
        self.executor = executor
        self.detector = detector
        self.hwnd = hwnd
        self.listener = listener
        self.async_executor = async_executor
        # 异步(后台)系统:惰性创建(见 async_verify_submit)。
        # 与旧 async_executor 的关系:旧的承担默认串行模式下的辅助,
        # 本系统承担规格要求的"主线程只发不验"路径。
        self.async_system = None
        # 异步(后台)模式开关:默认 False = 串行(流程规格的默认模式)。
        # True 时主线程"只发不验",验证交给 async_system 的后台线程。
        self.async_enabled = bool(async_enabled)
        # 用户确认点:默认走终端 input();测试注入可控的假实现
        self.ask_confirm = ask_confirm if ask_confirm is not None else _default_confirm
        # 「逐字匹配」类确认(kick 二次确认)必须拿原文,不能走 y/n 布尔化 ——
        # 否则提示写"输入 kick-confirm",实际却把 kick-confirm 判成否。
        self.ask_text_fn = ask_text_fn if ask_text_fn is not None else _default_ask_text
        # 确认点的结构化上下文（阶段名/目标人数/预计动作数、kick 名单）。
        # 由 stages 在提问前写入，GUI 模态据此展示；控制台模式忽略它。
        self.confirm_context: dict = {}
        # 时间源可注入,避免测试真的等 N 秒
        self.sleep = sleep if sleep is not None else time.sleep
        # 暂停查询:默认读本模块的 _paused(pynput 监听线程写、主线程/后台读);
        # 测试可注入恒 False 的回调。
        self.is_paused = stop_check if stop_check is not None else (lambda: _paused)

    def reset_window_state_after_pause(self, hwnd=None, detector=None):
        """发送前重置窗口状态(暂停恢复后有残留菜单时)。

        只在 _reset_needed 置位时做动作 —— 没有残留标志时反复按 Esc 才是 bug。
        实现委托给 main.reset_window_state_after_pause(pyautogui + detector 细节)。
        """
        from guild_auto.main import reset_window_state_after_pause as _reset

        return _reset(
            self.hwnd if hwnd is None else hwnd, self.detector if detector is None else detector
        )

    def confirm(self, prompt, default=False):
        """统一的阶段确认点。

        返回 True 表示用户同意继续;False 表示拒绝 —— 调用方必须立即停止后续操作
        (尤其是升/降/踢这类破坏性操作)。

        ask_confirm 契约:接收 (prompt, default),返回 bool。
        default=True 时(如"按回车继续"),空输入视为同意。
        """
        try:
            return bool(self.ask_confirm(prompt, default))
        except (EOFError, KeyboardInterrupt) as e:
            # 终端被关闭 / 用户按 Ctrl+C:一律按"拒绝"处理 ——
            # 宁可停下,也不能在状态不明时继续发命令
            logger.log_warning(f"确认点中断({type(e).__name__}) prompt={prompt!r}，按拒绝处理: {e}")
            return False

    def ask(self, prompt, default=False):
        """向用户提问,返回 True=是 / False=否(或未确认)。

        与 confirm() 的区别:confirm 面向"阶段边界是否继续",语义是
        "默认同意、拒绝即停";ask 面向"要一个明确的是/否答案",
        默认值由调用方给。

        同样兜底 EOF/Ctrl+C → False(拒绝),绝不在状态不明时继续发命令。
        """
        try:
            return bool(self.ask_confirm(prompt, default))
        except (EOFError, KeyboardInterrupt) as e:
            logger.log_warning(f"确认点中断({type(e).__name__}) prompt={prompt!r}，按拒绝处理: {e}")
            return False

    def ask_text(self, prompt):
        """向用户要一段**原文**(如 kick 二次确认的确认词)。

        与 ask() 的区别:ask 把输入布尔化(y/yes 才为真),用于"逐字匹配"
        的确认会把正确输入判成否 —— 必须拿原文让调用方自己比对。
        EOF/Ctrl+C 返回空串(= 未确认)。
        """
        try:
            return str(self.ask_text_fn(prompt) or "").strip()
        except (EOFError, KeyboardInterrupt) as e:
            logger.log_warning(f"确认点中断({type(e).__name__}) prompt={prompt!r}，按拒绝处理: {e}")
            return ""

    def pause_gate(self):
        """发送前的暂停门:暂停时阻塞等待,恢复后重置窗口状态。

        所有阶段函数(查询/通知/执行)发送前都要过这道门 —— 规格要求
        "暂停期间绝不发出任何指令"。

        阻塞可观测性:进入等待时记一条 WARNING(带阶段/命令,仅在状态变化
        那一刻记一次——逐条发送/重试会反复调本函数),解除时记 INFO 带耗时。
        """
        global _pause_gate_reported
        if self.is_paused():
            stage, command = pause_context()
            started = time.monotonic()
            if not _pause_gate_reported:
                _pause_gate_reported = True
                console("执行暂停，等待恢复...")
                logger.log_warning(
                    f"暂停门阻塞 stage={stage} 命令={command} 已等待=0.0s 等待=用户恢复(无限)"
                )
            while self.is_paused():
                self.sleep(0.1)
            _pause_gate_reported = False
            logger.log_info(f"暂停门解除 已等待={time.monotonic() - started:.1f}s")
        self.reset_window_state_after_pause(self.hwnd, self.detector)

    def async_verify_submit(self, command, player, meta=None):
        """异步模式:把指令交给 async_system(主线程只发不验)。

        惰性创建 AsyncVerifySystem 并注入 send_fn / verify_fn:
          * send_fn   只负责发出去(不读日志)
          * verify_fn 后台读日志 + 解析 + 判定
        返回 task_id。
        """
        from guild_auto.concurrency.async_system import AsyncVerifySystem

        if self.async_system is None:
            task_timeout = float(self.cfg.get("timeouts", {}).get("response_wait", 12.0)) + 5.0
            logger.log_debug(f"AsyncVerifySystem 建立 task_timeout={task_timeout:.1f}s")
            self.async_system = AsyncVerifySystem(
                verify_fn=self._async_verify_fn,
                task_timeout=task_timeout,
            )
            self.async_system.attach_sender(self._async_send_fn)
            self.async_system.start()
        return self.async_system.submit(command, player, meta or {})

    def _async_send_fn(self, command, player, meta):
        """后台发送:把已提交的指令发出去(不读日志)。

        必须与执行路径同一道暂停门:缺门时 sender 线程在暂停中调用 send_chat_text
        会因 stop_check 直接返回 False,而任务已被 mark_sent → 验证必然失败并烧光重试
        (通知阶段表现为"广播失败: 轮询超时",实际一条都没发出去)。
        恢复后还要再确认本轮没被主线程放弃,否则就是"报告记失败、游戏里真发了"。
        """
        set_pause_context(stage="后台发送", command=command)
        self.pause_gate()
        if self.async_system is not None and self.async_system.is_stopped():
            logger.log_warning("后台发送已取消(本轮已收工),跳过发送:" + str(command))
            return False
        sent = self.human.send_chat_text(command, detector=self.detector, stop_check=self.is_paused)
        self.human.wait_between_commands()
        # 只有明确的 False 才算"没发出去";成功路径历史上返回 None,
        # 用 bool() 会把每次成功发送都判成取消(独立复查找出的严重回归)。
        return sent is not False

    def _async_verify_fn(self, command, player, meta):
        """后台验证:读日志 → 提取块 → 判定(按命令类型分流)。"""
        timeout = float(self.cfg.get("timeouts", {}).get("response_wait", 12.0))
        logger.log_debug(f"异步验证 ENTER command={command} 超时={timeout:.1f}s")
        started = time.monotonic()
        block = self.watcher.wait_for_chat_block(timeout=timeout, stop_check=self.is_paused)
        elapsed = time.monotonic() - started
        block_text = "None" if block is None else f"{len(block)}行"
        logger.log_debug(f"异步验证 EXIT 耗时={elapsed:.1f}s block={block_text}")
        if block is None:
            logger.log_warning(f"异步验证超时 command={command} timeout={timeout:.1f}s")
            return False, "等待响应超时", None
        if str(meta.get("msg", "")).startswith("/gc "):
            text = "\n".join(block)
            return (meta["msg"] in text), None if meta["msg"] in text else "广播内容未回显", None
        return True, None, None

    def close(self):
        """释放本 Runtime 持有的资源(幂等,异常不外抛)。

        ⚠ 每个资源的释放方法不同,不能一律调 .close():
          watcher        -> close()
          async_executor -> stop(timeout)   (ThreadPoolExecutor 封装)
          async_system   -> stop(timeout)   (**该类的停止方法叫 stop,没有 close**)
          listener       -> stop()
        旧实现按 "async_executor" 字符串分支、其余一律 .close(),于是对
        async_system 调了不存在的 close() → AttributeError 被下面 except 吞掉,
        表现为"关闭时看起来正常,但后台验证线程从未被停止"(静默泄漏)。
        """
        stoppers = {
            "watcher": lambda o: o.close(),
            "async_executor": lambda o: o.stop(timeout=3),
            "async_system": lambda o: o.stop(timeout=3),
            "listener": lambda o: o.stop(),
        }
        for name, stop in stoppers.items():
            obj = getattr(self, name, None)
            if obj is None:
                continue
            try:
                stop(obj)
            except Exception as e:
                logger.log_debug(f"Runtime.close: {name} 释放异常 {e!r}")


def _default_confirm(prompt, default=False):
    """默认的终端确认实现。空输入时按 default 取值。

    这是无超时的阻塞点(input),所以进入前/返回后各记一条 INFO ——
    无人值守时日志停在上一阶段也能看出"卡在等确认"。
    """
    suffix = " [Y/n] " if default else " [y/N] "
    logger.log_info(f"确认点 ENTER func=confirm prompt={prompt!r} 等待=用户输入(无限)")
    started = time.monotonic()
    try:
        ans = input(prompt + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt) as e:
        logger.log_warning(f"确认点中断({type(e).__name__}) prompt={prompt!r}，按拒绝处理: {e}")
        return False
    if not ans:
        result = default
    else:
        result = ans in ("y", "yes", "是", "好", "确认")
    logger.log_info(f"确认点 EXIT 耗时={time.monotonic() - started:.1f}s result={result}")
    return result


def _default_ask_text(prompt):
    """默认的终端原文输入:原样返回(不 trim、不布尔化),由调用方逐字比对。"""
    logger.log_info(f"确认点 ENTER func=ask_text prompt={prompt!r} 等待=用户输入(无限)")
    started = time.monotonic()
    try:
        text = input(prompt)
    except (EOFError, KeyboardInterrupt) as e:
        logger.log_warning(f"确认点中断({type(e).__name__}) prompt={prompt!r}，按拒绝处理: {e}")
        raise
    logger.log_info(
        f"确认点 EXIT 耗时={time.monotonic() - started:.1f}s result=原文({len(str(text))}字符)"
    )
    return text


def build_runtime(cfg, console_fn=None, ask_confirm=None, ask_text_fn=None):
    """构造**真实** Runtime:接真实日志、真实键鼠、真实 OCR。

    单独抽出来的目的:让 main() 不再亲手碰硬件 —— main() 只接受一个
    已构造好的 Runtime,于是测试可以传假 Runtime 驱动完整流程。

    console_fn 可注入输出函数(默认 config.console),便于测试捕获提示文本。
    ask_confirm / ask_text_fn 可注入确认实现:GUI 模式下由 ui.binding 注入
    模态对话框版本,控制台模式保持默认的 input() —— 这样 stages 里的确认点
    一行都不用改,也不会在 GUI 模式下还去读控制台。

    失败(SystemExit / 异常)由调用方处理;本函数不吞异常。
    """
    console_fn = console_fn or console
    build_started = time.monotonic()
    step_started = build_started
    logger.log_info("[阶段][ENTER] S7 build_runtime.watcher")
    watcher = LogWatcher(
        cfg["game_log"],
        encoding=cfg.get("log_encoding", "auto"),
        poll_interval_ms=int(cfg.get("timeouts", {}).get("poll_interval_ms", 300)),
    )
    watcher.open()
    console_fn(f"日志已连接: {cfg['game_log']} (编码 {watcher.encoding})")
    logger.log_info(
        f"[阶段][EXIT ] S7 build_runtime.watcher "
        f"耗时={time.monotonic() - step_started:.2f}s 编码={watcher.encoding}"
    )

    def is_paused():
        return _paused

    step_started = time.monotonic()
    logger.log_info("[阶段][ENTER] S7 build_runtime.human")
    human = HumanInput(cfg, stop_check=is_paused)
    console_fn(
        f"聊天键: {human.chat_key}(来自 {'options.txt' if cfg['chat_key'] == 'auto' else '配置'})"
    )

    # 聊天键是 enter 时警告但不拒绝:网易版 enter 是发送键不是开聊天栏键
    if human.chat_key == "enter":
        console_fn("⚠ 警告:聊天键设置为 'enter',网易版 enter 是发送键不是开聊天栏键,")
        console_fn("  可能导致 _open_chat 按下后聊天栏未开,后续重试死循环")
        console_fn("  建议:config.json chat_key 改为 't' 或 '/' 之类(读取开聊天栏的键)")
        console_fn("  仍然继续运行——若发生死循环,可按 u 暂停/Ctrl+C 退出")
    logger.log_info(
        f"[阶段][EXIT ] S7 build_runtime.human "
        f"耗时={time.monotonic() - step_started:.2f}s 聊天键={human.chat_key}"
    )

    # 界面状态检测器(菜单 OCR;不可用自动降级为纯试错式发送)
    step_started = time.monotonic()
    logger.log_info("[阶段][ENTER] S7 build_runtime.detector")
    detector = None
    sc = cfg.get("state_check", {})
    if sc.get("enabled", True):
        detector = GameStateDetector(
            sc.get("menu_keywords"),
            sc.get("chat_bar_keywords"),
            float(sc.get("cache_seconds", 3.0)),
            chat_bar_state=sc.get("chat_bar_state", "auto"),
        )
        if not detector.available:
            console_fn("⚠ 界面状态检测不可用(缺少 winsdk/系统OCR),使用试错式发送")
        else:
            console_fn(
                "界面状态检测已启用(菜单界面自动 Esc;发送:首条直接粘贴试探,"
                "无响应自动开聊天栏重试,仍失败暂停等待用户)"
            )
    logger.log_info(
        f"[阶段][EXIT ] S7 build_runtime.detector "
        f"耗时={time.monotonic() - step_started:.2f}s "
        f"available={getattr(detector, 'available', None)}"
    )

    step_started = time.monotonic()
    logger.log_info(f"[阶段][ENTER] S8 find_game_window keywords={list(cfg['window_keywords'])}")
    hwnd = find_game_window(cfg["window_keywords"])
    if hwnd:
        console_fn("游戏窗口已找到(将在倒计时结束后自动置前)")
    else:
        console_fn("⚠ 未找到游戏窗口(请确认游戏已启动、窗口标题包含关键词)")
    hwnd_text = f"hwnd={hwnd}" if hwnd else "未命中,转入等待用户输入"
    logger.log_info(
        f"[阶段][EXIT ] S8 find_game_window 耗时={time.monotonic() - step_started:.2f}s "
        f"结果={hwnd_text}"
    )

    step_started = time.monotonic()
    logger.log_info("[阶段][ENTER] S7 build_runtime.executor")
    executor = ActionExecutor(
        cfg, human, watcher, hwnd=hwnd, detector=detector, stop_check=is_paused
    )
    # 注入暂停恢复标志的消费函数:reset_ui / _send_with_gate 内部据此在
    # "恢复后首次发送前"执行 Esc+OCR 菜单重置。
    # 延迟 import:pause.py 依赖 runtime(拿 _pause_event / set_reset_needed),
    # 模块级互引会成环。
    from guild_auto.application.pause import _consume_reset_flag

    executor.set_reset_hook(_consume_reset_flag)
    logger.log_info(
        f"[阶段][EXIT ] S7 build_runtime.executor 耗时={time.monotonic() - step_started:.2f}s"
    )
    logger.log_info(
        f"[阶段][EXIT ] S7 build_runtime 总耗时={time.monotonic() - build_started:.2f}s"
    )

    return Runtime(
        cfg=cfg,
        watcher=watcher,
        human=human,
        executor=executor,
        detector=detector,
        hwnd=hwnd,
        ask_confirm=ask_confirm,
        ask_text_fn=ask_text_fn,
    )
