# -*- coding: utf-8 -*-
"""
pending.py —— 待办持久化与断点续做
==================================
从 main.py 拆出。待办机制是"操作没做完就落盘,下次启动接着做",
它的失败模式(丢操作 / 重复执行不可逆的踢出)比主流程其他部分都贵,
所以单独成模块 + 单独测。

落盘位置:<report_dir>/pending_actions.json(随报告目录走)
结构:
    {
      "guild_name": "演示公会",
      "items": [
        {
          "player": "Test_子治积",     # 必过 validate_player_name
          "rank": "low",               # 面板等级键
          "actions": ["kick"],         # 未完成的动作(已完成的前缀已剥离)
          "trigger": "low_kick",       # 判定依据(可空)
          "weekly": 1200,              # 周贡献(可空)
          "status": "pending"|"failed",
          "error": "执行失败(kick): 超时"   # 可空
        }
      ]
    }

为什么玩家名必须过校验:这是本地 JSON,可能被手改或损坏。不过校验就会让任意
字符串拼进 `/guild kick {player}`(命令参数注入),也可能让报告文件名穿越。
非法项**丢弃并告警**,不让一条坏数据毁掉整个续做流程。
"""

import json
import os
import time

from guild_auto.infrastructure import logger
from guild_auto.config import console
from guild_auto.domain.parsers import ParseError, validate_player_name


def pending_path_of(cfg):
    """待办文件路径(随报告目录)。"""
    return os.path.join(cfg.get("report_dir", "reports"), "pending_actions.json")


def load_pending(path):
    """读取待办文件;不存在或损坏返回空列表。

    契约:返回的每一项 player 都已过 validate_player_name,
    调用方可以安全地用它拼命令和文件名。
    """
    logger.trace_call("pending.load_pending", args=("<path>",))
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("items", []) if isinstance(data, dict) else []
        raw = [
            it for it in items if isinstance(it, dict) and it.get("player") and it.get("actions")
        ]
    except FileNotFoundError:
        # 没有待办文件是正常情况(首次运行/上次全部完成),不告警。
        logger.log_debug(f"待办文件不存在 path={path},按无待办处理")
        logger.trace_return("pending.load_pending", "items=0 reason=missing")
        return []
    except (OSError, json.JSONDecodeError) as e:
        # 文件不可读/损坏 = "上次的未完成操作(可能是不可逆的踢人)静默消失",
        # 必须留 WARNING,否则表现为"没有待办"而无人知晓。
        logger.log_warning(
            f"读取待办失败 path={path} 异常={type(e).__name__}: {e}，本次按无待办处理"
        )
        logger.trace_return(
            "pending.load_pending", f"items=0 reason=read_error type={type(e).__name__}"
        )
        return []
    try:
        size = os.path.getsize(path)
    except OSError:
        size = None
    logger.trace_file_read(f"<path>/{os.path.basename(path)}", size=size)

    safe, dropped = [], 0
    for it in raw:
        try:
            it["player"] = validate_player_name(it["player"])
            safe.append(it)
        except ParseError as e:
            dropped += 1
            logger.log_warning(f"待办文件含非法玩家名,已跳过该条: {e}")
    if dropped:
        console(f"⚠ 待办文件有 {dropped} 条玩家名不合法,已跳过(详见日志)")
    logger.trace_return(
        "pending.load_pending", f"items={len(safe)} dropped={dropped} raw={len(raw)}"
    )
    return safe


def save_pending(path, guild_name, items):
    """写入待办文件(已完成项已从 items 中移除,未完成的保留)。"""
    logger.trace_call(
        "pending.save_pending",
        args=("<path>",),
        kwargs={"items": len(items)},
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"guild_name": guild_name or "", "items": items}, f, ensure_ascii=False, indent=2)
    logger.trace_file_write(f"<path>/{os.path.basename(path)}")
    logger.trace_return("pending.save_pending", f"items={len(items)}")


def merge_pending(existing, new_items):
    """合并待办:同一玩家同一动作列表只保留一条(R-10)。

    跨轮重复登记会让续做把同一条指令发两次 —— 踢出不可逆,所以合并必须在
    数据层完成,不能靠每个调用方自觉去重(此前主流程完全不去重、finally 分支
    按玩家去重,两处口径不一致)。后写入的是本轮最新状态,覆盖先前的同键项。
    """
    merged = {}
    for it in list(existing or []) + list(new_items or []):
        merged[(it.get("player"), tuple(it.get("actions") or []))] = it
    return list(merged.values())


def pending_items_from_records(records):
    """从本轮记录生成待办条目 —— R-09 的**写入侧契约**,单一真源。

    规则(顺序即语义):
      1. 被功能开关拦下 / kick 二次确认跳过 / 本次 UI 模式未执行的记录不写
         (否则下次续做会绕过本次的决定);
      2. 没有动作或没有错误的记录不写(已全部成功);
      3. **只写未完成的后缀**:执行层回填的 `remaining_actions` 才是"还没做的",
         没有该键说明整条都没进执行层(dry-run),此时才取全部动作。

    为什么必须集中在这里:此前这段逻辑内联在 main(),跨轮重放已成功的踢/升降
    正是因为没有单一可测的裁剪点(串行/异步各自一份,都可以漏)。
    """
    items = []
    for r in records:
        if r.get("blocked_by_switch") or r.get("skipped_by_confirm") or r.get("skipped_by_mode"):
            continue
        if not (r.get("actions") and r.get("error")):
            continue
        remaining_actions = r.get("remaining_actions")
        if remaining_actions is None:
            remaining_actions = r["actions"]
        if not remaining_actions:
            continue  # 已全部成功 → 不得再入待办
        items.append(
            {
                "player": r["player"],
                "rank": r["rank"],
                "actions": list(remaining_actions),
                "trigger": r.get("trigger"),
                "weekly": r.get("weekly_total"),
                "status": "failed",
                "error": r.get("error"),
            }
        )
    return items


def split_async_tasks(snaps):
    """按 meta.action 把异步任务快照分成 (可续做, 不可续做) —— R-12。

    旧实现拿 `command.split(",")` 当 actions,而 command 是
    `/guild kick <玩家>` 这种整句:续做端认不出任何一项,条目永久卡在待办里。
    `/gc` 广播类任务没有 action,本来就不可续做(广播非幂等),必须单独处置。
    """
    resumable, unresumable = [], []
    for s in snaps:
        action = (s.get("meta") or {}).get("action")
        (resumable if action else unresumable).append(s)
    return resumable, unresumable


def pending_items_from_async_system(system, verify_error="(异步)验证失败"):
    """把异步系统的未完成任务转成待办条目(R-12 + R-13)。

    **先 stop() 再取补做队列**:stop() 才会把"已提交但未验证"的任务标 STOPPED
    并推进补做队列;先取后停,队列恒为空,验证失败/在途任务被静默丢弃
    —— 既不进报告也不进待办,下次启动当然也不会补做。

    返回 (items, unresumable):unresumable 是不可续做的任务数(如 /gc 广播),
    由调用方明确处置并留痕,不得写成"动作=命令文本"的坏条目。
    """
    system.stop(timeout=5)
    resumable, unresumable = split_async_tasks(system.get_retry_queue())
    # 兜底:stop 后理论上没有在途任务了,但拿不到终态的任务绝不能被静默抹掉。
    stranded, stranded_bad = split_async_tasks(system.get_stranded())
    failed_players = {t["player"] for t in resumable}
    items = [
        {
            "player": t["player"],
            "rank": "?",
            "actions": [(t.get("meta") or {})["action"]],
            "trigger": None,
            "weekly": None,
            "status": "failed",
            "error": f"{verify_error}: {t.get('error')}",
        }
        for t in resumable
    ]
    items.extend(
        {
            "player": s["player"],
            "rank": "?",
            "actions": [(s.get("meta") or {})["action"]],
            "trigger": None,
            "weekly": None,
            "status": "failed",
            "error": "(异步)脚本中断,任务未完成,已转待办",
        }
        for s in stranded
        if s["player"] not in failed_players
    )
    return items, len(unresumable) + len(stranded_bad)


def run_pending(
    cfg, watcher, human, hwnd, executor, path, console_fn=None, input_fn=None, ask_text_fn=None
):
    """断点续做:读取上次未完成(或失败)的操作,询问后执行,并更新待办文件。

    返回 (continue_stat, remaining):是否继续新的统计 + 仍未完成的项。
    不进行任何新的统计查询。

    console_fn / input_fn / ask_text_fn 可注入,测试不必真的读终端。
    ask_text_fn 拿**原文**(kick 二次确认的确认词),不能用布尔化的 input_fn ——
    否则"按提示输入 kick-confirm"会被判成否、凭习惯敲 y 反而真踢(与主流程
    stage_confirm_execution 门 2 同一语义,R-11)。
    """
    console_fn = console_fn or console
    input_fn = input_fn or input
    ask_text_fn = ask_text_fn or input
    logger.trace_call("pending.run_pending", args=("<path>",))
    items = load_pending(path)
    if not items:
        logger.trace_return("pending.run_pending", "items=0 reason=no_pending")
        return True, []
    started = time.monotonic()
    logger.log_info(f"待办 ENTER 项数={len(items)} 询问=是否执行(等待=用户输入)")
    sw = cfg["switches"]
    console_fn(f"\n=== 发现上次未完成操作 {len(items)} 条(不进行新的统计查询)===")
    for it in items:
        console_fn(
            f"  {it['player']}: {'/'.join(it['actions'])} | 等级 {it.get('rank', '?')} | "
            f"周贡献 {it.get('weekly', '?')} | 状态 {it.get('status', 'pending')}"
        )
    do_exec = False
    if sw["execution_enabled"]:
        logger.trace_code_location(
            "pending.run_pending.ask",
            "blocking user input",
            items=len(items),
            wait="user(no timeout)",
        )
        try:
            ans = input_fn("是否执行以上操作?输入 y 或直接回车=执行,输入 n=跳过(保留待下次): ")
            ans = (ans or "").strip().lower()
        except EOFError as e:
            logger.log_warning(f"待办确认 EOF(终端关闭/无输入)，按跳过处理: {e!r}")
            ans = "n"
        do_exec = ans != "n"
    else:
        console_fn("(dry-run:不执行,保留待下次)")
    logger.log_info(f"待办执行决定 执行={do_exec}")

    remaining = []
    completed = 0
    if do_exec:
        # 延迟 import:暂停门住在 pause.py,而 pause.py 经 runtime 间接依赖,
        # 模块级 import 会成环
        from guild_auto.application import runtime as _runtime
        from guild_auto.application.pause import reset_window_state_after_pause, wait_if_paused

        # ---- R-11:续做路径的 kick 二次确认 ----
        # 续做同样是"真踢人",不能只靠上面那句 y/回车。此前续做只问一次
        # (y 或回车=执行),等于给不可逆操作留了一道可被顺手确认的口子。
        # 这里复用主流程门 2 的语义:逐字匹配确认词,不匹配就跳过全部 kick;
        # 配置缺确认词时不放行(宁可少踢,不可错踢)。
        sw = cfg.get("switches", {})
        skip_kick = False
        kick_items = [
            it
            for it in items
            if "kick" in (it.get("actions") or []) and sw.get("kick_enabled", True)
        ]
        if kick_items and cfg.get("kick_confirm", True):
            expected = str(cfg.get("kick_confirm_text") or "").strip()
            ans = ""
            logger.log_info(
                f"待办续做踢出二次确认 ENTER 人数={len(kick_items)} 确认词={'缺失' if not expected else '已配置'}"
            )
            if expected:
                try:
                    ans = (
                        ask_text_fn(
                            f"⚠ 续做将踢出 {len(kick_items)} 人(不可逆):"
                            f"{[it['player'] for it in kick_items]}。"
                            f"输入 {expected} 后回车执行,其他输入跳过踢出: "
                        )
                        or ""
                    ).strip()
                except EOFError as e:
                    logger.log_warning(f"待办续做踢出确认 EOF(终端关闭/无输入),按跳过处理: {e!r}")
            if not expected or ans != expected:
                # 配置里缺确认词时不放行(宁可少踢,不可错踢):空确认词等于
                # "任何人按回车都能真踢",是不可逆操作上最危险的一档默认。
                skip_kick = True
                logger.log_warning(
                    f"待办续做的踢出未经二次确认,已跳过 kick: "
                    f"{[it['player'] for it in kick_items]}"
                    f"(原因={'配置缺少 kick_confirm_text' if not expected else '输入不匹配'})"
                )
                console_fn("已跳过续做中的全部踢出(升/降照常;kick 不再补做)")

        for it in items:
            _runtime.set_pause_context(stage="待办续做", command="/".join(it["actions"]))
            wait_if_paused()
            # 暂停恢复后重置窗口状态(与主执行循环一致)
            reset_window_state_after_pause(executor.hwnd, executor.detector)
            # 续做同样受功能开关约束：本次关掉的开关不得因"上次遗留"而绕过。
            sw = cfg.get("switches", {})
            _SW = {"promote": "promote_enabled", "demote": "demote_enabled", "kick": "kick_enabled"}
            allowed = [a for a in it["actions"] if a in _SW and sw.get(_SW[a], True)]
            if not allowed:
                dropped = "".join(
                    {"promote": "升职", "demote": "降职", "kick": "踢出"}[a]
                    for a in it["actions"]
                    if a in _SW
                )
                console_fn(f"    - {it['player']}: {dropped}开关已关闭,跳过(保留待下次)")
                it["error"] = f"{dropped}开关已关闭"
                logger.log_debug(
                    f"续做跳过 player={it['player']} actions={it['actions']} 原因=开关已关闭"
                )
                remaining.append(it)
                continue
            if skip_kick and "kick" in allowed:
                # 摘掉 kick 但保留同条记录里的升/降职(混合动作列表,与主流程门 2 一致)
                allowed = [a for a in allowed if a != "kick"]
                if not allowed:
                    # 用户明确跳过 → 不再补踢(留待办等于下次再问/再踢)
                    console_fn(f"    - {it['player']}: 踢出未获二次确认,已跳过(不再补做)")
                    logger.log_warning(
                        f"续做跳过 player={it['player']} 原因=踢出未获二次确认(不写回待办)"
                    )
                    continue
                logger.log_warning(
                    f"续做跳过踢出,保留其余动作 player={it['player']} actions={allowed}"
                )
            console_fn(f"  执行 {it['player']}: {allowed}")
            item_started = time.monotonic()
            results = executor.execute_actions(it["player"], allowed)
            done = sum(1 for _, ok, _ in results if ok)
            first_err = next((e for _a, ok, e in results if not ok), None)
            logger.log_debug(
                f"续做 player={it['player']} actions={allowed} "
                f"耗时={time.monotonic() - item_started:.2f}s "
                f"ok={done}/{len(results)} err={first_err}"
            )
            if done == len(results):
                completed += 1
                console_fn(f"    ✓ {it['player']} 全部完成,已从待办移除")
                continue  # 完成 → 不写回待办
            it["actions"] = allowed[done:]  # 剩余动作保留,下次继续
            it["status"] = "failed"
            failed = next(((a, e) for a, ok, e in results if not ok), None)
            it["error"] = f"执行失败({failed[0]}): {failed[1]}" if failed else "未执行"
            console_fn(f"    ⚠ {it['player']} 未完成: {it['error']}")
            remaining.append(it)
    else:
        remaining = items  # 用户选择跳过:全部保留

    logger.trace_code_location(
        "pending.run_pending.result",
        "resume cycle",
        items=len(items),
        do_exec=do_exec,
        completed=completed,
        remaining=len(remaining),
    )
    save_pending(path, cfg.get("guild_name") or "", remaining)
    if remaining:
        console_fn(f"仍有 {len(remaining)} 条未完成,已保留在待办文件,下次启动可继续")
    else:
        console_fn("全部待办操作已完成")
    logger.trace_code_location(
        "pending.run_pending.ask",
        "blocking user input",
        items=len(remaining),
        wait="user(no timeout)",
    )
    try:
        ans = input_fn("直接退出(回车)还是继续新的统计操作(输入 c 回车)?: ")
        ans = (ans or "").strip().lower()
    except EOFError as e:
        logger.log_warning(f"待办续做后询问 EOF(终端关闭/无输入)，按直接退出处理: {e!r}")
        ans = ""
    logger.log_info(
        f"待办 EXIT 耗时={time.monotonic() - started:.1f}s 完成={completed} 剩余={len(remaining)}"
    )
    logger.trace_return(
        "pending.run_pending",
        f"continue={ans == 'c'} completed={completed} remaining={len(remaining)}",
    )
    # 语义:cont = "是否继续新的统计操作"。
    # 提示语是「直接退出(回车)还是继续新的统计(输入 c)」—— 所以输入 c 才是继续。
    # 原实现写成 `ans != "c"` 把语义取反了:用户按 c 反而退出、直接回车反而继续,
    # main() 的 `if not cont: return` 因此总是走反。这条路径此前无任何测试。
    return ans == "c", remaining
