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
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("items", []) if isinstance(data, dict) else []
        raw = [it for it in items
               if isinstance(it, dict) and it.get("player") and it.get("actions")]
    except (OSError, json.JSONDecodeError):
        return []

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
    return safe


def save_pending(path, guild_name, items):
    """写入待办文件(已完成项已从 items 中移除,未完成的保留)。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"guild_name": guild_name or "", "items": items}, f,
                  ensure_ascii=False, indent=2)


def run_pending(cfg, watcher, human, hwnd, executor, path, console_fn=None,
                input_fn=None):
    """断点续做:读取上次未完成(或失败)的操作,询问后执行,并更新待办文件。

    返回 (continue_stat, remaining):是否继续新的统计 + 仍未完成的项。
    不进行任何新的统计查询。

    console_fn / input_fn 可注入,测试不必真的读终端。
    """
    console_fn = console_fn or console
    input_fn = input_fn or input
    items = load_pending(path)
    if not items:
        return True, []
    sw = cfg["switches"]
    console_fn(f"\n=== 发现上次未完成操作 {len(items)} 条(不进行新的统计查询)===")
    for it in items:
        console_fn(f"  {it['player']}: {'/'.join(it['actions'])} | 等级 {it.get('rank','?')} | "
                   f"周贡献 {it.get('weekly','?')} | 状态 {it.get('status','pending')}")
    do_exec = False
    if sw["execution_enabled"]:
        try:
            ans = input_fn("是否执行以上操作?输入 y 或直接回车=执行,输入 n=跳过(保留待下次): ")
            ans = (ans or "").strip().lower()
        except EOFError:
            ans = "n"
        do_exec = (ans != "n")
    else:
        console_fn("(dry-run:不执行,保留待下次)")

    remaining = []
    if do_exec:
        # 延迟 import:暂停门住在 pause.py,而 pause.py 经 runtime 间接依赖,
        # 模块级 import 会成环
        from guild_auto.application.pause import reset_window_state_after_pause, wait_if_paused
        for it in items:
            wait_if_paused()
            # 暂停恢复后重置窗口状态(与主执行循环一致)
            reset_window_state_after_pause(executor.hwnd, executor.detector)
            console_fn(f"  执行 {it['player']}: {it['actions']}")
            results = executor.execute_actions(it["player"], it["actions"])
            done = sum(1 for _, ok, _ in results if ok)
            if done == len(results):
                console_fn(f"    ✓ {it['player']} 全部完成,已从待办移除")
                continue  # 完成 → 不写回待办
            it["actions"] = it["actions"][done:]  # 剩余动作保留,下次继续
            it["status"] = "failed"
            failed = next(((a, e) for a, ok, e in results if not ok), None)
            it["error"] = f"执行失败({failed[0]}): {failed[1]}" if failed else "未执行"
            console_fn(f"    ⚠ {it['player']} 未完成: {it['error']}")
            remaining.append(it)
    else:
        remaining = items  # 用户选择跳过:全部保留

    save_pending(path, cfg.get("guild_name") or "", remaining)
    if remaining:
        console_fn(f"仍有 {len(remaining)} 条未完成,已保留在待办文件,下次启动可继续")
    else:
        console_fn("全部待办操作已完成")
    try:
        ans = input_fn("直接退出(回车)还是继续新的统计操作(输入 c 回车)?: ")
        ans = (ans or "").strip().lower()
    except EOFError:
        ans = ""
    # 语义:cont = "是否继续新的统计操作"。
    # 提示语是「直接退出(回车)还是继续新的统计(输入 c)」—— 所以输入 c 才是继续。
    # 原实现写成 `ans != "c"` 把语义取反了:用户按 c 反而退出、直接回车反而继续,
    # main() 的 `if not cont: return` 因此总是走反。这条路径此前无任何测试。
    return ans == "c", remaining
