# -*- coding: utf-8 -*-
"""
stages.py —— 流程阶段编排(双模式)
================================
把 main() 里按"流程规格"划分的阶段抽成独立函数,每个函数:
  * 只接收 Runtime(依赖容器)与业务数据,不自己 import 硬件模块
  · 可被测试单独驱动(假 Runtime)
  * 处理一种阶段职责,读代码即知流程走到哪

阶段与流程规格(plan.txt 第 93-116 行)的对应:
  stage_query_async        异步模式查询(主线程只发不验,验证在后台)
  stage_confirm_execution  执行前确认(总确认 + kick 二次确认)
  stage_notify             通知阶段(/gc 广播,summary 与 per_player 两种)

双模式开关统一走 rt.async_enabled:
  * False = 默认串行:逐条发送 → 逐条读日志验证
  * True  = 异步(后台):主线程只发不验,验证交给 async_system
"""
from guild_auto.infrastructure import logger
from guild_auto.domain.decisions import build_notification, build_summary_message
from guild_auto.config import console
from guild_auto.domain.parsers import ParseError, is_masked_name, parse_guild_member

def stage_query_async(rt, targets, cfg):
    """异步(后台)模式的查询阶段 —— 流程规格 B→D→E 的落地。

    规格语义(plan.txt 第 21-24 行):
        B → 逐条发送指令:脚本【不读日志、不验证】→ 全部发送完毕后等待 → D
        B → 后台系统读日志并验证 → 成功的:忽略并抛弃;失败的:记录结果等待 → E
        E → D:若有失败且需重试则重试(仅发送一次,后台继续验证)→ B(必须防死锁)

    与旧实现的根本区别:旧 async_query_member 把 send_and_capture(含读日志+解析)
    投给 AsyncCommandExecutor 的 worker,主线程再 _join_with_progress 等它 ——
    "发送节奏仍被后台验证速度拖住",没有达到规格要的"发得更快"。

    本函数按规格实现:
      1. send_fn 只负责**把指令发出去**(_send_only,不读日志、不解析);
      2. verify_fn 由后台验证线程调用(读日志 → extract → parse → 判定);
      3. 主线程只调 run_async_round,内部"只发不验 + 后台验证 + 失败重试";

    返回 (records, failures)。records 供阶段3 判定,failures 进报告。
    """
    from guild_auto.concurrency.async_system import AsyncVerifySystem, run_async_round

    cmds = cfg["commands"]
    timeout = float(cfg["timeouts"].get("response_wait", 12.0))
    sw = cfg.get("switches", {})

    # ---- 发送器:主线程侧只"发出去",不读日志、不验证 ----
    watcher = rt.watcher

    def send_only(cmd, player, meta):
        """只发送指令。**不读日志、不解析、不判定** —— 规格的核心要求。"""
        rt.pause_gate()
        if sw.get("verbose_log", True):
            console(f"  → {cmd}")
        rt.human.send_chat_text(cmd, detector=rt.detector, stop_check=rt.is_paused)
        rt.human.wait_between_commands()

    # ---- 验证器:后台线程调用,负责读日志 + 解析 + 判定 ----
    def verify(cmd, player, meta):
        """后台验证一条已发送的查询指令。

        这是"所有指令发送后必须有读取日志并验证操作"(验收第7条)在异步模式
        下的承担者。成功返回 (True, None, info_dict);失败返回 (False, reason, None)。
        """
        block = watcher.wait_for_chat_block(timeout=timeout, stop_check=rt.is_paused)
        if block is None:
            return False, "等待响应超时", None
        try:
            info = parse_guild_member(block, expected_id=player)
        except ParseError as e:
            return False, f"解析失败:{e}", None
        return True, None, info

    system = AsyncVerifySystem(verify_fn=verify, task_timeout=timeout + 5.0)
    system.attach_sender(send_only)

    commands = [(cmds["guild_member"].format(player=pid), pid) for _rk, pid in targets]
    rank_of = {pid: rk for rk, pid in targets}

    logger.log_info(f"(异步)阶段2 查询: {len(commands)} 条指令,主线程只发不验")
    ok_snaps, failed_snaps = run_async_round(
        system, commands, send_only,
        max_retries=int(cfg.get("timeouts", {}).get("max_retries", 3)))
    system.stop()

    records, failures = [], []
    for snap in ok_snaps:
        info = snap.get("data")
        if isinstance(info, dict):
            info["rank"] = rank_of.get(snap["player"], snap["player"])
            records.append(info)
            if sw.get("verbose_log", True):
                console(f"    周贡献: {info['weekly_total']} | 加入: {info['joined_at']} "
                        f"| 上次在线: {info['last_online']}")
    for snap in failed_snaps:
        pid = snap["player"]
        reason = snap.get("error") or "查询失败"
        if is_masked_name(pid):
            reason = "查询失败: 玩家名被网易屏蔽词过滤(星号),无法确认身份,需人工确认"
        failures.append({"player": pid, "rank": rank_of.get(pid, ""), "reason": reason})
        logger.log_error(f"(异步)查询失败({reason}): {pid}")

    logger.log_info(f"(异步)阶段2 完成: 成功 {len(records)} 失败 {len(failures)}")
    return records, failures


def stage_confirm_execution(rt, records, cfg):
    """执行前的两道用户确认 —— 验收第 9 条 + kick 二次确认。

    流程规格:
        执行阶段:执行前需用户确认(kick 需二次确认)

    两道门:
      1) 总确认      —— 列出升/降/踢人数,输入 y 或回车=执行,n=跳过(只出报告)
      2) kick 二次确认 —— 踢出不可逆,需输入 kick-confirm,任何其他输入都跳过全部踢出
                          (升/降照常;被跳过的 kick 标 error,不会被再次执行)

    返回 (execution_enabled, skipped_kicks):
      execution_enabled   False 表示用户拒绝执行(只输出报告)
      skipped_kicks       True 表示 kick 被跳过(总确认可能仍通过)

    纯逻辑:所有输入都走 rt.confirm()(可注入),不直接碰 input(),
    于是"用户拒绝时不得执行任何命令"这条能被测试锁死。
    """
    sw = cfg.get("switches", {})
    actionable = [r for r in records if r.get("actions")]
    execution_enabled = bool(sw.get("execution_enabled", True))

    # 没有可执行项时不做任何确认(不拿无意义的提示打扰用户)
    if not actionable:
        return execution_enabled, False

    n_p = sum(1 for r in actionable if r["actions"][0] == "promote")
    n_d = sum(1 for r in actionable if r["actions"][0] == "demote")
    n_k = sum(1 for r in actionable if r["actions"][0] == "kick")
    console(f"待执行操作: 升职 {n_p} 人, 降职 {n_d} 人, 踢出 {n_k} 人")

    # ---- 门 1:总确认 ----
    ans = rt.ask("是否执行以上操作?输入 y 或直接回车=执行,输入 n=跳过(仅输出报告)",
                 default=True)
    if not ans:
        console("已跳过执行(仅输出报告)")
        execution_enabled = False

    # ---- 门 2:kick 二次确认(踢出不可逆) ----
    pending_kicks = [r for r in records if r["actions"] and r["actions"][0] == "kick"]
    skipped_kicks = False
    if execution_enabled and pending_kicks and cfg.get("kick_confirm", True):
        console(f"⚠ 即将踢出 {len(pending_kicks)} 人(不可逆): "
                f"{[r['player'] for r in pending_kicks]}")
        ans = rt.ask("确认执行踢出?输入 kick-confirm 后回车执行,其他输入跳过踢出",
                     default=False)
        if not ans:
            console("已跳过全部踢出(其他操作照常执行)")
            for r in pending_kicks:
                r["executed"] = True   # 标记已处理,防止执行循环再次执行(不可逆操作)
                r["error"] = "kick 已按确认要求跳过"
                r["new_rank"] = r["rank"]
            skipped_kicks = True

    return execution_enabled, skipped_kicks


def stage_notify(rt, records, cfg, debug_notify_skipped=False):
    """通知阶段:发送 /gc 广播(查询完成后、执行前)。

    流程规格:
        通知阶段:逐条通知、逐条发送、逐条读日志验证

    两种模式(由 rt.async_enabled 决定):
      * 串行(默认)  逐条发送 → 逐条读日志验证(executor.gc_broadcast_with_retry)
      * 异步(后台)  主线程只发不验,验证交后台系统(async_system 的 verify_fn)

    两种通知模式(sw.notify_mode):
      * summary    一条总广播(升/降/踢人数汇总),走 build_summary_message
      * per_player 逐条通知,文案走 build_notification(继承清单 §3,
                    含"无操作"玩家 —— 原脚本对每个查询成功成员都发)

    返回 failures 列表(广播失败的记录),空列表表示全部成功/未开启。
    """
    sw = cfg.get("switches", {})
    retries = int(cfg.get("timeouts", {}).get("max_retries", 3))
    timeout = float(cfg.get("timeouts", {}).get("error_check_window", 4.0))
    failures = []

    if debug_notify_skipped:
        # Debug 模式:模拟数据不得发进真实公会频道
        console("Debug: 跳过通知(notify 在 skip_stages,不向真实频道广播)")
        return failures
    if not sw.get("notify_enabled", True):
        console("[4/5] 通知已关闭,跳过 /gc")
        return failures
    if not records:
        console("  无玩家需要通知，跳过 /gc")
        return failures

    logger.log_info("阶段4 起点: 发送 /gc 通知")
    logger.log_info(f"准备发送 {len(records)} 条通知(模式: {sw.get('notify_mode', 'summary')})")
    console("[4/5] 发送 /gc 通知 ...")

    mode = sw.get("notify_mode", "summary")
    if mode == "summary":
        rt.pause_gate()
        msg = build_summary_message(records, cfg)
        logger.log_info(f"/gc summary: {msg}")
        if rt.async_enabled:
            _notify_one_async(rt, msg, "summary", cfg)
        else:
            ok, err = _notify_one_sync(rt, msg, cfg)
            _report_notify(msg, ok, err)
            if not ok:
                failures.append({"scope": "summary", "error": err})
    else:
        # per_player:逐条通知(含无操作玩家)
        for r in records:
            rt.pause_gate()
            msg = build_notification(r["player"], r["weekly_total"], r["actions"], cfg)
            if rt.async_enabled:
                _notify_one_async(rt, msg, r["player"], cfg)
            else:
                ok, err = _notify_one_sync(rt, msg, cfg)
                _report_notify(f"{r['player']}[周贡献 {r['weekly_total']}]", ok, err)
                if not ok:
                    failures.append({"scope": r["player"], "error": err})
            rt.human.wait_between_commands()

    logger.log_info("阶段4 完成: /gc 通知完成")
    return failures


def _notify_one_sync(rt, msg, cfg):
    """串行模式:发送一条广播并立即读日志验证。返回 (ok, error)。"""
    retries = int(cfg.get("timeouts", {}).get("max_retries", 3))
    timeout = float(cfg.get("timeouts", {}).get("error_check_window", 4.0))
    rt.reset_window_state_after_pause(rt.hwnd, rt.detector)
    return rt.executor.gc_broadcast_with_retry(msg, retries=retries, timeout=timeout)


def _notify_one_async(rt, msg, player, cfg):
    """异步模式:提交广播指令(主线程不读日志、不验证)。

    验证由后台系统承担(后台 verify_fn 读日志判定);主线程提交后立即返回,
    结果由调用方在"全部工作完成后"统一收回(见 run_async_round)。
    """
    rt.async_verify_submit(f"/gc {msg}", player, msg=msg)


def _report_notify(label, ok, err):
    """统一的通知结果输出。"""
    if ok:
        console(f"  /gc {label} (验证成功)")
    else:
        console(f"  /gc 广播失败: {err}")
        logger.log_warning(f"/gc 广播失败 [{label}]: {err}")

