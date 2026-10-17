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
_reset_needed = False          # 暂停恢复后需要重置界面
_reset_lock = threading.Lock()  # 保护重置标志
_pause_key = None
_pause_key_name = "?"
_force_stop = False            # Ctrl+C 标志:sig handler 置位,阻塞调用据此中断

# 暂停事件:主线程后台链路在 wait/IO 阻塞时也会被 _toggle_pause 置位,
# wait_if_paused / wait_for_chat_block 等通过 _pause_event.wait() 立即响应。
# 修复"pynput on_press 里 raise KeyboardInterrupt 会被 pynput 静默吞掉"的根因:
# pynput 监听线程对回调抛出的异常只 log 不重抛,导致主线程根本收不到 KeyboardInterrupt。
# 现在 on_press 仅设事件,主线程的阻塞调用通过 event.wait() 主动感知。
_pause_event = threading.Event()


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
        _pause_key = getattr(keyboard.Key, _pause_key_name,
                             keyboard.KeyCode.from_char("u"))
    return _pause_key, _pause_key_name


def reset_state():
    """复位全部暂停相关状态(Ctrl+C 菜单选择"继续运行"后重启主流程)。

    不复位的话 _paused=True 残留,重启后第一个 wait_if_paused / _send_with_gate
    会立即卡在暂停门,表现为"选了继续但脚本不动"。
    """
    global _paused, _force_stop, _reset_needed
    with _pause_lock:
        _paused = False
    with _reset_lock:
        _reset_needed = False
    _force_stop = False
    _pause_event.clear()


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

    def __init__(self, cfg, watcher=None, human=None, executor=None, detector=None,
                 hwnd=None, ask_confirm=None, sleep=None, listener=None,
                 async_executor=None, async_enabled=False, stop_check=None):
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
        return _reset(self.hwnd if hwnd is None else hwnd,
                      self.detector if detector is None else detector)

    def confirm(self, prompt, default=False):
        """统一的阶段确认点。

        返回 True 表示用户同意继续;False 表示拒绝 —— 调用方必须立即停止后续操作
        (尤其是升/降/踢这类破坏性操作)。

        ask_confirm 契约:接收 (prompt, default),返回 bool。
        default=True 时(如"按回车继续"),空输入视为同意。
        """
        try:
            return bool(self.ask_confirm(prompt, default))
        except (EOFError, KeyboardInterrupt):
            # 终端被关闭 / 用户按 Ctrl+C:一律按"拒绝"处理 ——
            # 宁可停下,也不能在状态不明时继续发命令
            return False

    def ask(self, prompt, default=False):
        """向用户提问,返回 True=是 / False=否(或未确认)。

        与 confirm() 的区别:confirm 面向"阶段边界是否继续",语义是
        "默认同意、拒绝即停";ask 面向"要一个明确的是/否答案",
        默认值由调用方给(kick 二次确认默认 False = 不输字样就不执行)。

        同样兜底 EOF/Ctrl+C → False(拒绝),绝不在状态不明时继续发命令。
        """
        try:
            return bool(self.ask_confirm(prompt, default))
        except (EOFError, KeyboardInterrupt):
            return False

    def pause_gate(self):
        """发送前的暂停门:暂停时阻塞等待,恢复后重置窗口状态。

        所有阶段函数(查询/通知/执行)发送前都要过这道门 —— 规格要求
        "暂停期间绝不发出任何指令"。
        """
        if self.is_paused():
            console("执行暂停，等待恢复...")
            while self.is_paused():
                self.sleep(0.1)
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
            self.async_system = AsyncVerifySystem(
                verify_fn=self._async_verify_fn,
                task_timeout=float(self.cfg.get("timeouts", {})
                                   .get("response_wait", 12.0)) + 5.0)
            self.async_system.attach_sender(self._async_send_fn)
            self.async_system.start()
        return self.async_system.submit(command, player, meta or {})

    def _async_send_fn(self, command, player, meta):
        """后台发送:把已提交的指令发出去(不读日志)。"""
        self.human.send_chat_text(command, detector=self.detector,
                                  stop_check=self.is_paused)
        self.human.wait_between_commands()

    def _async_verify_fn(self, command, player, meta):
        """后台验证:读日志 → 提取块 → 判定(按命令类型分流)。"""
        block = self.watcher.wait_for_chat_block(
            timeout=float(self.cfg.get("timeouts", {}).get("response_wait", 12.0)),
            stop_check=self.is_paused)
        if block is None:
            return False, "等待响应超时", None
        if str(meta.get("msg", "")).startswith("/gc "):
            text = "\n".join(block)
            return (meta["msg"] in text), None if meta["msg"] in text else "广播内容未回显", None
        return True, None, None

    def close(self):
        """释放本 Runtime 持有的资源(幂等,异常不外抛)。"""
        for name in ("watcher", "async_executor", "async_system", "listener"):
            obj = getattr(self, name, None)
            if obj is None:
                continue
            try:
                obj.stop(timeout=3) if name == "async_executor" else obj.close()
            except Exception as e:
                logger.log_debug(f"Runtime.close: {name} 释放异常 {e!r}")


def _default_confirm(prompt, default=False):
    """默认的终端确认实现。空输入时按 default 取值。"""
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        ans = input(prompt + suffix).strip().lower()
    except EOFError:
        return False
    if not ans:
        return default
    return ans in ("y", "yes", "是", "好", "确认")


def build_runtime(cfg, console_fn=None):
    """构造**真实** Runtime:接真实日志、真实键鼠、真实 OCR。

    单独抽出来的目的:让 main() 不再亲手碰硬件 —— main() 只接受一个
    已构造好的 Runtime,于是测试可以传假 Runtime 驱动完整流程。

    console_fn 可注入输出函数(默认 config.console),便于测试捕获提示文本。

    失败(SystemExit / 异常)由调用方处理;本函数不吞异常。
    """
    console_fn = console_fn or console
    watcher = LogWatcher(
        cfg["game_log"],
        encoding=cfg.get("log_encoding", "auto"),
        poll_interval_ms=int(cfg.get("timeouts", {}).get("poll_interval_ms", 300)),
    )
    watcher.open()
    console_fn(f"日志已连接: {cfg['game_log']} (编码 {watcher.encoding})")

    def is_paused():
        return _paused

    human = HumanInput(cfg, stop_check=is_paused)
    console_fn(f"聊天键: {human.chat_key}(来自 {'options.txt' if cfg['chat_key']=='auto' else '配置'})")

    # 聊天键是 enter 时警告但不拒绝:网易版 enter 是发送键不是开聊天栏键
    if human.chat_key == "enter":
        console_fn("⚠ 警告:聊天键设置为 'enter',网易版 enter 是发送键不是开聊天栏键,")
        console_fn("  可能导致 _open_chat 按下后聊天栏未开,后续重试死循环")
        console_fn("  建议:config.json chat_key 改为 't' 或 '/' 之类(读取开聊天栏的键)")
        console_fn("  仍然继续运行——若发生死循环,可按 u 暂停/Ctrl+C 退出")

    # 界面状态检测器(菜单 OCR;不可用自动降级为纯试错式发送)
    detector = None
    sc = cfg.get("state_check", {})
    if sc.get("enabled", True):
        detector = GameStateDetector(
            sc.get("menu_keywords"), sc.get("chat_bar_keywords"),
            float(sc.get("cache_seconds", 3.0)),
            chat_bar_state=sc.get("chat_bar_state", "auto"),
        )
        if not detector.available:
            console_fn("⚠ 界面状态检测不可用(缺少 winsdk/系统OCR),使用试错式发送")
        else:
            console_fn("界面状态检测已启用(菜单界面自动 Esc;发送:首条直接粘贴试探,"
                       "无响应自动开聊天栏重试,仍失败暂停等待用户)")

    hwnd = find_game_window(cfg["window_keywords"])
    if hwnd:
        console_fn("游戏窗口已找到(将在倒计时结束后自动置前)")
    else:
        console_fn("⚠ 未找到游戏窗口(请确认游戏已启动、窗口标题包含关键词)")

    executor = ActionExecutor(cfg, human, watcher, hwnd=hwnd, detector=detector,
                              stop_check=is_paused)
    # 注入暂停恢复标志的消费函数:reset_ui / _send_with_gate 内部据此在
    # "恢复后首次发送前"执行 Esc+OCR 菜单重置。
    # 延迟 import:pause.py 依赖 runtime(拿 _pause_event / set_reset_needed),
    # 模块级互引会成环。
    from guild_auto.application.pause import _consume_reset_flag
    executor.set_reset_hook(_consume_reset_flag)

    return Runtime(cfg=cfg, watcher=watcher, human=human, executor=executor,
                   detector=detector, hwnd=hwnd)


