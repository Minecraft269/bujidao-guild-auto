# -*- coding: utf-8 -*-
"""
stages.py —— 流程阶段编排(双模式)
================================
把 main() 里按"流程规格"划分的阶段抽成独立函数,每个函数:
  * 只接收 Runtime(依赖容器)与业务数据,不自己 import 硬件模块
  · 可被测试单独驱动(假 Runtime)
  * 处理一种阶段职责,读代码即知流程走到哪

阶段与流程规格(plan.txt「四、阶段 2:脚本完全重写」之【默认模式】【异步(后台)模式】)的对应:
  stage_query_async        异步模式查询(主线程只发不验,验证在后台)
  stage_confirm_execution  执行前确认(总确认 + kick 二次确认)
  stage_notify             通知阶段(/gc 广播,summary 与 per_player 两种)

双模式开关统一走 rt.async_enabled:
  * False = 默认串行:逐条发送 → 逐条读日志验证
  * True  = 异步(后台):主线程只发不验,验证交给 async_system
"""

import time

from guild_auto.application import runtime as _runtime
from guild_auto.infrastructure import logger
from guild_auto.domain.decisions import build_notification, build_summary_message
from guild_auto.config import console
from guild_auto.domain.parsers import ParseError, is_masked_name, parse_guild_member


def _gate(rt, stage, command):
    """过发送前的暂停门,并记录"哪个阶段/哪条命令在等"(日志据此定位阻塞点)。"""
    _runtime.set_pause_context(stage=stage, command=command)
    rt.pause_gate()


def stage_query_async(rt, targets, cfg):
    """异步(后台)模式的查询阶段 —— 流程规格 B→D→E 的落地。

    规格语义(plan.txt「异步(后台)模式」段):
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

    started = time.monotonic()
    cmds = cfg["commands"]
    timeout = float(cfg["timeouts"].get("response_wait", 12.0))
    sw = cfg.get("switches", {})

    # ---- 发送器:主线程侧只"发出去",不读日志、不验证 ----
    watcher = rt.watcher

    def send_only(cmd, player, meta):
        """只发送指令。**不读日志、不解析、不判定** —— 规格的核心要求。"""
        _gate(rt, "查询发送", cmd)
        if sw.get("verbose_log", True):
            console(f"  → {cmd}")
        sent = rt.human.send_chat_text(cmd, detector=rt.detector, stop_check=rt.is_paused)
        rt.human.wait_between_commands()
        # 只有明确的 False 才算没发出去(成功路径返回 True/None):
        # 被暂停取消的查询命令不该被标记已发送、白烧一次重试
        return sent is not False

    # ---- 验证器:后台线程调用,负责读日志 + 解析 + 判定 ----
    def verify(cmd, player, meta):
        """后台验证一条已发送的查询指令。

        这是"所有指令发送后必须有读取日志并验证操作"(验收第7条)在异步模式
        下的承担者。成功返回 (True, None, info_dict);失败返回 (False, reason, None)。
        """
        logger.log_debug(f"验证 ENTER player={player} 超时={timeout:.1f}s")
        verify_started = time.monotonic()
        block = watcher.wait_for_chat_block(timeout=timeout, stop_check=rt.is_paused)
        elapsed = time.monotonic() - verify_started
        block_text = "None" if block is None else f"{len(block)}行"
        logger.log_debug(f"验证 EXIT 耗时={elapsed:.1f}s block={block_text}")
        if block is None:
            logger.log_warning(f"等待响应超时 player={player} 已等待={elapsed:.1f}s")
            return False, "等待响应超时", None
        try:
            info = parse_guild_member(block, expected_id=player)
        except ParseError as e:
            return False, f"解析失败:{e}", None
        return True, None, info

    system = AsyncVerifySystem(verify_fn=verify, task_timeout=timeout + 5.0)
    system.attach_sender(send_only)

    rank_of = {pid: rk for rk, pid in targets}

    # ---- 屏蔽词玩家:**不发送** /guild member <全星号> ----
    # 网易服务器不会响应这种参数,发出去只会白烧重试周期(历史真 bug:
    # 浪费 3 次重试后仍被丢弃)。身份不可读是事实,直接记为 blocked,
    # 报告单列"屏蔽词玩家(指令不可查)"一节。
    # blocked_name_policy 由配置决定:skip(默认)=不发送;query_attempt=仍尝试。
    blocked_policy = str(cfg.get("blocked_name_policy", "skip")).lower()
    blocked_targets, queryable = [], []
    for rk, pid in targets:
        if is_masked_name(pid) and blocked_policy != "query_attempt":
            blocked_targets.append((rk, pid))
        else:
            queryable.append((rk, pid))
    if blocked_targets:
        logger.log_warning(
            f"(异步)跳过 {len(blocked_targets)} 名屏蔽词玩家:名字为全星号,指令不可查(不消耗重试)"
        )
        console(
            f"  ⚠ {len(blocked_targets)} 名玩家名字被网易屏蔽词替换为星号,"
            f"无法查询贡献(报告中将单列)"
        )

    commands = [(cmds["guild_member"].format(player=pid), pid) for _rk, pid in queryable]

    logger.log_info(f"(异步)阶段2 查询: {len(commands)} 条指令,主线程只发不验")
    ok_snaps, failed_snaps = run_async_round(
        system, commands, send_only, max_retries=int(cfg.get("timeouts", {}).get("max_retries", 3))
    )
    system.stop()

    records, failures = [], []
    for snap in ok_snaps:
        info = snap.get("data")
        if isinstance(info, dict):
            info["rank"] = rank_of.get(snap["player"], snap["player"])
            records.append(info)
            if sw.get("verbose_log", True):
                console(
                    f"    周贡献: {info['weekly_total']} | 加入: {info['joined_at']} "
                    f"| 上次在线: {info['last_online']}"
                )
    for snap in failed_snaps:
        pid = snap["player"]
        reason = snap.get("error") or "查询失败"
        if is_masked_name(pid):
            reason = "查询失败: 玩家名被网易屏蔽词过滤(星号),无法确认身份,需人工确认"
        failures.append({"player": pid, "rank": rank_of.get(pid, ""), "reason": reason})
        logger.log_error(f"(异步)查询失败({reason}): {pid}")

    for rk, pid in blocked_targets:
        failures.append(
            {
                "player": pid,
                "rank": rk,
                "blocked": True,
                "reason": "屏蔽词玩家: 名字被网易过滤为星号,指令不可查,需人工确认",
            }
        )

    logger.log_info(
        f"(异步)阶段2 完成: 成功 {len(records)} 失败 {len(failures)} "
        f"(其中屏蔽词 {len(blocked_targets)}) 耗时={time.monotonic() - started:.1f}s"
    )
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
    # GUI 模态需要结构化信息（阶段名/目标人数/预计动作数），控制台模式忽略这份上下文
    rt.confirm_context = {
        "stage": "执行确认",
        "target": len(actionable),
        "actions": n_p + n_d + n_k,
    }
    ans = rt.ask("是否执行以上操作?输入 y 或直接回车=执行,输入 n=跳过(仅输出报告)", default=True)
    if not ans:
        console("已跳过执行(仅输出报告)")
        execution_enabled = False

    # ---- 门 2:kick 二次确认(踢出不可逆) ----
    pending_kicks = [r for r in records if r["actions"] and r["actions"][0] == "kick"]
    skipped_kicks = False
    if execution_enabled and pending_kicks and cfg.get("kick_confirm", True):
        console(
            f"⚠ 即将踢出 {len(pending_kicks)} 人(不可逆): {[r['player'] for r in pending_kicks]}"
        )
        # 必须逐字匹配确认词:legacy 就是 ans != "kick-confirm" 才跳过。
        # 早先误用布尔化 ask()(只认 y/yes),导致"按提示输入 kick-confirm 反而跳过、
        # 凭习惯敲 y 反而真踢"——语义完全反转,是不可逆操作上的严重回归。
        expected = str(cfg.get("kick_confirm_text") or "").strip()
        # kick 模态要显示玩家名+等级+周贡献，并要求逐字输入确认词（§3.4）
        rt.confirm_context = {
            "stage": "踢出二次确认",
            "players": [
                {
                    "player": r["player"],
                    "level": r.get("rank"),
                    "weekly_total": r.get("weekly_total"),
                }
                for r in pending_kicks
            ],
            "confirm_text": expected,
        }
        ans = rt.ask_text(f"确认执行踢出?输入 {expected} 后回车执行,其他输入跳过踢出: ")
        if ans != expected:
            console("已跳过全部踢出(其他操作照常执行)")
            for r in pending_kicks:
                r["executed"] = True  # 标记已处理,防止执行循环再次执行(不可逆操作)
                r["error"] = "kick 已按确认要求跳过"
                r["skipped_by_confirm"] = True  # 写待办时排除:不得下次补踢
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
    started = time.monotonic()

    if debug_notify_skipped:
        # Debug 模式:模拟数据不得发进真实公会频道
        console("Debug: 跳过通知(notify 在 skip_stages,不向真实频道广播)")
        logger.log_info(
            f"[阶段][EXIT] stage_notify 耗时={time.monotonic() - started:.1f}s 跳过=debug"
        )
        return failures
    if not sw.get("notify_enabled", True):
        console("[4/5] 通知已关闭,跳过 /gc")
        logger.log_info(
            f"[阶段][EXIT] stage_notify 耗时={time.monotonic() - started:.1f}s 跳过=通知已关闭"
        )
        return failures
    if not records:
        console("  无玩家需要通知，跳过 /gc")
        logger.log_info(
            f"[阶段][EXIT] stage_notify 耗时={time.monotonic() - started:.1f}s 跳过=无记录"
        )
        return failures

    logger.log_info("阶段4 起点: 发送 /gc 通知")
    logger.log_info(f"准备发送 {len(records)} 条通知(模式: {sw.get('notify_mode', 'summary')})")
    console("[4/5] 发送 /gc 通知 ...")

    mode = sw.get("notify_mode", "summary")
    if rt.async_enabled:
        # 异步通知:主线程只发,验证交给后台;但**必须在本阶段内收回结果** ——
        # 否则 notify_failures 恒为空,报告里的"通知 成功/失败"是假的,
        # 失败只能等到进程结束进待办(验收 14 要求等验证完成再进下一步)。
        pending = []  # [(task_id, label, 业务 key, 原文)]
        if mode == "summary":
            msg = build_summary_message(records, cfg)
            _gate(rt, "通知发送", msg)
            logger.log_info(f"/gc summary: {msg}")
            pending.append((_notify_one_async(rt, msg, "summary", cfg), "summary", "summary", msg))
        else:
            for r in records:
                msg = build_notification(r["player"], r["weekly_total"], r["actions"], cfg)
                _gate(rt, "通知发送", msg)
                label = f"{r['player']}[周贡献 {r['weekly_total']}]"
                pending.append(
                    (_notify_one_async(rt, msg, r["player"], cfg), label, r["player"], msg)
                )
                rt.human.wait_between_commands()
        failures.extend(_collect_async_notifications(rt, pending, cfg))
    elif mode == "summary":
        msg = build_summary_message(records, cfg)
        _gate(rt, "通知发送", msg)
        logger.log_info(f"/gc summary: {msg}")
        ok, err = _notify_one_sync(rt, msg, cfg)
        _report_notify(msg, ok, err)
        if not ok:
            failures.append({"scope": "summary", "error": err})
    else:
        # per_player:逐条通知(含无操作玩家)
        for r in records:
            msg = build_notification(r["player"], r["weekly_total"], r["actions"], cfg)
            _gate(rt, "通知发送", msg)
            ok, err = _notify_one_sync(rt, msg, cfg)
            _report_notify(f"{r['player']}[周贡献 {r['weekly_total']}]", ok, err)
            if not ok:
                failures.append({"scope": r["player"], "error": err})
            rt.human.wait_between_commands()

    total = 1 if mode == "summary" else len(records)
    logger.log_info(
        f"阶段4 完成: /gc 通知完成 耗时={time.monotonic() - started:.1f}s "
        f"成功={max(0, total - len(failures))} 失败={len(failures)}"
    )
    return failures


def _notify_one_sync(rt, msg, cfg):
    """串行模式:发送一条广播并立即读日志验证。返回 (ok, error)。"""
    retries = int(cfg.get("timeouts", {}).get("max_retries", 3))
    timeout = float(cfg.get("timeouts", {}).get("error_check_window", 4.0))
    rt.reset_window_state_after_pause(rt.hwnd, rt.detector)
    return rt.executor.gc_broadcast_with_retry(msg, retries=retries, timeout=timeout)


def _notify_one_async(rt, msg, player, cfg):
    """异步模式:提交广播指令(主线程不读日志、不验证),返回 task_id。

    结果由 `_collect_async_notifications` 在本阶段内收回 —— 只提交不回收,
    失败会静默漏到进程结束。
    """
    return rt.async_verify_submit(f"/gc {msg}", player, msg=msg)


def _collect_async_notifications(rt, pending, cfg):
    """等后台验证完,失败项按 max_retries 重发,再转成通知失败清单。

    为什么要重试:串行路径调 gc_broadcast_with_retry(retries=max_retries),
    异步路径若只提交一次,同一份配置下两种模式行为就不同(验收 1「双模式行为一致」
    要求只差发送节奏);一次失败直接记失败会让报告低估重试后的成功率。
    """
    if not pending:
        return []
    system = rt.async_system
    timeouts = cfg.get("timeouts", {})
    timeout = float(timeouts.get("response_wait", 12.0)) + 5.0
    max_retries = max(1, int(timeouts.get("max_retries", 3)))
    # 重试间隔的唯一真源是 async.retry_delay(与异步执行同一个键)。
    # 不要用 timeouts.get("retry_delay") —— 那个键在 DEFAULT_CONFIG 里根本不存在,
    # 等于硬编码兜底,违反"配置单一真源"。
    retry_delay = float((cfg.get("async") or {}).get("retry_delay", 1.0))

    alive = list(pending)  # [(task_id, label, 业务 key, 原文)]
    last_error = {}  # label -> None(成功) / 错误文本
    for attempt in range(1, max_retries + 1):
        logger.log_info(
            f"wait_all ENTER timeout={timeout:.1f}s 任务数={len(alive)} "
            f"attempt={attempt}/{max_retries}"
        )
        wait_started = time.monotonic()
        got = system.wait_all(timeout=timeout)
        elapsed = time.monotonic() - wait_started
        logger.log_info(f"wait_all EXIT 耗时={elapsed:.1f}s got={got}")
        ids = [tid for tid, _label, _key, _msg in alive]
        done = {str(s.get("task_id")) for s in system.get_completed(ids)}
        failed = {str(s.get("task_id")): s for s in system.get_failed(ids)}
        retry = []
        inflight = []
        for tid, label, key, msg in alive:
            tid_s = str(tid)
            if tid_s in done:
                last_error[label] = None
                continue
            snap = failed.get(tid_s)
            if snap is None:
                # 既不在成功集、也不在失败集 = wait_all 超时后仍在途。
                # 绝不能记成成功(报告会出现假的"通知成功");也不重发,
                # 重发可能让同一广播发两遍。
                last_error[label] = "等待验证超时(后台未在时限内给出结论)"
                inflight.append(label)
                continue
            last_error[label] = snap.get("error") or "通知验证失败"
            retry.append((label, key, msg))
        if inflight:
            logger.log_warning(
                f"wait_all 超时 在途={len(inflight)} 已等待={elapsed:.1f}s items={inflight}"
            )
        if not retry or attempt >= max_retries:
            break
        reasons = sorted({str(last_error.get(label)) for label, _k, _m in retry})
        logger.log_warning(
            f"(异步)通知重试 attempt={attempt}/{max_retries} "
            f"原因={'|'.join(reasons)} items={[label for label, _k, _m in retry]}"
        )
        time.sleep(retry_delay)
        alive = [
            (_notify_one_async(rt, msg, key, cfg), label, key, msg) for label, key, msg in retry
        ]

    out = []
    for _tid, label, _key, _msg in pending:
        err = last_error.get(label)
        if err is None:
            _report_notify(label, True, None)
            continue
        _report_notify(label, False, err)
        out.append({"scope": label, "error": err})
    return out


def stage_execute_async(rt, records, cfg):
    """异步(后台)模式的执行阶段 —— 与查询阶段同一套 B→D→E 闭环。

    为什么用 run_async_round 而不是旧的 AsyncCommandExecutor 编排:
      旧编排(main.py)自带一套"submit → get_result_nowait 逐条回填 →
      finally 兜底收 retry_queue"的状态机,与 async_system 的语义相反
      (旧:主线程发一条后台验一条再 join;新:主线程只发不验)。
      规格要求两种模式行为一致 —— 只有发送节奏不同 ——
      所以执行阶段必须与查询阶段共用同一状态机,不再并存两套。

    主线程职责:把 actions 逐条转成命令提交(只发不验);后台 verify_fn
    委托 executor.execute_action(其重试/错误模式/面板回显判定在发送锁内,
    与串行路径完全同一套逻辑 —— 不是复制一份判定规则)。

    返回:就地写回每条 record 的 executed / error / new_rank,
    并返回 execution_failures 列表(执行失败项,供报告与待办使用)。
    """
    from guild_auto.concurrency.async_system import AsyncVerifySystem, run_async_round

    started = time.monotonic()
    failures = []
    actionable = [
        r for r in records if r.get("actions") and not r.get("executed")
    ]  # kick 被跳过的已标 executed=True
    if not actionable:
        logger.log_info(
            f"[阶段][EXIT] stage_execute_async 耗时={time.monotonic() - started:.1f}s "
            f"跳过=无可执行项"
        )
        return failures

    sw = cfg.get("switches", {})
    timeout = float(cfg.get("timeouts", {}).get("error_check_window", 4.0))
    retries = int(cfg.get("timeouts", {}).get("max_retries", 3))

    # ---- 功能开关优先于任何判定结果（含自定义规则命中）----
    # 串行与异步共用 apply_action_gates（见文件末尾），避免两模式行为不一致。
    apply_action_gates(records, cfg)

    # ---- 发送器:只发(暂停门在这一层,后台 sender 线程发送前也必须过)----
    def send_only(cmd, player, meta):
        # 与查询/通知、以及串行执行路径一致:暂停期间绝不发送,恢复后先重置窗口状态。
        # 缺这道门时,sender 线程会在暂停中调用 send_chat_text(因 stop_check 直接返回
        # False),而任务已被 mark_sent → 验证必然失败并烧光重试,最后误判成执行失败。
        _gate(rt, "执行发送", cmd)
        if system.is_stopped():
            # 暂停阻塞期间主线程可能已判超时收工:此时再发,就是"报告记执行失败、
            # 游戏里却真发了",下次启动会从待办再发一次 —— 踢人/升职被重复执行。
            logger.log_warning("异步执行发送已取消(本轮已收工),跳过发送:" + str(cmd))
            return False
        sent = rt.human.send_chat_text(cmd, detector=rt.detector, stop_check=rt.is_paused)
        rt.human.wait_between_commands()
        # 只有明确的 False 才算"没发出去"(成功路径返回 None)
        return sent is not False

    # ---- 验证器:后台线程调用,委托 executor 单条执行(含重试/回显判定) ----
    executor = rt.executor

    # 命令文本由配置真源 build_command 生成 —— 与串行路径逐字一致。
    # meta 里带 action 与业务 key:
    #   * action —— verify_fn 据此知道该执行哪个动作(不靠命令文本反查,
    #     因为同一玩家 promote×2 的两条命令**文本完全相同**,反查会张冠李戴);
    #   * key    —— 去重键。重试产生的新 task_id 与首次共享同一 key(应算一条),
    #     而 promote×2 的两条是**两次真实操作**,必须用不同 key。
    from guild_auto.domain.decisions import build_command

    commands = []
    for r in records:
        if not r.get("actions") or r.get("executed"):
            continue
        player = r["player"]
        for idx, a in enumerate(r["actions"]):
            cmd = build_command(cfg, a, player)
            commands.append((cmd, player, {"action": a, "key": (player, "action", idx)}))

    def verify(cmd, player, meta):
        action = (meta or {}).get("action")
        if not action:
            return False, "meta 缺少 action(内部错误)", None
        # 只验证,不再发送:命令已由 send_only 发出,这里再 execute_action
        # 会重复发送(实测 promote×2 升 4 级、踢出重复踢)。
        ok, err = executor.verify_action(action, player)
        return (ok, None if ok else (err or "执行失败"), None)

    system = AsyncVerifySystem(verify_fn=verify, task_timeout=timeout * retries + 10.0)
    system.attach_sender(send_only)

    console(f"[5/5] 执行操作(异步模式:主线程只发不验,{len(commands)} 条命令)...")
    logger.log_info(f"(异步)阶段5 执行: {len(commands)} 条命令(主线程只发不验)")
    ok_snaps, failed_snaps = run_async_round(system, commands, send_only, max_retries=retries)
    console("(异步)执行阶段验证完成,回填结果")
    system.stop()

    # ---- 回填:按 meta.key 精确知道"第几条动作成功",不靠命令文本匹配 ----
    ok_keys = {(s.get("meta") or {}).get("key") for s in ok_snaps}
    err_by_key = {
        (s.get("meta") or {}).get("key"): (s.get("error") or "执行失败") for s in failed_snaps
    }
    from guild_auto.domain.decisions import new_rank_after

    failures = []
    for r in records:
        if not r.get("actions") or r.get("executed"):
            continue
        player = r["player"]
        per_ok, per_err = [], []
        for idx, a in enumerate(r["actions"]):
            k = (player, "action", idx)
            if k in ok_keys:
                per_ok.append(a)
            else:
                per_err.append((a, err_by_key.get(k, "执行失败")))
        ok_count = len(per_ok)
        r["executed"] = True
        r["error"] = f"执行失败({per_err[0][0]}): {per_err[0][1]}" if per_err else None
        # 部分失败:按成功条数算 new_rank(与串行路径同一规则)
        r["new_rank"] = (
            new_rank_after(r["rank"], r["actions"][:ok_count]) if ok_count else r["rank"]
        )
        if per_err:
            console(f"    ⚠ {player} 执行失败({per_err[0][0]}): {per_err[0][1]}")
        else:
            console(f"    ✓ {player} 全部完成")
        if per_err:
            failures.append(
                {"player": player, "rank": r["rank"], "actions": r["actions"], "error": r["error"]}
            )
    logger.log_info(
        f"[阶段][EXIT] stage_execute_async 耗时={time.monotonic() - started:.1f}s "
        f"命令={len(commands)} 失败={len(failures)}"
    )
    return failures


def _report_notify(label, ok, err):
    """统一的通知结果输出。"""
    if ok:
        console(f"  /gc {label} (验证成功)")
    else:
        console(f"  /gc 广播失败: {err}")
        logger.log_warning(f"/gc 广播失败 [{label}]: {err}")


# 只有这三类动作会被发往游戏；notify 由通知阶段处理，ignore 无动作。
EXECUTABLE_ACTIONS = ("promote", "demote", "kick")
_SWITCH_OF = {"promote": "promote_enabled", "demote": "demote_enabled", "kick": "kick_enabled"}


def apply_action_gates(records, cfg):
    """统一的"动作门禁"：功能开关 + 非执行类动作过滤。

    串行与异步**共用本函数**，在通知/确认之前调用一次。返回被拦下的记录数。
    为什么必须逐动作过滤：执行器会执行 actions 列表里的**全部**动作，
    只按 actions[0] 判开关会让 ["demote","kick"] 在 kick 关闭时仍踢人。
    被拦下的记录标注 blocked_by_switch=True（写入待办时据此排除），
    避免"这次没踢、下次续做时补踢"。
    """
    sw = cfg.get("switches", {})
    blocked = 0
    for r in records:
        actions = r.get("actions") or []
        if not actions:
            continue
        kept, off = [], []
        for a in actions:
            if a not in EXECUTABLE_ACTIONS:
                continue  # notify/ignore 不进执行层
            key = _SWITCH_OF.get(a)
            if key and not sw.get(key, True):
                off.append(a)
            else:
                kept.append(a)
        if off:
            blocked += 1
            r["blocked_by_switch"] = True
            r["executed"] = True  # 已处理完毕,不得再进入待办
            names = "".join({"promote": "升职", "demote": "降职", "kick": "踢出"}[a] for a in off)
            r["error"] = f"{names}开关已关闭"
            r["new_rank"] = r["rank"]
        r["actions"] = kept
    return blocked
