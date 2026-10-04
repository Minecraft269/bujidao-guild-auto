# -*- coding: utf-8 -*-
"""
main.py —— 布吉岛公会管理脚本(全自动版)入口
=============================================
流程:读取配置 → 初始化 → 窗口置前 → /guild list → 逐人 /guild member → 判定(继承规则)→ /gc 通知 → 执行 升/降/踢 → 输出报告

运行:
    python main.py [--config 配置文件] [--admin 管理员] [--log 日志路径] [--options 游戏配置路径] [--dry-run]

首次运行自动生成 config.json;所有业务参数均在配置中,请阅读《配置说明.md》。
"""
import argparse
import json
import os
import re
import sys
import threading
import time
import string
import random

from pynput import keyboard
import pyautogui

import config as config_mod
from actions import build_notification, decide_actions, new_rank_after
from action_executor import ActionExecutor
from async_executor import AsyncCommandExecutor
import logger
from config import console
from game_state import GameStateDetector, prepare_chat_state
from log_watcher import LogWatcher, detect_encoding
from parsers import ParseError, parse_guild_list, parse_guild_member
from reporter import write_action_report, write_contribution_report
from window_input import HumanInput, activate_window, find_game_window

DEFAULT_CONFIG_FILE = "config.json"

# ---------------------------------------------------------------------------
# 暂停机制(原脚本继承:pynput 全局监听)
# ---------------------------------------------------------------------------
_paused = False
_pause_lock = threading.Lock()
_reset_needed = False          # 暂停恢复后需要重置界面
_reset_lock = threading.Lock()  # 保护重置标志
# 暂停事件:主线程后台链路在 wait/IO 阻塞时也会被 _toggle_pause 置位,
# wait_if_paused / wait_for_chat_block 等通过 _pause_event.wait() 立即响应。
# 修复"pynput on_press 里 raise KeyboardInterrupt 会被 pynput 静默吞掉"的根因:
# pynput 监听线程对回调抛出的异常只 log 不重抛,导致主线程根本收不到 KeyboardInterrupt。
# 现在 on_press 仅设事件,主线程的阻塞调用通过 event.wait() 主动感知。
_pause_event = threading.Event()


def _toggle_pause():
    # 修复"暂停键失效":按 u 后 pynput on_press 回调里 raise KeyboardInterrupt 会被 pynput
    # 监听线程静默吞掉——主线程根本收不到。现在改为仅修改 _paused + _pause_event,
    # 主线程在阻塞调用中(等待时 sleep 改 event.wait(timeout) 形式)能立即响应。
    global _paused, _reset_needed
    with _pause_lock:
        _paused = not _paused
        if _paused:
            _pause_event.set()      # 置位→所有阻塞中的 event.wait() 立即返回 True
        else:
            _pause_event.clear()    # 复位→下次 event.wait() 才会阻塞
            with _reset_lock:
                _reset_needed = True
    console(f">>> 脚本已{'暂停' if _paused else '继续'}(按 {_pause_key_name} 切换) <<<")


def _on_press(key):
    try:
        if key == _pause_key:
            _toggle_pause()
            # 不再 raise KeyboardInterrupt(pynput 静默吞掉)。
            # _toggle_pause 已设 _pause_event,主线程阻塞调用立即感知。
            return
        # 修复"Ctrl+C 被错误拦截":pynput on_press 里 raise KeyboardInterrupt
        # 会被 pynput listener 静默吞掉,主线程根本收不到,菜单不弹出。pynput 也不让
        # Ctrl+C 正常转 SIGINT(Windows 下 SIGINT 必须由控制台子系统分发,pynput 抢先
        # 拿走了按键事件 SIGINT 路径不会触发)。改:用 os.kill(self_pid, CTRL_C_EVENT)
        # 主动向本进程发一个 CTRL_C_EVENT → Windows 把它转成 SIGINT → signal handler
        # raise KeyboardInterrupt → 顶层 except 接住走菜单。pynput 拦截后我们不
        # 屏蔽 SIGINT(原 listener 仍会跑回调,但 _signal_handler 也会被触发——双路保险)。
        if (hasattr(key, "char") and key.char == chr(3)):
            import os as _os
            import signal as _sig
            logger.log_warning("用户按 Ctrl+C(pynput 拦截),主动 os.kill(CTRL_C_EVENT)→SIGINT 走菜单")
            try:
                _os.kill(_os.getpid(), _sig.CTRL_C_EVENT)
            except Exception as e:
                logger.log_warning(f"os.kill(CTRL_C_EVENT) 失败({e!r}),回退为 _force_stop=True + _pause_event.set()")
                # 回退路径:虽然不 raise 也能让 wait_if_paused 主动抛 KeyboardInterrupt
                global _force_stop
                _force_stop = True
                with _pause_lock:
                    _paused = True
                _pause_event.set()
            return
        # 记录其他按键便于排查
        try:
            key_repr = key.char if hasattr(key, "char") and key.char else str(key)
        except Exception:
            key_repr = str(key)
        logger.log_debug(f"[键盘] 按键: {key_repr!r}")
    except Exception as e:
        # 不静默吞——记录异常(防止 pynput 后台挂掉但日志无声)
        logger.log_debug(f"[键盘] _on_press 异常: {type(e).__name__}: {e}")


_pause_key = None
_pause_key_name = "?"


def start_pause_listener(pause_key_cfg):
    """启动暂停键监听(pynput)。支持单字符('u')或特殊键名('esc')。"""
    global _pause_key, _pause_key_name
    _pause_key_name = str(pause_key_cfg)
    if len(str(pause_key_cfg)) == 1:
        _pause_key = keyboard.KeyCode.from_char(str(pause_key_cfg))
    else:
        _pause_key = getattr(keyboard.Key, str(pause_key_cfg), keyboard.KeyCode.from_char("u"))
    listener = keyboard.Listener(on_press=_on_press)
    listener.daemon = True
    listener.start()
    return listener


def wait_if_paused():
    # 修复"Ctrl+C 死循环重发命令" + "暂停键失效":
    # signal handler / pynput on_press 收到 Ctrl+C 后:
    #   * 旧版:wait_if_paused 看到 _force_stop=True 立即 return → 上层 while 循环
    #     把"暂停中断"当作普通"interrupted"分支 continue → 永远不退出 query_guild_list
    #     死循环重发同一命令(latest.log 行 47-205 共发了 ~30 次 /guild list)
    #   * 修复:看到 _force_stop=True 必须 raise KeyboardInterrupt 让顶层 except
    #     KeyboardInterrupt 接住进 _ctrlc_pause_menu
    # 用户按 u(非 Ctrl+C):pynput 仅 set _paused + _pause_event,wait_if_paused
    # 用 _pause_event.wait(timeout=0.1) 阻塞等到 _paused=False(用户再按 u 恢复)
    logged = False
    while True:
        with _pause_lock:
            if _force_stop:
                if not logged:
                    logger.log_warning("wait_if_paused: 收到 _force_stop(Ctrl+C),抛 KeyboardInterrupt 进菜单")
                    logged = True
                raise KeyboardInterrupt("Ctrl+C 触发 _force_stop,冒泡到顶层 except")
            if not _paused:
                if logged:
                    logger.log_debug("暂停解除,流程继续")
                break
        # 事件驱动阻塞:0.1s 内暂停事件置位则立即唤醒(原来 sleep 0.05 后还要再 check)
        _pause_event.wait(timeout=0.1)
    # _paused=False:重置界面——只在 _reset_needed=True 时做(避免 wait_if_paused
    # 被循环重入时反复按 Esc)
    try:
        from main import hwnd as _hwnd, detector as _detector, _reset_needed as _rn
    except ImportError:
        _hwnd, _detector, _rn = None, None, False
    if _rn:
        try:
            if _hwnd is not None:
                activate_window(_hwnd)
            pyautogui.press("esc")
            time.sleep(0.1)
            if _detector is not None:
                prepare_chat_state(_detector, console)
            with _reset_lock:
                _reset_needed = False
            logger.log_info("暂停恢复:界面已重置(activate+Esc+OCR)")
        except Exception as e:
            logger.log_warning(f"暂停恢复重置异常(忽略): {e}")
        if not logged:   # 仅首次打印,避免长时间暂停刷屏
            logger.log_debug("wait_if_paused: 检测到暂停状态,阻塞等待恢复 ...")
            logged = True
        time.sleep(0.1)


def reset_window_state_after_pause(hwnd, detector):
    """执行阶段暂停恢复后:消费 _reset_needed 并重置窗口状态。

    与查询阶段 send_and_capture 内的 _reset_needed 消费逻辑保持等效:
      * 重新激活游戏窗口(暂停期间用户可能切走)
      * 按 Esc 关闭可能残留的菜单/聊天栏
      * OCR 检测并处理菜单界面
    执行阶段不走 send_and_capture,若不在此消费,_reset_needed 会残留,
    恢复后直接发送命令时窗口状态未知 → 命令可能发错目标。
    返回 True 表示本次确有消费并重置;无残留标志时返回 False(不动作)。"""
    global _reset_needed
    with _reset_lock:
        if not _reset_needed:
            return False
        _reset_needed = False
    console("  (执行暂停恢复,重置窗口状态...)")
    if hwnd is not None:
        activate_window(hwnd)
    pyautogui.press("esc")   # 关闭可能残留的菜单/聊天栏
    time.sleep(0.2)
    if detector is not None:
        prepare_chat_state(detector, console)
    time.sleep(0.5)          # 等待界面稳定后再发送命令
    return True


def _consume_reset_flag():
    """线程安全地读取并复位 _reset_needed(供 ActionExecutor.reset_ui 注入使用,
    使异步工作线程在恢复后的首次发送前也能完成窗口重置)。"""
    global _reset_needed
    with _reset_lock:
        if not _reset_needed:
            return False
        _reset_needed = False
        return True


# ---------------------------------------------------------------------------
# 管理员名自动识别与待办持久化(断点续做)
# ---------------------------------------------------------------------------
def auto_detect_admin(cfg):
    """从游戏日志启动参数 '--username, <ID>' 识别管理员名;配置已填则直接返回。"""
    if cfg.get("admin"):
        return cfg["admin"]
    path = cfg.get("game_log", "")
    try:
        with open(path, encoding=detect_encoding(path, cfg.get("log_encoding", "auto")),errors="replace") as f:
            for line in f:
                m = re.search(r"--username\s*[,，]?\s*([^\s,，]+)", line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return ""


def pending_path_of(cfg):
    return os.path.join(cfg.get("report_dir", "reports"), "pending_actions.json")


def load_pending(path):
    """读取待办文件;不存在或损坏返回空列表。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("items", []) if isinstance(data, dict) else []
        return [it for it in items if isinstance(it, dict) and it.get("player") and it.get("actions")]
    except (OSError, json.JSONDecodeError):
        return []


def save_pending(path, guild_name, items):
    """写入待办文件(已完成项已从 items 中移除,未完成的保留)。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"guild_name": guild_name or "", "items": items}, f, ensure_ascii=False, indent=2)


def run_pending(cfg, watcher, human, hwnd, executor, path):
    """断点续做:读取上次未完成(或失败)的操作,询问后执行,并更新待办文件。
    返回 (continue_stat, remaining):是否继续新的统计 + 仍未完成的项。
    不进行任何新的统计查询。"""
    items = load_pending(path)
    if not items:
        return True, []
    sw = cfg["switches"]
    console(f"\n=== 发现上次未完成操作 {len(items)} 条(不进行新的统计查询)===")
    for it in items:
        console(f"  {it['player']}: {'/'.join(it['actions'])} | 等级 {it.get('rank','?')} | "
                f"周贡献 {it.get('weekly','?')} | 状态 {it.get('status','pending')}")
    do_exec = False
    if sw["execution_enabled"]:
        try:
            ans = input("是否执行以上操作?输入 y 或直接回车=执行,输入 n=跳过(保留待下次): ").strip().lower()
        except EOFError:
            ans = "n"
        do_exec = (ans != "n")
    else:
        console("(dry-run:不执行,保留待下次)")

    remaining = []
    if do_exec:
        for it in items:
            wait_if_paused()
            # 暂停恢复后重置窗口状态(与主执行循环一致)
            reset_window_state_after_pause(executor.hwnd, executor.detector)
            console(f"  执行 {it['player']}: {it['actions']}")
            results = executor.execute_actions(it["player"], it["actions"])
            done = sum(1 for _, ok, _ in results if ok)
            if done == len(results):
                console(f"    ✓ {it['player']} 全部完成,已从待办移除")
                continue  # 完成 → 不写回待办
            it["actions"] = it["actions"][done:]  # 剩余动作保留,下次继续
            it["status"] = "failed"
            failed = next(((a, e) for a, ok, e in results if not ok), None)
            it["error"] = f"执行失败({failed[0]}): {failed[1]}" if failed else "未执行"
            console(f"    ⚠ {it['player']} 未完成: {it['error']}")
            remaining.append(it)
    else:
        remaining = items  # 用户选择跳过:全部保留

    save_pending(path, cfg.get("guild_name") or "", remaining)
    if remaining:
        console(f"仍有 {len(remaining)} 条未完成,已保留在待办文件,下次启动可继续")
    else:
        console("全部待办操作已完成")
    try:
        ans = input("直接退出(回车)还是继续新的统计操作(输入 c 回车)?: ").strip().lower()
    except EOFError:
        ans = ""
    return ans != "c", remaining


# ---------------------------------------------------------------------------
# 查询原语
# ---------------------------------------------------------------------------
def _join_with_progress(executor, label, expected_n=None, poll_interval=0.6):
    """替代 async_executor.join():主线程在等后台完成期间每 0.6s 打印一次进度,
    修复"主线程似乎仍在等待验证"——用户看到主线程在做事(虽然实际是轮询)。
    退出条件:executor 内部 queue 已空 + 所有 player 事件已置位。"""
    import time as _t
    last_done = -1
    last_print_ts = 0.0
    # 直接轮询 _player_events + queue:当所有 player 事件已置位且 queue 空时退出
    while True:
        # 统计已完成数量:_player_results 大小
        with executor._player_results_lock:
            done_n = sum(1 for r in executor._player_results.values() if r is not None)
        if done_n != last_done or _t.time() - last_print_ts > 2.0:
            if expected_n is not None:
                console(f"  [{label}] 后台进度: {done_n}/{expected_n} 已验证...")
            else:
                console(f"  [{label}] 后台进度: {done_n} 条已验证...")
            last_done = done_n
            last_print_ts = _t.time()
        # 退出条件:queue 空 + 全部 player 事件已置位(用 _queue 状态判)
        if executor._queue.qsize() == 0 and done_n >= (expected_n or 0):
            # 再 wait 一次让 worker 把最后任务消费完
            executor._queue.join()
            break
        _t.sleep(poll_interval)
    # 最后一次打印
    with executor._player_results_lock:
        done_n = sum(1 for r in executor._player_results.values() if r is not None)
    console(f"  [{label}] 后台验证全部完成(共 {done_n} 条)")


def _pause_until_resume():
    """置暂停标志并阻塞,等待用户按暂停键恢复(用于发送无响应时人工介入)。
    Debug 模式(cfg.debug.enabled)按 u 后直接退出,不等第二次 u 继续 — 避免
    暂停后仍持续提交新指令(用户原话)。
    """
    global _paused
    with _pause_lock:
        _paused = True
    console(">>> 脚本已暂停,请检查游戏状态后按暂停键继续 <<<")
    if cfg.get("debug", {}).get("enabled", False):
        console("Debug 模式:按 u 后立即退出,不重试。")
        sys.exit(0)
    wait_if_paused()


def send_and_capture(watcher, human, hwnd, cmd, timeout, verbose=True, detector=None, try_wait=3.0, try_times=3, first_command=False, stop_check=None):
    """发送命令并等待日志响应块(消息级切分)。
    
    返回 (block, interrupted)：
        block: 响应块列表或 None
        interrupted: 是否因暂停而中断（若中断，调用方应重置重试计数）

    完整流畅流程:

    1. 打开脚本后的【第一条命令】:直接粘贴+回车尝试发送(此时聊天栏状态未知,试探);
    2. 不成功 → 打开聊天栏 → 粘贴 → 回车发送(重试,查询类命令重复发送无害);
    3. 仍不成功 → 检查是否菜单界面(OCR):
        是 → 按 Esc 关闭菜单 → 打开聊天栏重试;
        否 → 暂停脚本,提示用户检查游戏状态后按暂停键继续;
    4. 用户恢复后按正常流程继续(本次返回 None,由调用方重试/跳过)。
    """
    if hwnd is not None:
        activate_window(hwnd)
    if verbose:
        console(f"  → {cmd}")

    try_times = max(1, int(try_times))

    # ---- 如果暂停恢复后需要重置界面，则执行一次 Esc 并处理菜单 ----
    global _reset_needed
    with _reset_lock:
        if _reset_needed:
            console("  (暂停恢复，重置界面状态...)")
            pyautogui.press("esc")
            time.sleep(0.1)
            if detector is not None:
                prepare_chat_state(detector, console)
            _reset_needed = False

    # ---- first_command=True 跳过试探(用户原话) ----
    # 用户原话"按 Esc 关了聊天栏→按回车开了→按回车又关"死循环:
    # 用户的 chat_key=回车,在游戏中(开聊天栏/关聊天栏/发送)行为不同,不可靠开栏。
    # 完全跳过试探,直接走标准路径(后续重试已包含 _open_chat + 粘贴 + 发送)
    # first_command=True 试探:先 paste_and_enter(不按 chat_key,假设聊天栏已开)
    # 若游戏已开聊天栏,粘贴+回车直接发送;否则读不到回显
    if first_command:
        if verbose:
            console("  (first_command: 试探式直接粘贴+回车,假设聊天栏已开,失败再走标准路径)")
        # 试探:仅 paste + enter(不按 chat_key,避免按错 chat_key 引发用户原话的死循环)
        # 每步前 stop_check 守护(防止按 u 后还在粘贴/enter)
        # 修复"first_command 过早获取日志":先 sleep 200ms 让前次命令残留日志清空,
        # 避免 read_for_chat_block 读到前一次命令的残留回显
        time.sleep(0.2)
        # paste_and_enter 现在返回 True(执行到底)/ False(被 stop_check 打断)
        # 之前是隐式 None → `if not ok: pass` 把 None 当 False → 跳过 wait_for_chat_block
        # → 用户看不到任何 READ 日志。改为显式 if not False 才走等待,None 视作成功
        ok = human.paste_and_enter(cmd, stop_check=stop_check)
        if ok is not False:
            # min_wait_after_start=0.5:让游戏在 500ms 内至少"确认收到"了 /guild 指令
            # ——防止 discard_buffer 之后又有少量"启动期"日志延迟写入被错认为响应
            block = watcher.wait_for_chat_block(timeout=min(timeout, try_wait), stop_check=stop_check,
                                                 min_wait_after_start=0.5)
            if block is not None:
                return block, False
        if verbose:
            console("  (试探未响应,改走标准开栏路径)")

    # ---- 后续重试(暂停恢复后继续) ----
    # 修复"暂停后脚本仍在运行":主循环每次 attempt 前 wait_if_paused() — 用户
    # 按 u 期间彻底阻塞;retry 失败后 wait_if_paused()(替换原 sleep(1.0))
    for attempt in range(1, try_times + 1):
        wait_if_paused()  # 每次 attempt 前等暂停恢复
        # 修复"Ctrl+C 不暂停":signal handler 设 _force_stop=True 后立即返回
        if _force_stop:
            return None, True
        if stop_check and stop_check():
            return None, True

        # 仅 attempt==1 调 prepare_chat_state(OCR 可能误判菜单按 Esc,反复按 Esc 死循环);
        # attempt>1 跳过(已开聊天栏或确认非菜单状态,_open_chat 内 detector 仍会查 chat_open)
        if attempt == 1 and detector is not None:
            prepare_chat_state(detector, console)

        # 打开聊天栏 → 粘贴 → 回车发送
        human.send_chat_text(cmd, detector=detector, stop_check=stop_check)

        wait = timeout if attempt >= try_times else min(timeout, try_wait)
        # attempt==1 时设置 min_wait_after_start=0.3:让游戏在 300ms 内"确认收到"
        # ——防止 discard_buffer 之后又有少量"启动期"日志延迟写入被错认为响应
        min_wait = 0.3 if attempt == 1 else 0.0
        block = watcher.wait_for_chat_block(timeout=wait, stop_check=stop_check,
                                             min_wait_after_start=min_wait)
        if block is not None:
            return block, False

        if verbose:
            console(f"  (第{attempt}次无响应,等待暂停恢复...)")
        # retry 失败后等暂停恢复(替换固定 sleep(1.0),用户按 u 时不继续跑)
        if stop_check and stop_check():
            return None, True

    # 所有尝试均失败，暂停等待用户介入
    console("⚠ 连续多次发送无响应，请检查游戏状态（可能卡菜单、掉线等），按暂停键恢复后重试。")
    _pause_until_resume()
    return None, False


def query_guild_list(cfg, watcher, human, hwnd, detector=None, stop_check=None):
    """查询并解析 /guild list。失败抛 RuntimeError。"""
    cmds = cfg["commands"]
    cmd = cmds["guild_list"]
    retries = int(cfg["timeouts"].get("max_retries", 3))
    timeout = float(cfg["timeouts"].get("response_wait", 12.0))
    try_wait = float(cfg["timeouts"].get("try_wait", 6.0))
    try_times = int(cfg["timeouts"].get("try_times", 3))
    last_err = None
    attempt = 1
    used_first_paste = False   # 首条"直接粘贴试探"仅用一次;暂停恢复后不再走试探分支
    while attempt <= retries:
        wait_if_paused()
        # 修复"first_command 过早获取日志":attempt==1 启动前丢弃前次命令残留
        if attempt == 1:
            watcher.discard_buffer()
        console(f"[{attempt}/{retries}] 发送 {cmd} ...")
        block, interrupted = send_and_capture(
            watcher, human, hwnd, cmd, timeout,
            detector=detector, try_wait=try_wait, try_times=try_times,
            first_command=(attempt == 1 and not used_first_paste),  # 仅首次启动直接粘贴试探;
            stop_check=stop_check                                   # 暂停恢复后改走开栏发送逻辑
        )
        if interrupted:
            console("  暂停中断，重试...")
            # 不消耗重试次数，继续循环;但试探机会已用过——恢复后按普通流程(开聊天栏)发送
            used_first_paste = True
            continue
        if not block:
            last_err = "等待响应超时"
            attempt += 1
            continue
        try:
            info = parse_guild_list(block, cfg["rank_names"])
            logger.log_debug(f"解析 /guild list 成功: 公会={info['guild_name']} "
                             f"总数={info['total']} 分组={ {k: len(v) for k, v in info['groups'].items()} }")
            # 公会名来源校验:配置了 guild_name 则强校验;
            # 未配置则告警(日志可被任意玩家伪造面板,强烈建议配置)
            if cfg.get("guild_name"):
                if info["guild_name"] != cfg["guild_name"]:
                    last_err = f"公会名不匹配: 期望 {cfg['guild_name']},实际 {info['guild_name']}"
                    attempt += 1
                    continue
            else:
                logger.log_warning("config.guild_name 为空:无法校验 /guild list 来源,"
                                   "存在伪造面板风险(建议在 config.json 填写公会名)")
            # 一致性自检:解析出的成员总数应接近面板声明的 total(差值≤跳过职位数)。
            # 伪造面板很难同时凑齐总数一致,可拦截大部分假花名册
            if info.get("total") is not None:
                parsed_sum = sum(len(v) for v in info["groups"].values())
                if abs(info["total"] - parsed_sum) > len(cfg.get("skip_ranks", [])):
                    last_err = (f"面板成员数不一致: 声明总数 {info['total']},"
                                f"实际解析 {parsed_sum}(疑似伪造输出)")
                    attempt += 1
                    continue
            return info
        except ParseError as e:
            last_err = str(e)
            attempt += 1
    raise RuntimeError(f"/guild list 解析失败: {last_err}")


def query_member(cfg, watcher, human, hwnd, pid, detector=None, stop_check=None):
    """查询单个成员,返回 MemberInfo dict;重试耗尽返回 None。"""
    cmds = cfg["commands"]
    cmd = cmds["guild_member"].format(player=pid)
    retries = int(cfg["timeouts"].get("max_retries", 3))
    timeout = float(cfg["timeouts"].get("response_wait", 12.0))
    try_wait = float(cfg["timeouts"].get("try_wait", 6.0))
    try_times = int(cfg["timeouts"].get("try_times", 3))
    verbose = cfg["switches"].get("verbose_log", True)
    attempt = 1
    while attempt <= retries:
        wait_if_paused()
        block, interrupted = send_and_capture(
            watcher, human, hwnd, cmd, timeout, verbose=verbose,
            detector=detector, try_wait=try_wait, try_times=try_times,
            first_command=False,  # 从不使用直接粘贴
            stop_check=stop_check
        )
        if interrupted:
            console(f"    [{pid}] 暂停中断，重试...")
            # 不消耗重试次数
            continue
        human.wait_between_commands()  # 命令间随机间隔(模拟人工,防频率检测;重试间同样生效)
        if not block:
            menu_chat = ""
            if detector is not None:
                try:
                    in_menu, chat_open, _ = detector.detect()
                    menu_chat = f" menu={in_menu} chat_open={chat_open}"
                except Exception:
                    pass
            console(f"    [{pid}] 第{attempt}次超时{menu_chat}")
            attempt += 1
            continue
        try:
            info = parse_guild_member(block, expected_id=pid)
            logger.log_debug(f"解析 [{pid}] 成功: 周贡献={info['weekly_total']} "
                             f"daily={len(info.get('daily') or [])} 条")
            return info
        except ParseError as e:
            console(f"    [{pid}] 第{attempt}次解析失败: {e}")
            attempt += 1
    return None


def async_query_member(cfg, watcher, human, hwnd, async_executor,
                       detector, stop_check, pid):
    """纯提交式异步查询(主线程不等验证返回):把 send_and_capture + parse_guild_member
    投到后台 worker,主线程立即返回继续下一条;后台验证完成(成功/失败/重试耗尽)
    写入 result[player] —— 失败会由 _worker 内部重试 + 补做队列自动处理。
    主线程最后用 async_executor.get_result_nowait(player) 一次性取回。
    真正的发送+读回显在后台走 ActionExecutor._send_with_gate 锁内串行。"""
    cmds = cfg["commands"]
    cmd = cmds["guild_member"].format(player=pid)
    timeout = float(cfg["timeouts"].get("response_wait", 12.0))
    try_wait = float(cfg["timeouts"].get("try_wait", 6.0))
    try_times = int(cfg["timeouts"].get("try_times", 3))

    def verify(_cmd, _pid):
        block, _interrupted = send_and_capture(
            watcher, human, hwnd, cmd, timeout,
            detector=detector, try_wait=try_wait, try_times=try_times,
            first_command=False, stop_check=stop_check)
        if _interrupted:
            return False, "暂停中断"
        if not block:
            return False, "等待响应超时"
        try:
            return True, parse_guild_member(block, expected_id=pid)
        except ParseError as e:
            return False, f"解析失败: {e}"

    # 仅提交,不阻塞等待:verify 在后台 _worker 串行执行+重试+补做;
    # 主线程立即继续下一条,最后用 get_result_nowait(pid) 取结果
    async_executor.submit(cmd, pid, verify)
    return True  # 已入队


def async_broadcast(cfg, executor, async_executor, hwnd, detector, stop_check,
                     msg, player, retries, timeout):
    """纯提交式异步广播:仅 submit,主线程不阻塞等验证;后台失败/重试/补做自动处理。
    返回 key(player+seq 唯一),主线程最后用 get_result_nowait(key) 取回结果。"""
    def verify(_cmd, _pid):
        ok, err = executor.gc_broadcast_with_retry(msg, retries=retries, timeout=timeout)
        return ok, err
    # player+seq 组合避免 hash(msg) 碰撞(summry 与 per_player 文本相似时撞)
    pid_key = f"__broadcast__:{player}"
    async_executor.submit(f"/gc broadcast({len(msg)}字->{player})", pid_key, verify)
    return pid_key


def _random_name(prefix="Test_"):
    """生成 Test_ 开头，后接 3~6 个随机字符（含中文）"""
    chars = string.ascii_letters + string.digits + "的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面而方后多定行学法所民得经十三之进着等部度家电力里如水化高自二理起小物现实加量都两体制机当使点从业本去把性好应开它合还因由其些然前外天政四日那社义事平形相全表间样与关各重新线内数正心反你明看原又么利比或但质气第向道命此变条只没结解问意建月公无系军很情者最立代想已通并提直题党程展五果料象员革位入常文总次品式活设及管特件长求老头基资边流路级少图山统接知较将组见计别她手角期根论运农指几九区强放决西被干做必战先回则任取据处队南给色光门即保治北造百规热领七海口东导器压志世金增争济阶油思术极交受联什认六共权收证改清己美再采转更单风切打白教速花带安场身车例真务具万每目至达走积示议声报斗完类八离华名确才科张信马节话米整空元况今集温传土许步群广石记需段研界拉林律叫且究观越织装影算低持音众书布复容儿须际商非验连断深难近矿千周委素技备半办青省列习响约支般史感劳便团往酸历市克何除消构府称太准精值号率族维划选标写存候毛亲快效斯院查江型眼王按格养易置派层片始却专状育厂京识适属圆包火住调满县局照参红细引听该铁价严龙飞"
    name_len = random.randint(3, 6)
    suffix = ''.join(random.choice(chars) for _ in range(name_len))
    return f"{prefix}{suffix}"

def _generate_debug_guild(cfg):
    """生成模拟公会数据，支持 players_per_rank 为整数或对象"""
    sim = cfg.get("debug", {}).get("simulation", {})
    guild_name = sim.get("guild_name", "Debug公会")
    rank_names = cfg.get("rank_names", {})
    ppr = sim.get("players_per_rank", 3)
    
    # 确定每个等级的玩家数量
    if isinstance(ppr, dict):
        per_rank = {rk: ppr.get(rk, 1) for rk in rank_names.keys()}  # 缺失的等级默认 1
    else:
        per_rank = {rk: ppr for rk in rank_names.keys()}
    
    groups = {}
    for rank_key, count in per_rank.items():
        groups[rank_key] = [_random_name() for _ in range(count)]
    return {
        "guild_name": guild_name,
        "groups": groups,
        "total": sum(len(v) for v in groups.values()),
        "online": 0,
    }

def _generate_debug_records(cfg, guild, targets, decide_skipped=False):
    """生成模拟成员记录，周贡献在 contribution_range 内随机"""
    sim = cfg.get("debug", {}).get("simulation", {})
    cr = sim.get("contribution_range", [0, 50000])
    min_c, max_c = cr[0], cr[1]
    records = []
    for rank, pid in targets:
        weekly = random.randint(min_c, max_c)
        record = {
            "player": pid,
            "rank": rank,
            "weekly_total": weekly,
            "joined_at": "2026-01-01 00:00:00",
            "last_online": "2026-08-17 00:00:00",
            "daily": [],
        }
        if decide_skipped:
            # 随机分配动作（可以配置比例，这里简单平均）
            action_types = ["promote", "demote", "kick", None]  # None 表示无操作
            # 但需要符合等级链，简单起见不校验
            chosen = random.choice(action_types)
            if chosen == "promote":
                record["actions"] = ["promote"] * random.randint(1, 3)
            elif chosen == "demote":
                record["actions"] = ["demote"]
            elif chosen == "kick":
                record["actions"] = ["kick"]
            else:
                record["actions"] = []
            record["trigger"] = "debug_skip_decide"
        else:
            record["actions"] = []
            record["trigger"] = None
        records.append(record)
    return records

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def parse_args():
    ap = argparse.ArgumentParser(description="布吉岛公会管理脚本(全自动版)")
    ap.add_argument("--config", default=DEFAULT_CONFIG_FILE, help="配置文件路径(默认 ./config.json)")
    ap.add_argument("--admin", default=None, help="管理员名(覆盖配置)")
    ap.add_argument("--log", default=None, help="游戏日志路径(覆盖配置)")
    ap.add_argument("--options", default=None, help="游戏 options.txt 路径(覆盖配置)")
    ap.add_argument("--dry-run", action="store_true", help="试运行:仅查询+通知+报告,不执行升/降/踢")
    ap.add_argument("--debug", action="store_true", help="启用 debug 模式（覆盖配置）")
    ap.add_argument("--debug-skip", default=None, help="跳过的阶段列表，逗号分隔，如 'list,member'")
    return ap.parse_args()


def main():
    # 初始化 is_paused
    def is_paused():
        # 仅看 _paused(暂停)——_force_stop 由 signal handler raise KeyboardInterrupt 直接
        # 冒泡到 main() 顶层 except 接住进菜单;链路不停(避免后台链路提前 return)
        return _paused

    args = parse_args()

    # 1. 配置
    created = config_mod.ensure_default_config(args.config)
    if created:
        # 首次运行:提醒先查看配置,回车退出;下次启动配置文件存在则不再提醒
        console(f"首次运行:已生成默认配置文件 {os.path.abspath(args.config)}")
        console("请先查看并修改该配置文件——每个配置键前都有 '//' 中文注释,说明作用与取值范围")
        console("重点确认:管理员名(留空自动识别)、日志路径、聊天键、升/降/踢阈值、各开关")
        console("详细说明见同目录《配置说明.md》")
        try:
            input("按回车退出...")
        except EOFError:
            pass
        sys.exit(0)

    cfg = config_mod.load_config(args.config)
    config_mod.apply_cli_overrides(cfg, args)

    # 1.5 初始化日志系统(在校验前,使配置校验错误也能记录到日志)
    log_cfg = cfg.get("log", {})
    logger.init_logger(
        log_dir=log_cfg.get("dir", "logs"),
        level=int(log_cfg.get("level", 4)),
        enabled=bool(log_cfg.get("enabled", True)),
    )
    logger.log_info(f"脚本启动 (日志级别 {logger.current_level()}, "
                    f"{'启用' if logger.is_enabled() else '禁用'})")

    # 合并 debug 配置（命令行覆盖）
    debug_cfg = cfg.get("debug", {})
    if args.debug:
        debug_cfg["enabled"] = True
    if args.debug_skip:
        debug_cfg["skip_stages"] = [s.strip() for s in args.debug_skip.split(",") if s.strip()]
    cfg["debug"] = debug_cfg

    if args.dry_run:
        cfg["switches"]["execution_enabled"] = False
        console("※ dry-run 模式:只查询+通知+输出报告,不执行任何升/降/踢")
    # 管理员名:配置已填用之;否则自动从日志 --username 启动参数识别
    auto_admin = auto_detect_admin(cfg)
    if auto_admin:
        if not cfg.get("admin") and not args.admin:
            console(f"管理员: {auto_admin}(自动识别自游戏日志 --username)")
        cfg["admin"] = auto_admin
    elif not cfg.get("admin"):
        console("⚠ 未识别到管理员名(配置 admin 留空且日志无 --username 行),公告联系人将不完整")

    issues = config_mod.validate_config(cfg)
    errors = [m for lvl, m in issues if lvl == "error"]
    for lvl, m in issues:
        console(f"  [{'错误' if lvl == 'error' else '警告'}] {m}",
                level="error" if lvl == "error" else "warning")
    if errors:
        console("配置校验失败,请修正后重试(参考《配置说明.md》)", level="error")
        sys.exit(1)

    sw = cfg["switches"]
    console("=" * 56)
    console("布吉岛公会管理脚本(全自动版)")
    console(f"  管理员: {cfg.get('admin') or '(未设置,建议 --admin 或配置)'}")
    console(f"  公会群: {cfg.get('guild_qq')}")
    console(f"  开关: 升职{'✓' if sw['promote_enabled'] else '✗'} 降职{'✓' if sw['demote_enabled'] else '✗'} "
            f"踢出{'✓' if sw['kick_enabled'] else '✗'} 执行{'✓' if sw['execution_enabled'] else '✗(dry-run)'} "
            f"通知{'✓' if sw['notify_enabled'] else '✗'}")
    console("=" * 56)

    # 2. 初始化(异常时释放已打开的资源)
    watcher = None
    listener = None
    async_executor = None
    detector = None
    hwnd = None
    gname = ""     # 公会名(第二 try 块赋值;finally 写待办时兜底引用)
    records = []   # 查询记录(第二 try 块赋值;finally 中断兜底补扫引用,提前退出时为空)
    failures = []
    try:
        watcher = LogWatcher(cfg["game_log"], encoding=cfg.get("log_encoding", "auto"), poll_interval_ms=int(cfg["timeouts"].get("poll_interval_ms", 300)))
        watcher.open()
        console(
            f"日志已连接: {cfg['game_log']} (编码 {watcher.encoding})")

        human = HumanInput(cfg, stop_check=is_paused)
        console(f"聊天键: {human.chat_key}(来自 {'options.txt' if cfg['chat_key']=='auto' else '配置'})")

        # 检查聊天键是否为 enter——仅警告不拒绝(用户可能已配置 enter 故意走"试探+回车"路径)
        if human.chat_key == "enter":
            console("⚠ 警告:聊天键设置为 'enter',网易版 enter 是发送键不是开聊天栏键,")
            console("  可能导致 _open_chat 按下后聊天栏未开,后续重试死循环")
            console("  建议:config.json chat_key 改为 't' 或 '/' 之类(读取开聊天栏的键)")
            console("  仍然继续运行——若发生死循环,可按 u 暂停/Ctrl+C 退出")

        # 界面状态检测器(菜单 OCR;不可用自动降级为纯试错式发送)
        detector = None
        sc = cfg.get("state_check", {})
        if sc.get("enabled", True):
            detector = GameStateDetector(sc.get("menu_keywords"), sc.get("chat_bar_keywords"), float(sc.get("cache_seconds", 3.0)), chat_bar_state=sc.get("chat_bar_state", "auto"))
            if not detector.available:
                console("⚠ 界面状态检测不可用(缺少 winsdk/系统OCR),使用试错式发送")
            else:
                console("界面状态检测已启用(菜单界面自动 Esc;发送:首条直接粘贴试探,无响应自动开聊天栏重试,仍失败暂停等待用户)")

        # 1. 只查找窗口，暂不激活
        hwnd = find_game_window(cfg["window_keywords"])
        if hwnd:
            console("游戏窗口已找到(将在倒计时结束后自动置前)")
        else:
            console("⚠ 未找到游戏窗口(请确认游戏已启动、窗口标题包含关键词)")
            try:
                ans = input("按回车仍继续发送命令(可能误操作其他程序),输入 q 回车退出: ").strip().lower()
            except EOFError:
                ans = "q"
            if ans == "q":
                console("已退出")
                sys.exit(0)

        executor = ActionExecutor(cfg, human, watcher, hwnd=hwnd, detector=detector, stop_check=is_paused)
        # 注入暂停恢复标志的消费函数:executor.reset_ui / _send_with_gate 内部据此
        # 在"恢复后首次发送前"执行 Esc+OCR 菜单重置(与 main.reset_window_state_after_pause 同源标志)
        executor.set_reset_hook(_consume_reset_flag)

        # ---- 初始化异步执行器(若启用) ----
        async_cfg = cfg.get("async", {})
        async_executor = None
        if async_cfg.get("enabled", True):
            # 游戏命令经模拟键盘串行发送,多线程会交叉输入 → 强制单 worker
            n_workers = max(1, int(async_cfg.get("num_workers", 1)))
            if n_workers != 1:
                logger.log_warning(f"async.num_workers={n_workers} > 1 会交叉发送键盘输入,已强制为 1")
                n_workers = 1
            async_executor = AsyncCommandExecutor(
                num_workers=n_workers,
                max_queue_size=int(async_cfg.get("max_queue_size", 100)),
                max_retries=int(async_cfg.get("max_retries", 3)),
                retry_delay=float(async_cfg.get("retry_delay", 1.0)),
            )
            async_executor.start()
            logger.log_info(f"异步执行器已启动 (后台逐一执行验证,重试 {async_cfg.get('max_retries', 3)} 次)")
        else:
            logger.log_info("异步执行器未启用,使用串行模式")

        # ---- 以下为新增：待办处理 + 统一提示 + 倒计时 ----
        # 1. 先处理待办（run_pending 内部已有自己的询问，但不会触发这里的提示）
        pending_path = pending_path_of(cfg)
        cont, _remaining = run_pending(cfg, watcher, human, hwnd, executor, pending_path)
        if not cont:
            console("\n再见，本次仅处理了上次未完成的操作")
            return

        # 2. 只有决定继续新的统计时才显示提示和倒计时（仅一次）
        try:
            input("按回车开始运行,3 秒后启动...")
        except EOFError:
            pass
        console("3 秒后开始运行(请将鼠标移开,不要操作键盘)...")
        for _i in (3, 2, 1):
            console(f"  脚本将在{_i}秒后运行，请不要动鼠标/键盘")
            time.sleep(1)

        # 3. 倒计时结束, 此时才激活窗口并启动暂停监听
        if hwnd:
            activate_window(hwnd)
            console("游戏窗口已置前")
        else:
            console("⚠ 未找到游戏窗口, 后续发送命令可能发往其他程序")

        listener = start_pause_listener(cfg.get("pause_key", "u"))
        console(f"暂停键: {_pause_key_name}(随时按暂停/继续)")

        console("开始运行\n")
    except BaseException:  # 含 SystemExit(sys.exit(0) 未找到窗口退出路径),确保资源清理
        if watcher is not None:
            watcher.close()
        if async_executor is not None:
            try:
                async_executor.stop(timeout=3)
            except Exception:
                pass
        if listener is not None:
            listener.stop()
        raise

    try:
        # 3. /guild list
        console("\n[1/5] 查询公会列表 ...")
        logger.log_info("阶段1: 查询公会列表开始")
        debug = cfg.get("debug", {})
        if debug.get("enabled") and "list" in debug.get("skip_stages", []):
            # 仍发送指令，但忽略结果
            try:
                query_guild_list(cfg, watcher, human, hwnd, detector=detector, stop_check=is_paused)
            except Exception as e:
                console(f"Debug: list 发送指令（忽略失败）: {e}")
            guild = _generate_debug_guild(cfg)
            console(f"Debug: 使用模拟公会数据 {guild['guild_name']}")
        else:
            guild = query_guild_list(cfg, watcher, human, hwnd, detector=detector, stop_check=is_paused)
        gname = guild["guild_name"]
        console(f"公会: {gname} | 成员总数 {guild['total']} | 在线 {guild['online']}")
        for rk, ids in guild["groups"].items():
            console(f"  {cfg['rank_names'].get(rk, rk)}: {len(ids)} 人")

        # 4. 查询范围过滤
        scope = cfg.get("query_scope", "all")
        skip = set(cfg.get("skip_ranks", []))
        targets = []  # [(rank, pid)]
        for rk, ids in guild["groups"].items():
            if rk in skip:
                continue
            if scope == "all" or rk == scope:
                targets.extend((rk, pid) for pid in ids)
        logger.log_info(f"阶段2 起点: 逐人查询 targets={len(targets)} 范围={scope} 跳过={sorted(skip)}")
        console(f"[2/5] 逐人查询: 共 {len(targets)} 名玩家(范围 {scope},跳过 {sorted(skip)})")

        # 5. 逐人查询
        failures = []   # 提前初始化，确保所有路径可用
        if debug.get("enabled") and "member" in debug.get("skip_stages", []):
            # 仍发送每个玩家的查询指令，但忽略结果
            for idx, (rk, pid) in enumerate(targets, 1):
                try:
                    query_member(cfg, watcher, human, hwnd, pid, detector=detector, stop_check=is_paused)
                except Exception as e:
                    pass  # 忽略错误
            records = _generate_debug_records(cfg, guild, targets, decide_skipped=("decide" in debug.get("skip_stages", [])))
        else:
            records = []
            # 异步模式:仅向后台提交任务,不阻塞等待验证返回;
            # 失败/重试/补做全部在后台 _worker 内部完成(走 _send_with_gate 锁内串行)
            use_async = async_executor is not None
            for idx, (rk, pid) in enumerate(targets, 1):
                wait_if_paused()
                console(f"[{idx}/{len(targets)}] 查询 {pid} ({cfg['rank_names'].get(rk, rk)})")
                if use_async:
                    async_query_member(cfg, watcher, human, hwnd, async_executor,
                                       detector, is_paused, pid)
                else:
                    info = query_member(cfg, watcher, human, hwnd, pid,
                                       detector=detector, stop_check=is_paused)
                    if info is None:
                        failures.append({"player": pid, "rank": rk, "reason": "查询重试耗尽"})
                        logger.log_error(f"查询失败(重试耗尽): {pid} ({cfg['rank_names'].get(rk, rk)})")
                        continue
                    info["rank"] = rk
                    records.append(info)
                    if sw.get("verbose_log", True):
                        console(f"    周贡献: {info['weekly_total']} | 加入: {info['joined_at']} | 上次在线: {info['last_online']}")
            if use_async:
                # 阶段门控:主线程快速 submit 完所有 player → 等异步线程补完整 records。
                # 用户原话"主线程等异步线程 等完整records"——
                # 阶段 2 循环已经 submit 全部 targets(快速,只入队不等响应);
                # 这里 join 一次等后台串行执行+重试,所有 player 必有最终结果
                # (成功入 _player_results,失败入 retry_queue);然后再次 get_result_nowait
                # 收集全部 records(已就绪 + 刚就绪)。重试耗尽的 player 标记"查询失败"
                # 留到阶段 9 报告,不全填 records(避免判定时基于不完整数据)。
                _join_with_progress(async_executor, "阶段2 查询", expected_n=len(targets))
                ok_n = 0
                for rk, pid in targets:
                    res = async_executor.get_result_nowait(pid)
                    if res is not None and res.get("status") == "ok":
                        info = res.get("data")
                        if isinstance(info, dict):
                            info["rank"] = rk
                            records.append(info)
                            ok_n += 1
                            if sw.get("verbose_log", True):
                                console(f"    周贡献: {info['weekly_total']} | 加入: {info['joined_at']} | 上次在线: {info['last_online']}")
                        continue
                    # join 后仍未 ok:失败(重试耗尽),记入 failures 供报告用,不进 records
                    err = (res or {}).get("error", "重试耗尽")
                    failures.append({"player": pid, "rank": rk, "reason": err})
                    logger.log_error(f"查询失败(重试耗尽): {pid} ({cfg['rank_names'].get(rk, rk)})")
                retry_q = async_executor.get_retry_tasks()
                logger.log_info(f"查询阶段: ok={ok_n} 失败={len(failures)} retry_queue={len(retry_q)} "
                                 f"(主线程已等异步补完整 records)")

        # 6. 判定(继承规则)
        logger.log_info(f"阶段3 起点: 判定 records={len(records)}")
        logger.log_info(f"阶段1 完成: 查询+判定 解析={len(records)} records")
        console(f"[3/5] 判定: {len(records)} 名玩家")
        if not (debug.get("enabled") and "decide" in debug.get("skip_stages", [])):
            for r in records:
                actions, trigger = decide_actions(r["rank"], r["weekly_total"], cfg["thresholds"])
                logger.log_debug(f"判定输入: player={r['player']} rank={r['rank']} "
                                 f"weekly={r['weekly_total']} → 输出 actions={actions} trigger={trigger}")
                r["actions"] = actions
                r["trigger"] = trigger
                if actions:
                    console(f"  {r['player']}: 周贡献 {r['weekly_total']} → {'/'.join(actions)} (阈值 {trigger})")
        else:
            console("Debug: 跳过判定，使用模拟动作")
            # 确保所有记录都有 actions（已在 generate 中设置）
            pass

        # 7. 通知(/gc,查询完成后、执行前)
        # debug skip notify:跳过真实广播(模拟数据不得发进真实公会频道)
        debug_notify_skipped = (debug.get("enabled")
                                and "notify" in debug.get("skip_stages", []))
        if sw["notify_enabled"] and not debug_notify_skipped:
            logger.log_info("阶段4 起点: 发送 /gc 通知")
            logger.log_info(f"阶段3 完成: 判定完成 actionable={sum(1 for r in records if r.get('actions'))}")
            console("[4/5] 发送 /gc 通知 ...")
            logger.log_info(f"准备发送 {len(records)} 条通知(模式: {sw.get('notify_mode', 'summary')})")
            if not records:
                console("  无玩家需要通知，跳过 /gc")
            else:
                n_promote = sum(1 for r in records if r["actions"] and r["actions"][0] == "promote")
                n_demote = sum(1 for r in records if r["actions"] and r["actions"][0] == "demote")
                n_kick = sum(1 for r in records if r["actions"] and r["actions"][0] == "kick")
                mode = sw.get("notify_mode", "summary")
                retries = int(cfg["timeouts"].get("max_retries", 3))
                timeout = float(cfg["timeouts"].get("error_check_window", 4.0))

                if mode == "summary":
                    wait_if_paused()
                    reset_window_state_after_pause(hwnd, detector)
                    tmpl = cfg["notify_templates"].get("summary",
                        "本周公会清人结果:升职 {promote} 人,降职 {demote} 人,踢出 {kick} 人,执行人:{admin},详情见群文件")
                    msg = tmpl.format(promote=n_promote, demote=n_demote, kick=n_kick, admin=cfg.get("admin") or "管理员")
                    if async_executor is not None:
                        # 原始要求"主线程不进行任何验证 快速完成所有指令和操作由异步系统进行验证"
                        # ——不 join:主线程 submit 后立即推报告。失败由后台 retry_queue 兜底,finally 收尾。
                        bc_key = async_broadcast(cfg, executor, async_executor, hwnd, detector,
                                                is_paused, msg, "summary", retries, timeout)
                        res = async_executor.get_result_nowait(bc_key)
                        bc_status = "(后台重试中)" if res is None else (
                            "验证成功" if res.get("status") == "ok" else f"失败: {res.get('error', '结果缺失')}")
                        console(f"  /gc {msg} {bc_status}")
                        if res is not None and res.get("status") != "ok":
                            logger.log_warning(f"/gc 广播失败: {res.get('error', '结果缺失')}")
                    else:
                        ok, err = executor.gc_broadcast_with_retry(msg, retries=retries, timeout=timeout)
                        if ok:
                            console(f"  /gc {msg} (验证成功)")
                        else:
                            console(f"  /gc 广播失败: {err}")
                else:  # per_player —— 继承清单 §3:对每个查询成功成员发送(含无操作玩家)
                    if async_executor is not None:
                        # 纯提交式:仅向后台队列提交任务,主线程不阻塞等验证
                        # 循环提交完后阶段内 join 一次等所有 retries 完成
                        bc_keys = []
                        for r in records:
                            wait_if_paused()
                            reset_window_state_after_pause(hwnd, detector)
                            msg = build_notification(r["player"], r["weekly_total"], r["actions"], cfg)
                            bc_keys.append(async_broadcast(cfg, executor, async_executor, hwnd, detector,
                                                          is_paused, msg, r["player"], retries, timeout))
                            human.wait_between_commands()
                        # 原始要求"主线程不进行任何验证 快速完成所有指令和操作由异步系统进行验证"
                        # ——不 join:submit 后立即推下一阶段(执行)。失败入 retry_queue 兜底,finally 收尾。
                        for r, key in zip(records, bc_keys):
                            res = async_executor.get_result_nowait(key)
                            bc_status = "(后台重试中)" if res is None else (
                                "验证成功" if res.get("status") == "ok" else f"失败: {res.get('error')}")
                            console(f"  /gc {r['player']}: 周贡献 {r['weekly_total']} → /gc 发送 {bc_status}")
                            if res is not None and res.get("status") != "ok":
                                logger.log_warning(f"/gc 广播失败 [{r['player']}]: {res.get('error', '结果缺失')}")
                    else:
                        for r in records:
                            wait_if_paused()
                            reset_window_state_after_pause(hwnd, detector)
                            msg = build_notification(r["player"], r["weekly_total"], r["actions"], cfg)
                            ok, err = executor.gc_broadcast_with_retry(msg, retries=retries, timeout=timeout)
                            if ok:
                                console(f"  /gc {msg} (验证成功)")
                            else:
                                console(f"  /gc 广播失败: {err}")
                                logger.log_warning(f"/gc 广播失败 [{r['player']}]: {err}")
                            human.wait_between_commands()
        else:
            if debug_notify_skipped:
                console("Debug: 跳过通知(notify 在 skip_stages,不向真实频道广播)")
            else:
                console("[4/5] 通知已关闭,跳过 /gc")

        # 8. 执行(先询问是否执行)
        logger.log_info("阶段5 起点: 执行操作")
        logger.log_info("阶段4 完成: /gc 通知完成")
        console("[5/5] 执行操作 ...")
        executed = []
        actionable = [r for r in records if r["actions"]]
        if not actionable:
            # 查询阶段无任何成功(records 为空),无可执行操作,显式提示后直接走报告
            console("  无玩家需要执行(查询阶段无成功结果),跳过执行阶段")
        elif sw["execution_enabled"] and actionable:
            n_p = sum(1 for r in actionable if r["actions"][0] == "promote")
            n_d = sum(1 for r in actionable if r["actions"][0] == "demote")
            n_k = sum(1 for r in actionable if r["actions"][0] == "kick")
            console(f"待执行操作: 升职 {n_p} 人, 降职 {n_d} 人, 踢出 {n_k} 人")
            try:
                ans = input("是否执行以上操作?输入 y 或直接回车=执行,输入 n=跳过(仅输出报告): ").strip().lower()
            except EOFError:
                ans = "n"
            if ans == "n":
                console("已跳过执行(仅输出报告)")
                sw["execution_enabled"] = False
        pending_kicks = [r for r in records if r["actions"] and r["actions"][0] == "kick"]
        if sw["execution_enabled"] and pending_kicks and cfg.get("kick_confirm", True):
            console(f"⚠ 即将踢出 {len(pending_kicks)} 人(不可逆): {[r['player'] for r in pending_kicks]}")
            ans = input("确认执行踢出?输入 kick-confirm 后回车执行,其他输入跳过踢出: ").strip().lower()
            if ans != "kick-confirm":
                console("已跳过全部踢出(其他操作照常执行)")
                for r in pending_kicks:
                    r["executed"] = True  # 标记已处理,防止执行循环再次执行(不可逆操作)
                    r["error"] = "kick 已按确认要求跳过"
                    r["new_rank"] = r["rank"]
                executed.extend(pending_kicks)

        # ---- 执行循环前统一重置（只执行一次） ----
        if sw["execution_enabled"]:
            console("执行前重置界面状态...")
            if hwnd:
                activate_window(hwnd)
            # 强制按 Esc 关闭菜单
            pyautogui.press("esc")
            time.sleep(0.2)
            if detector is not None:
                prepare_chat_state(detector, console)
            time.sleep(0.5)  # 等待界面稳定

        if sw["execution_enabled"]:
            # 即使debug跳过执行，仍发送，但需要确保 actions 存在（模拟数据已有）
            if debug.get("enabled") and "execute" in debug.get("skip_stages", []):
                console("Debug: 跳过执行（但会尝试发送指令，预期失败）")
                # 仍然执行，但注意玩家不存在，execute_action 会返回错误
            # 下面的执行代码不变，因为 execute_action 会验证并返回失败
            for r in records:
                if r.get("executed"):
                    continue
                wait_if_paused()
                # 暂停恢复后重置窗口状态(激活窗口+关菜单+处理聊天栏),
                # 否则 _reset_needed 残留,恢复后直接发命令窗口状态未知。
                # 异步模式跳过:主线程此时动 Esc/Enter 会与持锁发送的 worker 竞争
                #(Esc 取消已粘贴未回车的命令、OCR 误判的 Enter 提前发出半成品),
                # 重置统一交给 _send_with_gate 锁内 reset_ui(同样消费 _reset_needed)
                if async_executor is None:
                    reset_window_state_after_pause(hwnd, detector)
                if not r["actions"]:
                    r["executed"] = False
                    executed.append(r)
                    continue
                kind = r["actions"][0]
                if kind == "promote" and not sw["promote_enabled"]:
                    r["executed"], r["error"] = False, "升职开关已关闭"
                    r["new_rank"] = r["rank"]
                    executed.append(r)
                    continue
                if kind == "demote" and not sw["demote_enabled"]:
                    r["executed"], r["error"] = False, "降职开关已关闭"
                    r["new_rank"] = r["rank"]
                    executed.append(r)
                    continue
                if kind == "kick" and not sw["kick_enabled"]:
                    r["executed"], r["error"] = False, "踢出开关已关闭"
                    r["new_rank"] = r["rank"]
                    executed.append(r)
                    continue
                console(f"  执行 {r['player']}: {r['actions']}")
                logger.trace_code_location("execute_phase.player", f"player={r['player']} actions={r['actions']}")
                if async_executor is not None:
                    # 异步流水线:把每个 action 拆成独立任务提交,主线程不阻塞等回显
                    # AsyncCommandExecutor._worker 内调 verify_fn(cmd, player) —— verify 必须是
                    # 2 参数 (cmd, player) 签名。修复"执行阶段不能执行操作"TypeError:
                    # 之前 _verify 写成 4 参数 (cmd, player, a, r) 但 _worker 只传 2 个位置参数
                    # → 每次执行都 TypeError: _verify() missing 1 required positional argument
                    # 用闭包捕获 a 和 r,_verify 实际只有 (cmd, player) 两个参数。
                    def _make_verify(_a, _r):
                        def _verify(_cmd, _player):
                            # 同步走 _send_with_gate 锁内串行读回显,后台 worker 内执行
                            ok, err = executor.execute_action(_a, _player)
                            return ok, err
                        return _verify
                    action_keys = []
                    # 修复"执行阶段指令拼接错误":player 字段必须是纯玩家名("Test_子治积"),
                    # 不能带 @action 后缀——execute_action 内部 build_command 用 player 拼
                    # /guild promote {player} 等命令,带 @promote 会变成非法命令
                    # /guild promote Test_子治积@promote。但 task_id 仍要带 @action 才能
                    # 区分同一玩家多条 action 的结果(2 条 promote 必须分开追踪)。
                    player = r["player"]
                    for a in r["actions"]:
                        task_id = f"{player}@{a}"  # 唯一 ID(玩家+动作)
                        k = async_executor.submit(a, task_id, _make_verify(a, player))
                        action_keys.append((a, k))
                    r["_async_action_keys"] = action_keys   # 记下供 finally 回填结果
                    r["_async_pending"] = True
                else:
                    # 串行模式:同步执行并等待结果
                    results = executor.execute_actions(r["player"], r["actions"])
                    failed = next(((a, e) for a, ok, e in results if not ok), None)
                    r["executed"] = True
                    r["error"] = f"执行失败({failed[0]}): {failed[1]}" if failed else None
                    # 部分失败:按成功条数计算实际新等级(如 promote×2 第一条成功则升 1 级)
                    ok_count = sum(1 for _, ok, _ in results if ok)
                    r["new_rank"] = new_rank_after(r["rank"], r["actions"][:ok_count]) if ok_count else r["rank"]
                    if failed:
                        console(f"    ⚠ {r['player']} 执行失败: {failed[1]}")
                        logger.log_error(f"执行失败 {r['player']} ({failed[0]}): {failed[1]}")
                executed.append(r)

            # ---- 异步模式:阶段内 join + 收集(确保阶段门控:执行阶段完成才进入下一阶段) ----
            # 用户原话"上一阶段 -> 下一阶段发送指令 -> 下一条指令 -> .... 最后后台验证完成
            #   返回失败/无动作给主线程重试(在该阶段完成前不显示下一阶段入口)"
            # 实现:主线程先把所有 action 快速入队,后台串行执行+重试+补做;
            # 阶段结束前 join 一次拿全部结果(后台重试也在这里完成),
            # 阶段真正完成才推下一阶段。失败/重试耗尽写入 retry_queue,主线程同步取回
            # 给报告+待办使用。
            if async_executor is not None and any(r.get("_async_pending") for r in records):
                pending_rs = [r for r in records if r.pop("_async_pending", None)]
                total_tasks = sum(len(r.get("_async_action_keys", [])) for r in pending_rs)
                logger.log_info(f"(异步)已提交 {len(pending_rs)} 玩家 {total_tasks} 条 action")
                # 原始要求"主线程不进行任何验证 快速完成所有指令和操作由异步系统进行验证"
                # ——不 join:主线程立即推报告(阶段 9)。后台 retry 失败入 retry_queue,finally 收尾。
                # pending 的 record 标"待回填"在报告里。
                for r in pending_rs:
                    r["executed"] = True   # 占位:动作已提交后台,真实结果在 finally 收尾时填
                    action_keys = r.pop("_async_action_keys", [])
                    pending_actions = []
                    ok_actions = []
                    for (action, key) in action_keys:
                        res = async_executor.get_result_nowait(key)
                        if res is None or res.get("status") != "ok":
                            err_txt = (res or {}).get("error") or "后台重试中"
                            pending_actions.append((action, err_txt))
                        else:
                            ok_actions.append(action)
                    # 部分成功按成功条数算 new_rank;pending 留原等级
                    r["error"] = (f"{len(pending_actions)} 条后台重试中,下次启动从待办续做"
                                  if pending_actions else None)
                    ok_count = len(ok_actions)
                    r["new_rank"] = new_rank_after(r["rank"], r["actions"][:ok_count]) if ok_count else r["rank"]
                    if pending_actions:
                        console(f"    ⏳ {r['player']} {r['error']}")
                    elif ok_actions:
                        console(f"    ✓ {r['player']} {len(ok_actions)} 条 action 已提交后台")
        else:
            console("  dry-run:跳过全部执行")
            for r in records:
                if r["actions"]:
                    r["executed"] = False
                    r["error"] = "未执行(dry-run)"
                    r["new_rank"] = new_rank_after(r["rank"], r["actions"])
                executed.append(r)
            executed.extend(dict(f, new_rank=f.get("rank"), actions=[],
                            error=f.get("reason") or "查询失败") for f in failures)

        logger.log_info("阶段5 完成: 执行操作完成")
        logger.log_info("阶段9 起点: 输出报告")
        console("\n输出报告 ...")
        p1 = write_contribution_report(cfg["report_dir"], gname, records, cfg.get("admin", ""))
        action_records = executed + [
            dict(f, new_rank=f.get("rank"), actions=[],error=f.get("reason") or "查询失败")
            for f in failures
            if not any(f["player"] == e.get("player") for e in executed)
            ]
        p2 = write_action_report(cfg["report_dir"], gname, action_records, cfg)
        console(f"  贡献报告: {os.path.abspath(p1)}")
        console(f"  公告文件: {os.path.abspath(p2)}")

        # 10. 未完成操作(跳过/失败)写入待办,供下次启动续做(与上次失败项合并)
        incomplete = []
        for r in records:
            if r.get("actions") and r.get("error"):
                incomplete.append({
                    "player": r["player"], "rank": r["rank"],
                    "actions": r["actions"], "trigger": r.get("trigger"),
                    "weekly": r.get("weekly_total"), "status": "failed",
                    "error": r.get("error"),
                })
        if incomplete:
            merged = load_pending(pending_path) + incomplete
            save_pending(pending_path, gname, merged)
            console(f"⚠ {len(incomplete)} 条操作未完成(跳过/失败),已写入待办文件({pending_path}),下次启动可续做")

        # 总结
        console("\n" + "=" * 56)
        console(f"完成!查询 {len(records)} 人,失败 {len(failures)} 人,"
                f"升职 {sum(1 for r in executed if r.get('actions') and r['actions'][0]=='promote' and r.get('executed') and not r.get('error'))} 人,"
                f"降职 {sum(1 for r in executed if r.get('actions') and r['actions'][0]=='demote' and r.get('executed') and not r.get('error'))} 人,"
                f"踢出 {sum(1 for r in executed if r.get('actions') and r['actions'][0]=='kick' and r.get('executed') and not r.get('error'))} 人")
        console("=" * 56)
        logger.log_info(f"运行结束: 查询 {len(records)} 人, 失败 {len(failures)} 人")
        # 给主线程一个机会查看结果:最多等 5s,超时/回车/非交互式都立即进 finally 收尾
        try:
            import select
            if sys.stdin.isatty():
                ready, _, _ = select.select([sys.stdin], [], [], 5.0)
                if ready:
                    sys.stdin.readline()
        except (EOFError, ValueError, OSError):
            pass
    finally:
        watcher.close()
        # 停止异步执行器(若已启动):失败/中断的任务真正写入 pending_actions.json(不丢失)
        if async_executor is not None:
            try:
                retry_tasks = async_executor.get_retry_tasks()
                failed_players = {t["player"] for t in retry_tasks}
                # ---- ① 重试耗尽的补做任务 → 待办格式(actions 从命令摘要拆回) ----
                incomplete_async = [{
                    "player": t["player"], "rank": "?",
                    "actions": [a for a in (t.get("command") or "").split(",") if a],
                    "trigger": None, "weekly": None, "status": "failed",
                    "error": f"(异步)验证失败: {t.get('error')}",
                } for t in retry_tasks]
                # ---- ② Ctrl+C/异常中断兜底:补扫仍带 _async_pending 的记录 ----
                # (执行循环被中断时回填块未运行,这批任务既未执行也未入待办,防静默丢失)
                interrupted = [r for r in records
                               if r.pop("_async_pending", None) and r.get("actions")]
                incomplete_interrupted = [{
                    "player": r["player"], "rank": r["rank"],
                    "actions": r["actions"], "trigger": r.get("trigger"),
                    "weekly": r.get("weekly_total"), "status": "failed",
                    "error": "(异步)脚本中断,任务未完成,已转待办",
                } for r in interrupted if r["player"] not in failed_players]
                to_save = incomplete_async + incomplete_interrupted
                if to_save:
                    merged = load_pending(pending_path) + to_save
                    # 按 player 去重:后写覆盖先写(避免 step10 与 finally 对同一玩家重复落盘)
                    dedup = {}
                    for it in merged:
                        dedup[it["player"]] = it
                    merged = list(dedup.values())
                    save_pending(pending_path, gname, merged)
                    console(f"⚠ {len(to_save)} 条异步失败/中断任务已写入待办({pending_path}),下次启动可续做")
                    logger.log_warning(f"{len(to_save)} 条异步失败/中断任务转入待办: "
                                       f"{[it['player'] for it in to_save]}")
                async_executor.stop(timeout=5)
            except Exception as e:
                logger.log_error(f"停止异步执行器异常: {e}")
        try:
            listener.stop()
        except Exception:
            pass


def _ctrlc_pause_menu():
    """Ctrl+C 后的交互菜单:回车=退出;输入 c 回车=继续运行(未完成的操作会经待办续做)。
    返回 True 表示用户选择继续。

    修复"Ctrl+C 立即退出":之前用 select.select([sys.stdin]) → OSError [WinError 10038];
    改用 thread+Queue 后,SIGINT 仍会立即传到主线程,主线程在 join(timeout=3.0) 期间被
    signal handler raise KeyboardInterrupt,join 提前返回 → 走 default-exit 分支。
    现在改用 msvcrt.kbhit() 在主线程直接轮询按键(Windows 原生,无需 SIGINT-immune 线程),
    并在调用前后切换 SIGINT 处理:菜单期间恢复默认(让用户再按 Ctrl+C 立即退出),其余时段
    仍是 _signal_handler(继续触发暂停→冒泡到顶层 except KeyboardInterrupt)。"""
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
                    break   # 回车:确认输入
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
    q = _queue.Queue()

    def _read():
        try:
            q.put(input("退出请直接回车,继续请输入 c: ").strip().lower())
        except BaseException:
            # 关键:捕获 KeyboardInterrupt/EOFError 后必须塞个空串,否则 q.empty() 一直 True
            # → 主线程走 "q.empty() → return False" 路径(看起来像"立即退出"但实际是超时)
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


import signal

# Ctrl+C 信号处理:立即把 _paused 置 True(后台 pause 门立即阻断)并设 force_stop 标志,
# 让 wait_for_chat_block / sleep 等检测后快速退出
_force_stop = False
def _signal_handler(sig, frame):
    # 修复"Ctrl+C 不进菜单":signal handler 不仅设标志位,还主动 raise KeyboardInterrupt
    # Windows 上 signal handler 在主线程下次字节码边界会重新抛出此异常,
    # main() 调用栈收到 KeyboardInterrupt 后会冒泡到顶层 except KeyboardInterrupt 接住,
    # 走"进入中断菜单"逻辑(用户原话"按 Ctrl+C 等进入菜单")
    global _paused, _force_stop, _reset_needed
    with _pause_lock:
        _paused = True
    with _reset_lock:
        _reset_needed = True
    _force_stop = True
    logger.log_warning("收到 SIGINT(用户按 Ctrl+C),_paused=True + raise KeyboardInterrupt")
    raise KeyboardInterrupt

if hasattr(signal, 'SIGINT'):
    signal.signal(signal.SIGINT, _signal_handler)
if hasattr(signal, 'SIGTERM'):
    signal.signal(signal.SIGTERM, _signal_handler)

if __name__ == "__main__":
    # 修复"Ctrl+C 静默退出日志未记录":注册全局 excepthook 捕获所有未处理异常
    import sys as _sys_ex
    def _excepthook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            logger.log_warning("Ctrl+C 触发 KeyboardInterrupt(用户退出): %s" % (exc_value or ""))
        else:
            logger.log_exception("未处理异常: %s: %s" % (exc_type.__name__, exc_value), exc_value)
        _sys_ex.__excepthook__(exc_type, exc_value, exc_tb)
    _sys_ex.excepthook = _excepthook
    while True:
        try:
            main()
            break   # 正常走完 main() → 退出
        except KeyboardInterrupt:
            # Ctrl+C:不直接退出,等待用户选择
            logger.log_warning("用户按 Ctrl+C 中断,进入中断菜单(回车退出/c 继续)")
            if _ctrlc_pause_menu():
                # 复位全局暂停状态:中断时可能 _paused=True,不复位会导致
                # 重启后 wait_if_paused / _send_with_gate 立即卡在暂停门。
                # 模块级作用域直接赋值即为修改全局变量,无需 global 声明
                with _pause_lock:
                    _paused = False
                with _reset_lock:
                    _reset_needed = False
                logger.log_info("用户选择继续运行,全局状态已复位,重新初始化启动"
                                "(已完成操作经待办续做防重复)")
                continue   # 重新完整启动:配置/窗口/异步执行器全部重建,
                           # 上次已完成的操作由 pending_actions.json 断点续做机制保证不重复
            console("\n再见")
            logger.log_info("用户手动中断(Ctrl+C),确认退出")
            break
        except Exception as e:
            console(f"\n错误: {e}")
            # 意外关闭(非用户手动):记录致命级日志(级别1也可见)
            logger.log_exception(f"脚本意外关闭(非用户手动): {e}", e)
            sys.exit(1)
