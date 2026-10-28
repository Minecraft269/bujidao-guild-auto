# -*- coding: utf-8 -*-
"""
pause.py —— 暂停机制与 Ctrl+C 中断菜单
=====================================
从 main.py 按关注点拆出（原 main.py 1356 行，暂停/待办/模拟数据/编排混在一起）。

为什么单独成模块：
  * 暂停状态经 pynput 监听线程写、主线程与后台线程读，是**跨线程共享状态**，
    必须有自己的单一所有者（runtime.py）与自己的单元测试，不能混在入口文件里。
  * Ctrl+C 菜单（msvcrt 轮询）与暂停恢复重置都是"人对终端做的操作"，
    与业务流程无关，混在 main() 里会让 main 既不可读也不可测。

状态所有权：_paused / _reset_needed / _force_stop / _pause_event / _pause_key
全部住在 runtime.py。本模块只**调用**它们的访问器，不重新定义 ——
否则 main 与 runtime 会各持一份副本，暂停键静默失效（这正是拆分后踩过的坑：
`from runtime import _paused` 拿到的是值副本，`global _paused` 写的是自己的副本，
runtime._paused 永远 False，rg.is_paused() 永远 False，暂停门形同虚设）。
"""

import time

from pynput import keyboard
import pyautogui

from guild_auto.infrastructure import logger
from guild_auto.application import runtime
from guild_auto.config import console
from guild_auto.infrastructure.game_state import prepare_chat_state
from guild_auto.application.runtime import (
    _pause_event,
    consume_reset_flag,
    set_reset_needed,
)
from guild_auto.infrastructure.window_input import activate_window

__all__ = [
    "start_pause_listener",
    "wait_if_paused",
    "reset_window_state_after_pause",
    "_consume_reset_flag",
    "_ctrlc_pause_menu",
    "_ctrlc_pause_menu_fallback",
    "_signal_handler",
    "install_signal_handlers",
]


def _toggle_pause():
    """切换暂停状态(pynput 监听线程调用)。

    修复"暂停键失效":按 u 后若在 on_press 回调里 raise KeyboardInterrupt，
    会被 pynput 监听线程静默吞掉——主线程根本收不到。现在只改 runtime 的
    _paused + _pause_event，主线程在阻塞调用中(event.wait)能立即响应。
    """
    paused_now = not runtime.is_paused()
    runtime.set_paused(paused_now)
    if paused_now:
        _pause_event.set()  # 置位 → 所有阻塞中的 event.wait() 立即返回
    else:
        _pause_event.clear()  # 复位 → 下次 event.wait() 才会阻塞
        set_reset_needed(True)  # 恢复后需要重置界面
    console(f">>> 脚本已{'暂停' if paused_now else '继续'}(按 {runtime.pause_key_name()} 切换) <<<")


def _on_press(key):
    """pynput 全局按键回调:暂停键切换暂停;Ctrl+C 转成 SIGINT 走菜单。"""
    try:
        if key == runtime.pause_key():
            _toggle_pause()
            return
        # 修复"Ctrl+C 被错误拦截":pynput on_press 里 raise KeyboardInterrupt 会被
        # pynput listener 静默吞掉;pynput 也不让 Ctrl+C 正常转 SIGINT(Windows 下
        # SIGINT 必须由控制台子系统分发)。改:主动 os.kill(CTRL_C_EVENT)。
        if hasattr(key, "char") and key.char == chr(3):
            import os as _os
            import signal as _sig

            logger.log_warning(
                "用户按 Ctrl+C(pynput 拦截),主动 os.kill(CTRL_C_EVENT)→SIGINT 走菜单"
            )
            try:
                _os.kill(_os.getpid(), _sig.CTRL_C_EVENT)
            except Exception as e:
                logger.log_warning(
                    f"os.kill(CTRL_C_EVENT) 失败({e!r}),"
                    "回退为 _force_stop=True + _pause_event.set()"
                )
                runtime.set_force_stop(True)
                runtime.set_paused(True)
                _pause_event.set()
            return
        try:
            key_repr = key.char if hasattr(key, "char") and key.char else str(key)
        except Exception:
            key_repr = str(key)
        logger.log_debug(f"[键盘] 按键: {key_repr!r}")
    except Exception as e:
        # 不静默吞——记录异常(防止 pynput 后台挂掉但日志无声)
        logger.log_debug(f"[键盘] _on_press 异常: {type(e).__name__}: {e}")


def start_pause_listener(pause_key_cfg):
    """启动暂停键监听(pynput)。支持单字符('u')或特殊键名('esc')。"""
    runtime.set_pause_key_cfg(pause_key_cfg)
    listener = keyboard.Listener(on_press=_on_press)
    listener.daemon = True
    listener.start()
    return listener


def wait_if_paused():
    """暂停门:暂停时阻塞,直到用户按暂停键恢复。

    修复"Ctrl+C 死循环重发命令":signal handler 把 _force_stop 置位后,
    若这里只是 return,上层 while 循环会把"中断"当普通 interrupted 分支
    continue → 永远不退出 query_guild_list,死循环重发同一命令
    (latest.log 实测发过 ~30 次 /guild list)。
    所以看到 _force_stop 必须 raise KeyboardInterrupt 让顶层 except 接住。
    """
    logged = False
    while True:
        if runtime.is_force_stop():
            if not logged:
                logger.log_warning(
                    "wait_if_paused: 收到 _force_stop(Ctrl+C),抛 KeyboardInterrupt 进菜单"
                )
                logged = True
            raise KeyboardInterrupt("Ctrl+C 触发 _force_stop,冒泡到顶层 except")
        if not runtime.is_paused():
            if logged:
                logger.log_debug("暂停解除,流程继续")
            break
        # 事件驱动阻塞:0.1s 内暂停事件置位则立即唤醒
        _pause_event.wait(timeout=0.1)


def reset_window_state_after_pause(hwnd, detector):
    """执行阶段暂停恢复后:消费 _reset_needed 并重置窗口状态。

    与查询阶段 send_and_capture 内的 _reset_needed 消费逻辑保持等效:
      * 重新激活游戏窗口(暂停期间用户可能切走)
      * 按 Esc 关闭可能残留的菜单/聊天栏
      * OCR 检测并处理菜单界面
    返回 True 表示本次确有消费并重置;无残留标志时返回 False(不动作)——
    没有残留标志时反复按 Esc 才是 bug。
    """
    if not consume_reset_flag():
        return False
    console("  (执行暂停恢复,重置窗口状态...)")
    # 整段重置是**尽力而为**:恢复后马上要发下一条命令,这里按 Esc 失败、
    # OCR 抛异常都不该把主流程打断 —— 命令发出去仍可由重试链补救,
    # 而异常冒泡会直接终结整个批次。异常一律记日志不抛。
    try:
        if hwnd is not None:
            activate_window(hwnd)
        pyautogui.press("esc")  # 关闭可能残留的菜单/聊天栏
        time.sleep(0.2)
        if detector is not None:
            prepare_chat_state(detector, console)
        time.sleep(0.5)  # 等待界面稳定后再发送命令
    except Exception as e:
        logger.log_warning(f"暂停恢复重置窗口异常(忽略,继续发送): {e!r}")
    return True


def _consume_reset_flag():
    """线程安全地读取并复位 _reset_needed。

    供 ActionExecutor.reset_ui 注入使用,使异步工作线程在恢复后的首次发送前
    也能完成窗口重置。就是 runtime.consume_reset_flag 的别名,
    保留本名是为了不破坏历史调用点。
    """
    return consume_reset_flag()


def _ctrlc_pause_menu():
    """Ctrl+C 后的交互菜单:回车=退出;输入 c 回车=继续运行。
    返回 True 表示用户选择继续。

    修复"Ctrl+C 立即退出":之前用 select.select([sys.stdin]) → OSError [WinError 10038];
    改用 thread+Queue 后,SIGINT 仍会立即传到主线程。现在用 msvcrt.kbhit()
    在主线程直接轮询按键(Windows 原生),并在调用前后切换 SIGINT 处理:
    菜单期间恢复默认(再按 Ctrl+C 立即退出),其余时段仍是 _signal_handler。
    """
    console("\n" + "=" * 56)
    console("脚本已被 Ctrl+C 中断")
    console("直接回车 = 退出;输入 c 回车 = 继续运行(未完成的操作会经待办续做,不会重复执行)")
    console("=" * 56)
    import signal as _sig

    _prev_handler = None
    try:
        _prev_handler = _sig.signal(_sig.SIGINT, _sig.SIG_DFL)
    except Exception:
        _prev_handler = None
    buf = []
    deadline = time.time() + 5.0
    try:
        import msvcrt

        while time.time() < deadline:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"):
                    break  # 回车:确认输入
                if ch == "\x03":
                    # 菜单期间再按 Ctrl+C:立即退出(不再嵌套到外层 except)
                    logger.log_info("Ctrl+C 菜单期间再按 Ctrl+C,立即退出")
                    return False
                if ch == "\x08":
                    if buf:
                        buf.pop()
                elif ch.isprintable():
                    buf.append(ch)
            time.sleep(0.05)
    except ImportError:
        # 非 Windows:回退到 thread+Queue 模式
        logger.log_warning("msvcrt 不可用(非 Windows?),回退到 thread+Queue 读 stdin")
        return _ctrlc_pause_menu_fallback()
    finally:
        if _prev_handler is not None:
            try:
                _sig.signal(_sig.SIGINT, _prev_handler)
            except Exception:
                pass
    if not buf:
        logger.log_info("中断菜单 5s 未按键,默认退出")
        return False
    ans = "".join(buf).strip().lower()
    return ans == "c"


def _ctrlc_pause_menu_fallback():
    """非 Windows 平台的 Ctrl+C 菜单回退实现:thread+Queue 读 stdin。"""
    import queue as _queue
    import threading

    q = _queue.Queue()

    def _read():
        try:
            q.put(input("退出请直接回车,继续请输入 c: ").strip().lower())
        except BaseException:
            # 关键:捕获 KeyboardInterrupt/EOFError 后必须塞个空串,否则 q.empty()
            # 一直 True → 主线程走超时分支(看起来像"立即退出"但实际是超时)
            q.put("")

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout=5.0)
    if t.is_alive():
        return False
    if q.empty():
        return False
    ans = q.get_nowait()
    return ans == "c"


def _signal_handler(_sig, _frame):
    """Ctrl+C 信号处理:置暂停 + force_stop,并 raise KeyboardInterrupt 进菜单。

    Windows 上 signal handler 在主线程下次字节码边界重新抛出此异常,
    main() 调用栈收到后冒泡到顶层 except KeyboardInterrupt 接住,走中断菜单。
    """
    runtime.set_paused(True)
    set_reset_needed(True)
    runtime.set_force_stop(True)
    logger.log_warning("收到 SIGINT(用户按 Ctrl+C),_paused=True + raise KeyboardInterrupt")
    raise KeyboardInterrupt


def install_signal_handlers():
    """注册 SIGINT/SIGTERM 处理。返回被替换的旧 handler 字典(供测试还原)。"""
    import signal as _sig

    prev = {}
    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(_sig, name, None)
        if signum is None:
            continue
        try:
            prev[name] = _sig.signal(signum, _signal_handler)
        except (ValueError, OSError, RuntimeError):
            # 非主线程注册会 ValueError;某些环境无控制台信号会 OSError
            pass
    return prev


# 模块导入即注册(与拆分前的 main.py 行为一致:import main 时就已挂钩)
_SIGNAL_HANDLERS_INSTALLED = install_signal_handlers()
