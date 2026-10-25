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
import itertools
import os
import re
import sys
import time

from pynput import keyboard
import pyautogui

import guild_auto.config as config_mod
from guild_auto.domain.decisions import build_notification, decide_actions, new_rank_after
from guild_auto.infrastructure.action_executor import ActionExecutor
from guild_auto.concurrency.async_executor import AsyncCommandExecutor
from guild_auto.infrastructure import logger
from guild_auto.config import console
from guild_auto.infrastructure.game_state import GameStateDetector, prepare_chat_state
from guild_auto.infrastructure.log_watcher import LogWatcher, detect_encoding
from guild_auto.domain import parsers
from guild_auto.domain.parsers import ParseError, parse_guild_list, parse_guild_member
from guild_auto.domain.reporter import write_action_report, write_contribution_report
from guild_auto.application.stages import stage_confirm_execution, stage_notify, stage_query_async
from guild_auto.infrastructure.window_input import HumanInput, activate_window, find_game_window

DEFAULT_CONFIG_FILE = "config.json"

# 广播 key 自增序号:同一玩家可能被广播多次(summary + per_player),
# 只用 player 做键会让后一条结果覆盖前一条。进程内单调即可,无需持久化。
_BROADCAST_SEQ = itertools.count(1)

# ---------------------------------------------------------------------------
# 从拆分出的模块 re-export(保持历史调用点不变)
# ---------------------------------------------------------------------------
# 暂停机制 → pause.py;待办 → pending.py;模拟数据 → debug_data.py。
# ⚠ 暂停状态(_paused / _reset_needed / _force_stop / _pause_event)**不再**从
# runtime re-export:那样拿到的是值副本,写它只会改 main 自己的副本,
# 而 runtime.is_paused() 读的是 runtime 的变量 → 暂停键静默失效。
# 唯一写入口是 runtime.set_paused / set_force_stop / set_reset_needed。
import guild_auto.application.runtime as runtime_mod  # noqa: E402
from guild_auto.application.debug_data import (  # noqa: E402
    _generate_debug_guild,
    _generate_debug_records,
)
from guild_auto.application.debug_data import random_name as _random_name  # noqa: E402,F401
from guild_auto.application.pause import (  # noqa: E402
    _consume_reset_flag,
    _ctrlc_pause_menu,
    _ctrlc_pause_menu_fallback,
    _signal_handler,
    reset_window_state_after_pause,
    start_pause_listener,
    wait_if_paused,
)
from guild_auto.application.pause import _on_press  # noqa: E402,F401
from guild_auto.application.pending import (  # noqa: E402
    load_pending,
    pending_path_of,
    run_pending,
    save_pending,
)

# ---------------------------------------------------------------------------
# 暂停状态的**兼容别名**(只读,禁止写)
# ---------------------------------------------------------------------------
# 拆分前这些名就住在 main.py,历史调用方与测试按 `main._paused` 直接访问。
# 这里保留同名的**对象引用**而不是值副本:对于 Event / Lock 这类可变对象,
# 别名与 runtime 里的是同一个实例,`with main._pause_lock:` 依然生效;
# 而 `main._paused` / `main._reset_needed` / `main._force_stop` 是 bool,
# 对它们的**赋值不会传播回 runtime** —— 那正是拆分后暂停键失效的根因。
# 所以新增的 helper 供测试安全地设置状态,新代码一律走 runtime.set_*。
_pause_event = runtime_mod._pause_event
_pause_lock = runtime_mod._pause_lock
_reset_lock = runtime_mod._reset_lock
_paused = runtime_mod._paused          # 只读快照;写请用 _set_paused_for_test
_reset_needed = runtime_mod._reset_needed
_force_stop = runtime_mod._force_stop
_pause_key = runtime_mod._pause_key
_pause_key_name = runtime_mod._pause_key_name


def _set_paused_for_test(value):     # noqa: F811
    """测试/兼容用:写唯一所有者。真代码请直接用 runtime.set_paused。"""
    runtime_mod.set_paused(value)
    globals()["_paused"] = bool(value)


def _set_force_stop_for_test(value):  # noqa: F811
    """测试/兼容用:写唯一所有者(见 _set_paused_for_test 的说明)。"""
    runtime_mod.set_force_stop(value)
    globals()["_force_stop"] = bool(value)


def _set_reset_needed_for_test(value):  # noqa: F811
    """测试/兼容用:写唯一所有者(见 _set_paused_for_test 的说明)。"""
    runtime_mod.set_reset_needed(value)
    globals()["_reset_needed"] = bool(value)


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


def _pause_until_resume(cfg=None):
    """置暂停标志并阻塞,等待用户按暂停键恢复(用于发送无响应时人工介入)。

    接受 cfg 作为可选参数:调用方是 send_and_capture,它手上只有
    watcher/human/hwnd/cmd,没有 cfg —— 原本这里裸读 `cfg` 是个未定义名,
    只在"所有重试都失败"这条最不常走的路径上才会炸,所以一直没被发现。
    现在改为:未传 cfg 时跳过 debug 早退判断,只做"置暂停 + 阻塞等恢复"。
    """
    # ⚠ 必须走 runtime.set_paused():写模块级 _paused 副本不会影响
    # runtime.is_paused(),而暂停门读的正是后者 —— 那样"置暂停"会是空操作。
    runtime_mod.set_paused(True)
    console(">>> 脚本已暂停,请检查游戏状态后按暂停键继续 <<<")
    if cfg is not None and cfg.get("debug", {}).get("enabled", False):
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
    # ⚠ 走 runtime.consume_reset_flag() 而不是裸读 _reset_needed:
    # 拆分后回溯 `from runtime import _reset_needed` 拿到的是值副本,
    # 而 pause.py 的 set_reset_needed() 写的是 runtime 那一份 ——
    # 裸读副本永远读到旧的 False,恢复后的界面重置**整段失效**。
    if runtime_mod.consume_reset_flag():
        console("  (暂停恢复，重置界面状态...)")
        pyautogui.press("esc")
        time.sleep(0.1)
        if detector is not None:
            prepare_chat_state(detector, console)

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
        # 修复"Ctrl+C 不暂停":signal handler 设 _force_stop=True 后立即返回。
        # 走 runtime.is_force_stop() 读唯一所有者,不复用已失效的模块级副本。
        if runtime_mod.is_force_stop():
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
    """查询单个成员,返回 MemberInfo dict;重试耗尽返回 None。

    网易屏蔽词玩家(名字被客户端换成整串星号)在**发命令之前**就能判定不可用:
    命令发出去服务端一样拒,重试 3 次结果必然相同。这类直接以真实原因失败,
    不去 burn 重试次数,报告里留着"查询重试耗尽"会让人以为是网络抖动。
    """
    cmds = cfg["commands"]
    if parsers.is_masked_name(pid):
        logger.log_error(f"查询跳过(玩家名被网易屏蔽词替换为星号,身份不可读): {pid}")
        return None
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
        # 屏蔽词玩家:身份不可读,后台验证重发命令结果必然相同 —— 立即失败,
        # 不占用 _worker 的重试配额(留着给真的网络抖动)
        if parsers.is_masked_name(pid):
            return False, "解析失败: 玩家名被网易屏蔽词过滤(星号),无法确认身份,需人工确认"
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
    返回 key(player+seq 唯一),主线程最后用 get_result_nowait(key) 取回结果。

    key 必须含**自增 seq** 而不能只用 player:同一玩家可能被广播多次
    (summary 总广播 + per_player 逐条通知),只用 player 做键会让后一条
    结果覆盖前一条 —— 主线程取回的是错的那条,通知状态遂不可信
    (原测试断言的是自己拼的字面量所以恒真;改成真调本函数后立刻抓到
    两条 key 都是 __broadcast__:P1)。

    seq 用 itertools.count 保证进程内单调;CPython 下 next() 对 int 自增是
    原子的,多线程调用不需额外加锁。
    """
    def verify(_cmd, _pid):
        ok, err = executor.gc_broadcast_with_retry(msg, retries=retries, timeout=timeout)
        return ok, err
    pid_key = f"__broadcast__:{player}:{next(_BROADCAST_SEQ)}"
    async_executor.submit(f"/gc broadcast({len(msg)}字->{player})", pid_key, verify)
    return pid_key


def build_targets(guild_groups, scope="all", skip_ranks=()):
    """从公会名单构建查询目标 [(rank, pid)],**跨等级去重**。

    为什么必须去重:面板异常或被伪造时,同一玩家可能出现在多个等级段。
    不去重会导致查询两遍、判定两遍,执行阶段**重复发送**升职/踢出命令
    —— 踢两次尤其致命。

    去重策略:保留**首次出现**的等级(面板的等级段顺序即游戏展示顺序),
    后续同名条目丢弃。返回 (targets, dropped_count)。

    纯函数,无 I/O、无硬件依赖,便于单测。
    """
    skip = set(skip_ranks or ())
    targets, seen = [], set()
    dropped = 0
    for rk, ids in guild_groups.items():
        if rk in skip:
            continue
        if scope != "all" and rk != scope:
            continue
        for pid in ids:
            if pid in seen:
                dropped += 1
                continue
            seen.add(pid)
            targets.append((rk, pid))
    return targets, dropped


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


def prepare_config(args, console_fn=None, exit_fn=None):
    """流程第一步:检测配置文件 → 加载 → 校验 → 初始化日志。

    对应流程规格(两个模式相同的启动段):
        启动脚本 → 检测同目录下是否有配置文件
           ├─ 无 → 创建配置文件(每项有详细描述) → 提示查看修改
           │       → 提示"回车关闭脚本" → 用户回车 → 脚本关闭
           └─ 有 → 加载 → 校验 → (校验失败则退出)

    返回 cfg;配置校验失败时调用 exit_fn(默认 sys.exit)退出。

    抽成函数的目的:这段含 3 个退出分支(首次创建、校验失败)与
    日志初始化副作用,是流程里**最需要被测试**的一段 ——
    否则"配置文件缺失时会不会创建""校验失败会不会退出"
    只能靠人工点。console_fn / exit_fn 可注入,测试不真的退出。
    """
    console_fn = console_fn or console
    exit_fn = exit_fn or sys.exit

    created = config_mod.ensure_default_config(args.config)
    if created:
        # 首次运行:提醒先查看配置,回车退出;下次启动配置文件存在则不再提醒
        console_fn(f"首次运行:已生成默认配置文件 {os.path.abspath(args.config)}")
        console_fn("请先查看并修改该配置文件——每个配置键前都有 '//' 中文注释,说明作用与取值范围")
        console_fn("重点确认:管理员名(留空自动识别)、日志路径、聊天键、升/降/踢阈值、各开关")
        console_fn("详细说明见同目录《配置说明.md》")
        try:
            input("按回车退出...")
        except EOFError:
            pass
        exit_fn(0)

    cfg = config_mod.load_config(args.config)
    config_mod.apply_cli_overrides(cfg, args)

    # 初始化日志系统(在校验前,使配置校验错误也能记录到日志)
    log_cfg = cfg.get("log", {})
    logger.init_logger(
        log_dir=log_cfg.get("dir", "logs"),
        level=int(log_cfg.get("level", 4)),
        enabled=bool(log_cfg.get("enabled", True)),
    )
    logger.log_info(f"脚本启动 (日志级别 {logger.current_level()}, "
                    f"{'启用' if logger.is_enabled() else '禁用'})")

    # 合并 debug 配置(命令行覆盖)
    debug_cfg = cfg.get("debug", {})
    if args.debug:
        debug_cfg["enabled"] = True
    if args.debug_skip:
        debug_cfg["skip_stages"] = [s.strip() for s in args.debug_skip.split(",") if s.strip()]
    cfg["debug"] = debug_cfg

    if args.dry_run:
        cfg["switches"]["execution_enabled"] = False
        console_fn("※ dry-run 模式:只查询+通知+输出报告,不执行任何升/降/踢")

    # 管理员名:配置已填用之;否则自动从日志 --username 启动参数识别
    auto_admin = auto_detect_admin(cfg)
    if auto_admin:
        if not cfg.get("admin") and not getattr(args, "admin", None):
            console_fn(f"管理员: {auto_admin}(自动识别自游戏日志 --username)")
        cfg["admin"] = auto_admin
    elif not cfg.get("admin"):
        console_fn("⚠ 未识别到管理员名(配置 admin 留空且日志无 --username 行),公告联系人将不完整")

    issues = config_mod.validate_config(cfg)
    errors = [m for lvl, m in issues if lvl == "error"]
    for lvl, m in issues:
        console_fn(f"  [{'错误' if lvl == 'error' else '警告'}] {m}",
                   level="error" if lvl == "error" else "warning")
    if errors:
        console_fn("配置校验失败,请修正后重试(参考《配置说明.md》)", level="error")
        exit_fn(1)

    sw = cfg["switches"]
    console_fn("=" * 56)
    console_fn("布吉岛公会管理脚本(全自动版)")
    console_fn(f"  管理员: {cfg.get('admin') or '(未设置,建议 --admin 或配置)'}")
    console_fn(f"  公会群: {cfg.get('guild_qq')}")
    console_fn(f"  开关: 升职{'✓' if sw['promote_enabled'] else '✗'} 降职{'✓' if sw['demote_enabled'] else '✗'} "
               f"踢出{'✓' if sw['kick_enabled'] else '✗'} 执行{'✓' if sw['execution_enabled'] else '✗(dry-run)'} "
               f"通知{'✓' if sw['notify_enabled'] else '✗'}")
    console_fn("=" * 56)
    return cfg


# count_actions / build_summary_message 已迁至 actions.py(文案生成的唯一家),
# 此处保留 re-export 以不破坏历史调用点。
from guild_auto.domain.decisions import build_summary_message, count_actions  # noqa: E402,F401
def main():
    # 暂停查询钩子：直接委托 runtime 的唯一所有者，不复用模块级副本
    # （副本会读到永远 False 的旧值，是拆分后踩过的静默失效坑）。
    def is_paused():
        return runtime_mod.is_paused()

    args = parse_args()

    # 流程第一步:配置检测/生成/校验/日志初始化(见 prepare_config)
    cfg = prepare_config(args)
    # 功能开关的本地别名。此前 main() 里直接引用 `sw` 却从未定义 —— 查询阶段的
    # 详细输出、执行阶段的三个开关判断、dry-run 分支全都挂在未定义名上,
    # 而这些代码行此前从未被任何测试执行过,所以一直没被发现。
    sw = cfg["switches"]

    # 2. 初始化(异常时释放已打开的资源)
    listener = None
    gname = ""     # 公会名(第二 try 块赋值;finally 写待办时兜底引用)
    records = []   # 查询记录(第二 try 块赋值;finally 中断兜底补扫引用,提前退出时为空)
    failures = []
    try:
        # 真实硬件构造收口在 runtime.build_runtime():watcher / human / executor /
        # detector / hwnd 全部由它建好并注入暂停回调,main() 不再亲手碰硬件。
        rt = runtime_mod.build_runtime(cfg)
        watcher = rt.watcher
        human = rt.human
        executor = rt.executor
        detector = rt.detector
        hwnd = rt.hwnd

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
        # 窗口句柄以本次查找为准（build_runtime 内查过一次，此处可能得到新值）
        executor.hwnd = hwnd

        # ---- 异步(后台)模式开关 ----
        # 默认**串行**(async.enabled 默认 False)。规格:异步系统用于更快地发送指令,
        # 主线程不验证、不读取、不解析,全靠后台系统返回结果。
        # 注意这与旧 AsyncCommandExecutor 的语义**相反**(那是"主线程发一条、
        # 后台验一条、主线程 join 等结果"),故异步分支改用 async_system.AsyncVerifySystem,
        # 不沿用旧的 join 阻塞编排。
        async_cfg = cfg.get("async", {})
        rt.async_enabled = bool(async_cfg.get("enabled", False))
        if rt.async_enabled:
            logger.log_info("异步(后台)模式已启用:主线程只发不验,验证由后台系统承担")
        else:
            logger.log_info("异步模式未启用,使用串行模式(逐条发送、逐条读日志验证)")

        # ---- 以下为新增：待办处理 + 统一提示 + 倒计时 ----
        # 1. 先处理待办（run_pending 内部已有自己的询问，但不会触发这里的提示）
        pending_path = pending_path_of(cfg)
        cont, _remaining = run_pending(cfg, watcher, human, hwnd, executor, pending_path)
        if not cont:
            console("\n再见，本次仅处理了上次未完成的操作")
            return

        # 2. 只有决定继续新的统计时才显示提示和倒计时（仅一次）
        #    流程规格:启动前等用户回车确认;按流程规格,此时必须已提示
        #    "请保证游戏正在运行且在主大厅(防止 AFK)"与"按下回车后不要操作键鼠"
        if not rt.confirm(
                "请保证游戏正在运行且在主大厅(防止 AFK)。\n"
                "按回车键运行;按下回车后请不要操作键盘和鼠标,脚本会自动前置游戏窗口",
                default=True):
            console("已取消,未开始运行")
            return
        console("3 秒后开始运行(请将鼠标移开,不要操作键盘)...")
        console("请不要操作键盘和鼠标,脚本会自动前置游戏窗口")
        for _i in (3, 2, 1):
            console(f"  脚本将在{_i}秒后运行，请不要动鼠标/键盘")
            rt.sleep(1)

        # 3. 倒计时结束, 此时才激活窗口并启动暂停监听
        if hwnd:
            activate_window(hwnd)
            console("游戏窗口已置前")
        else:
            console("⚠ 未找到游戏窗口, 后续发送命令可能发往其他程序")

        listener = start_pause_listener(cfg.get("pause_key", "u"))
        console(f"暂停键: {runtime_mod.pause_key_name()}(随时按暂停/继续)")

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

        # 4. 查询范围过滤 + 跨等级去重(见 build_targets docstring)
        scope = cfg.get("query_scope", "all")
        skip = set(cfg.get("skip_ranks", []))
        targets, dup_dropped = build_targets(guild["groups"], scope=scope,
                                            skip_ranks=skip)
        if dup_dropped:
            logger.log_warning(
                f"阶段2: 丢弃 {dup_dropped} 个跨等级重复玩家(同一名字出现在多个等级段,"
                f"只保留首次出现的等级,避免重复执行升/降/踢)")
            console(f"⚠ 有 {dup_dropped} 个玩家名字重复出现在多个等级,已按首次出现处理")
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
            if rt.async_enabled:
                # 异步(后台)模式:主线程只发不验,验证全部交给后台系统
                # (流程规格 B→D→E,见 stage_query_async docstring)
                console("[2/5] 逐人查询(异步模式:主线程快速发送,后台验证)...")
                records, async_failures = stage_query_async(rt, targets, cfg)
                failures.extend(async_failures)
            else:
                # 默认串行模式:逐条发送 → 逐条读日志验证
                for idx, (rk, pid) in enumerate(targets, 1):
                    wait_if_paused()
                    console(f"[{idx}/{len(targets)}] 查询 {pid} ({cfg['rank_names'].get(rk, rk)})")
                    info = query_member(cfg, watcher, human, hwnd, pid,
                                        detector=detector, stop_check=is_paused)
                    if info is None:
                        # 屏蔽词玩家:身份不可读,报真实原因而非"查询重试耗尽"
                        # 否则报告里看不出是网易过滤还是网络抖动
                        reason = ("查询失败: 玩家名被网易屏蔽词过滤(星号),无法确认身份,需人工确认"
                                  if parsers.is_masked_name(pid) else "查询重试耗尽")
                        failures.append({"player": pid, "rank": rk, "reason": reason})
                        logger.log_error(f"查询失败({reason}): {pid} ({cfg['rank_names'].get(rk, rk)})")
                        continue
                    info["rank"] = rk
                    records.append(info)
                    if sw.get("verbose_log", True):
                        console(f"    周贡献: {info['weekly_total']} | 加入: {info['joined_at']} | 上次在线: {info['last_online']}")

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

        # 7. 通知(/gc,查询完成后、执行前) —— 见 stage_notify
        # Debug 的 notify 跳过开关:debug.enabled 且 skip_stages 含 'notify' 时,
        # 通知阶段照常走流程但不真发广播(与 list/member/execute 的跳过语义一致)。
        # ⚠ 这里此前是个未定义变量名,进通知阶段必抛 NameError —— 全流程第一次
        # 真正跑通前它一直被 15% 的 main.py 覆盖率掩盖着。
        _debug_notify_skipped = bool(debug.get("enabled")
                                     and "notify" in debug.get("skip_stages", []))
        if _debug_notify_skipped:
            console("Debug: 跳过通知发送(仅走流程,不实际广播)")
        notify_failures = stage_notify(rt, records, cfg, _debug_notify_skipped)
        failures.extend(notify_failures)

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
            # 执行前确认(总确认 + kick 二次确认)—— 见 stage_confirm_execution
            execution_enabled, _skipped = stage_confirm_execution(rt, records, cfg)
            if not execution_enabled:
                sw["execution_enabled"] = False
        pending_kicks = [r for r in records if r["actions"] and r["actions"][0] == "kick"]

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
                        # 注意:submit() 的返回值是 True/False(入队是否成功),
                        # **不是**结果 key。拿它当 key 会让 get_result_nowait()
                        # 永远取到 None,于是每条动作都被误报成"后台重试中"。
                        # 真正的 key 是 task_id(AsyncCommandExecutor 用它作 player 键)。
                        if not async_executor.submit(a, task_id, _make_verify(a, player)):
                            logger.log_warning(f"异步队列已满,丢弃: {task_id}")
                        action_keys.append((a, task_id))
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


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
# Ctrl+C 菜单(_ctrlc_pause_menu)与 signal handler 已迁至 pause.py,
# 并在文件头 re-export;signal handler 由 pause.py 在导入时注册。

# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
# Ctrl+C 菜单(_ctrlc_pause_menu / _ctrlc_pause_menu_fallback)与 signal handler
# 已迁至 pause.py(文件头 re-export);signal handler 由 pause.py 在导入时注册。

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
                # ⚠ 唯一写入口是 runtime.reset_state()。不能在这里写 _paused:
                # `from runtime import _paused` 拿到的是值副本,写它改不到 runtime
                # 那一份,而 runtime.is_paused() 读的是自己那份 → 复位静默失效。
                runtime_mod.reset_state()
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
