# -*- coding: utf-8 -*-
"""
reporter.py —— 报告输出
========================
职责:输出三份报告(前两份见 docs/业务规则继承清单.md §5 与用户提供的参考文件;
第三份见 docs/详细报告规范.md):
  * <日期> <公会名>公会 所有玩家详细贡献.txt
      格式:---高活跃--- / ---中等活跃--- / ---低活跃--- / ---入门成员---
            玩家:<ID> 周贡献为:<数值> 执行操作:<操作>
  * <公会名>_<日期>.txt(群公告)
      格式:被升职（N人）/ 被踢出（N人）/ 被降职（N人)+ ID 空格分隔 + 联系人
  * <日期> <公会名>公会 运行分析报告.txt(第三份,只增不改)
      七节:元信息 / 阶段耗时 / 逐人决策 / 失败跳过 / 统计汇总 /
            配置快照 / UI识别记录(仅 ui 模式)/ 全员名册对账
输出目录:配置 report_dir(相对脚本运行目录,自动创建)。
"""

import os
import re
from datetime import date, datetime

from guild_auto.domain.parsers import is_masked_name
from guild_auto.domain.report_stats import (
    action_counts,
    contribution_stats,
    percentiles,
    rank_transition_matrix,
)
from guild_auto.infrastructure import logger

# 文件名安全化:公会名来自日志解析,替换路径分隔符与 Windows 非法字符,
# 防止 "..\..\evil" 之类名字把报告写到 report_dir 之外
_UNSAFE_FNAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')
# 报告文件名里的公会名上限。公会名来自日志解析(不可信),不限长会让
# open() 抛 OSError [Errno 22] —— 报告是流程最后一步,崩在这里
# 会让用户丢掉全部查询结果(独立审查指出,实测 560 字符名即崩)。
# 60 字符远大于任何真实公会名,又能把完整路径压在系统上限内。
_MAX_GUILD_NAME_LEN = 60

# 报告相关日志的截断/去重:坏时间字段只记一次,避免每行刷一条;
# 原始值片段截断,防止脏数据把日志撑爆。
_SNIP_MAX = 80
_REPORT_WARNED = set()


def _snip(value, limit=_SNIP_MAX):
    """值 → 单行短片段(超长截断),用于日志里带原始值。"""
    if value is None:
        return "None"
    s = value if isinstance(value, str) else repr(value)
    s = s.replace("\r", " ").replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + f"...(+{len(s) - limit}字符)"


def _warn_once(key, msg):
    """同一来源只记一条 WARNING(报告大量行共用同一个格式化函数,避免刷屏)。"""
    if key in _REPORT_WARNED:
        return
    _REPORT_WARNED.add(key)
    logger.log_warning(msg)


def _report_path_for_log(path):
    """报告路径的脱敏形式:相对路径,绝不写本机绝对路径(§四 隐私)。"""
    try:
        return os.path.relpath(path)
    except ValueError:  # Windows 跨盘符无法求相对路径
        return os.path.basename(path)


def _action_counts_text(records):
    """报告条数统计片段:升/降/踢/失败各几人(第 4 级日志可见)。"""
    c = action_counts(records)
    failed = sum(1 for r in records or [] if r.get("error"))
    return (
        f"record_count={len(records or [])} promote={c['promote']} demote={c['demote']} "
        f"kick={c['kick']} failed={failed}"
    )


def _safe_guild_name(guild_name):
    """把公会名净化成可安全用于文件名的片段。

    三重处理:替换路径/Windows 非法字符 -> 去掉首尾点与空格
    -> 截断到 _MAX_GUILD_NAME_LEN。截断用省略号收尾,避免两个
    不同长名被截成同一个文件名而互相覆盖。
    """
    cleaned = _UNSAFE_FNAME_RE.sub("_", str(guild_name or "")).strip(". ")
    if len(cleaned) > _MAX_GUILD_NAME_LEN:
        cleaned = cleaned[:_MAX_GUILD_NAME_LEN].rstrip() + "…"
    return cleaned or "unknown"


# 贡献报告的分组标题(与原脚本输出一致:高/中/低不带"成员",入门带)
_RANK_SECTION = {
    "high": "高活跃",
    "mid": "中等活跃",
    "low": "低活跃",
    "entry": "入门成员",
}
_RANK_ORDER = ["high", "mid", "low", "entry"]


def today_str():
    return date.today().strftime("%Y-%m-%d")


def _ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def action_display(actions):
    """操作显示文本(与原脚本一致):升职(N次)/降职/踢出/无。"""
    if not actions:
        return "无"
    kind = actions[0]
    if kind == "promote":
        return f"升职({len(actions)}次)"
    if kind == "demote":
        return "降职"
    if kind == "kick":
        return "踢出"
    return "未知"


def write_contribution_report(report_dir, guild_name, records, admin=""):
    """全员贡献报告(原脚本风格)。
    records: [{player, rank, weekly_total, actions, ...}]
    命名: <日期> <公会名>公会 所有玩家详细贡献.txt
    格式: ---高活跃--- 分组,玩家:ID 周贡献为:N 执行操作:操作"""
    _ensure_dir(report_dir)
    fname = f"{today_str()} {_safe_guild_name(guild_name)}公会 所有玩家详细贡献.txt"
    fpath = os.path.join(report_dir, fname)

    grouped = {rk: [] for rk in _RANK_ORDER}
    for r in records:
        grouped.setdefault(r.get("rank"), []).append(r)

    lines = [
        f"=== {guild_name}公会 所有玩家详细贡献(统计日 {today_str()}) ===",
        f"执行人: {admin or '(未填写)'}",
        "",
    ]
    total_weekly = sum(r.get("weekly_total", 0) for r in records)
    lines.append(f"统计玩家数: {len(records)}   周贡献合计: {total_weekly}")
    lines.append("")
    for rk in _RANK_ORDER:
        items = grouped.get(rk) or []
        if not items:
            continue
        lines.append(f"---{_RANK_SECTION[rk]}---")
        for r in items:
            lines.append(
                f"玩家:{r.get('player', '')} 周贡献为:{r.get('weekly_total', 0)} "
                f"执行操作:{action_display(r.get('actions'))}"
            )
        lines.append("")

    with open(fpath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    logger.trace_file_write(fpath)
    logger.log_info(
        f"[报告] 全员贡献报告已写入 | report_kind=contribution "
        f"| path={_report_path_for_log(fpath)} | {_action_counts_text(records)}"
    )
    return fpath


def write_action_report(report_dir, guild_name, action_records, cfg):
    """群公告(原脚本风格,参考 演示公会_<日期>.txt)。
    action_records: [{player, rank, new_rank, weekly_total, actions, trigger, executed, error}]
    命名: <公会名>_<日期>.txt
    格式: 被升职（N人） / 被踢出（N人） / 被降职（N人),每组 ID 空格分隔 + 联系人;
    存在失败/未执行记录时,尾部附加【未执行】区。"""
    _ensure_dir(report_dir)
    fname = f"{_safe_guild_name(guild_name)}_{today_str()}.txt"
    fpath = os.path.join(report_dir, fname)

    def pick(kind):
        return [r for r in action_records if r.get("actions") and r["actions"][0] == kind]

    promoted = pick("promote")
    kicked = pick("kick")
    demoted = pick("demote")
    failed = [r for r in action_records if r.get("error")]

    contact = cfg.get("contact_text", "").format(admin=cfg.get("admin") or "管理员")

    lines = []
    for title, items in (("被升职", promoted), ("被踢出", kicked), ("被降职", demoted)):
        lines.append(f"{title}（{len(items)}人）")
        ids = [r.get("player", "") for r in items]
        if ids:
            lines.append(" ".join(ids))
        lines.append(contact)
        lines.append("")

    if failed:
        lines.append(f"【未执行】({len(failed)}人)")
        for r in failed:
            lines.append(f"  {r.get('player')} : {r.get('error') or '未执行(dry-run)'}")
        lines.append("")

    with open(fpath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    logger.trace_file_write(fpath)
    logger.log_info(
        f"[报告] 群公告报告已写入 | report_kind=action "
        f"| path={_report_path_for_log(fpath)} | {_action_counts_text(action_records)}"
    )
    return fpath


# ---------------------------------------------------------------------------
# 第三份报告:运行分析报告(docs/详细报告规范.md)
# ---------------------------------------------------------------------------
# 等级展示顺序:职位在前,四个活跃等级按高→低,与 /guild list 面板顺序一致
# (操作者拿报告和面板逐行对照时才不会来回找)。
_ANALYSIS_RANK_ORDER = ["leader", "vice_leader", "admin", "high", "mid", "low", "entry"]

# 阈值边界语义,仅用于报告展示。判定真源仍是 decisions.decide_actions 的 if 链;
# 这里只把同一条规则的边界写进报告,方便操作者核对"为什么是这个结论"。
_THRESHOLD_OPS = {
    "high_demote": "<=",
    "mid_promote": ">=",
    "mid_demote": "<",
    "low_promote_2": ">=",
    "low_promote_1": ">=",
    "low_demote": "<",
    "entry_promote_3": ">=",
    "entry_promote_2": ">=",
    "entry_promote_1": ">=",
    "entry_kick": "<",
}
_THRESHOLD_KEYS = list(_THRESHOLD_OPS)

# 日志级别名(1~6 与 DEFAULT_CONFIG["log"]["level"] 的说明一一对应)
_LOG_LEVEL_NAMES = {1: "致命", 2: "严重错误", 3: "错误", 4: "信息", 5: "调试", 6: "追踪"}

# 名册状态 → 显示文本。第 7 节状态列与"其中"汇总共用,避免两处措辞漂移。
_STATUS_TEXT = {
    "processed": "已处理",
    "skip_rank": "跳过职位",
    "blacklist": "黑名单跳过",
    "not_in_whitelist": "白名单外跳过",
    "protected": "保护名单(不踢)",
    "blocked": "屏蔽词不可查",
    "query_failed": "查询失败",
    "unprocessed": "未处理",
}
_STATUS_LEDGER = [
    ("processed", "已处理"),
    ("skip_rank", "跳过职位"),
    ("blacklist", "黑名单"),
    ("not_in_whitelist", "白名单外"),
    ("blocked", "屏蔽词"),
    ("query_failed", "查询失败"),
    ("protected", "保护名单(未踢)"),
]
_PLACEHOLDER = "——"

# 成员总数来源 → 报告文案。由运行侧通过 counts.member_total_source 传入,
# 报告不再按操作模式猜测来源(那会把"逐页累计"误写成"公会详情 tooltip")。
_SOURCE_TEXT = {
    "guild_list_panel": "指令模式=/guild list 面板末行",
    "guild_detail_tooltip": "UI模式=公会详情 tooltip「人数: [N/M]」",
    "ui_paging_accumulated": "UI模式=逐页翻页累计(未读到公会详情 tooltip)",
}


def _num_text(value):
    """数值字段的展示文本:缺失用占位符,而不是误导性的 0。"""
    if value is None:
        return _PLACEHOLDER
    return str(value)


def _fmt_seconds(seconds):
    """秒 → 报告时间文本(0m04s / 53m39s / 1h02m03s);非数值给占位符。"""
    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError) as e:
        # 报告里出现「——」时,要能区分"字段缺失"与"格式坏了" —— 必须留痕。
        _warn_once(
            "seconds",
            f"[报告] 秒数字段无法格式化,使用占位符 | field=seconds "
            f"| raw_value={_snip(seconds)} | fallback={_PLACEHOLDER!r} | error={e!r}",
        )
        return _PLACEHOLDER
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    return f"{minutes}m{secs:02d}s"


def _parse_ts(value):
    """把 run_meta 的时间戳解析成 datetime;支持 datetime 对象与常见文本格式。"""
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    text = str(value).strip()
    last_err = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError as e:
            last_err = e
            continue
    _warn_once(
        "timestamp",
        f"[报告] 时间戳无法解析,使用占位符 | field=timestamp "
        f"| raw_value={_snip(value)} | fallback=None | error={last_err!r}",
    )
    return None


def _ts_text(value):
    parsed = _parse_ts(value)
    if parsed is not None:
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    return str(value) if value else _PLACEHOLDER


def _window_seconds(started, ended):
    """运行窗口总秒数;任一端缺失/无法解析时返回 None(报告给占位符)。"""
    begin, finish = _parse_ts(started), _parse_ts(ended)
    if begin is None or finish is None:
        return None
    return max(0.0, (finish - begin).total_seconds())


def _rank_text(rank, rank_names):
    """等级显示名:一律取自配置 rank_names;已踢出/不变是报告专用列名。"""
    if rank == "kicked":
        return "已踢出"
    if rank == "unchanged":
        return "不变"
    if rank is None:
        return _PLACEHOLDER
    return (rank_names or {}).get(rank, str(rank))


def _action_text(actions):
    """动作列表 → 报告用短文本:升职 / 升职×2 / 降职 / 踢出 / 无操作。"""
    acts = actions or []
    if not acts:
        return "无操作"
    kind = acts[0]
    if kind == "promote":
        return "升职" if len(acts) == 1 else f"升职×{len(acts)}"
    if kind == "demote":
        return "降职"
    if kind == "kick":
        return "踢出"
    return str(kind)


def _decision_text(record, cfg):
    """第 2 节「判定」行:必须给出 (阈值/规则 + 输入值 + 结论) 三元组。

    名单层拦截与阈值命中用不同措辞 —— 否则操作者分不清"没查"和"查了但不动"。
    """
    weekly = record.get("weekly_total")
    actions = record.get("actions") or []
    trigger = record.get("trigger")
    conclusion = _action_text(actions)
    if trigger == "blacklist":
        return f"黑名单拦截 → {conclusion}"
    if trigger == "not_in_whitelist":
        return f"白名单外拦截 → {conclusion}"
    if trigger == "whitelist_protected":
        return f"保护名单(免降职与踢出) → {conclusion}"
    if isinstance(trigger, str) and trigger.startswith("rule:"):
        return f"规则 {trigger[len('rule:') :]} 命中 → {conclusion}"
    if trigger in _THRESHOLD_OPS:
        limit = (cfg.get("thresholds") or {}).get(trigger)
        return f"阈值 {trigger}({_THRESHOLD_OPS[trigger]}{limit}) 输入 {weekly} → {conclusion}"
    if trigger is None and not actions:
        return f"阈值矩阵未命中(输入 {weekly}) → 无操作"
    return f"无阈值记录(trigger={trigger}, 输入 {weekly}) → {conclusion}"


def _execution_text(record, rank_names):
    """第 2 节「执行」行:成功写 原等级 → 新等级,失败/未执行写原因原文。"""
    error = record.get("error")
    actions = record.get("actions") or []
    if error:
        return str(error)
    if not actions:
        return "无操作"
    if not record.get("executed"):
        return "未执行"
    old = _rank_text(record.get("rank"), rank_names)
    new = _rank_text(record.get("new_rank"), rank_names)
    return f"成功 → {old} → {new}"


def build_roster(guild_groups, records, failures, cfg):
    """把 /guild list 的分组名册摊平成逐人对账清单(第 7 节数据源)。

    顺序与名册来源完全一致(先按 groups 的插入顺序,组内保持原顺序),
    操作者可以拿报告和游戏面板逐行对照。

    状态取值见 _STATUS_TEXT,按判定链的最终结果优先:
      跳过职位 > 有记录(按 trigger 分名单拦截/已处理)
               > 有失败项(屏蔽词/查询失败) > 未处理
    无记录也无失败项的人仍会列出(status=unprocessed)—— 名册必须零遗漏,
    对账差值非 0 时由报告显式报缺陷。
    """
    rec_by_player = {str(r.get("player")): r for r in records or []}
    fail_by_player = {}
    for f in failures or []:
        pid = f.get("player")
        if pid is not None:
            fail_by_player[str(pid)] = f
    skip = set(cfg.get("skip_ranks") or [])

    roster = []
    for rank, players in (guild_groups or {}).items():
        for pid in players or []:
            key = str(pid)
            reason = None
            if rank in skip:
                status = "skip_rank"
            elif key in rec_by_player:
                trigger = rec_by_player[key].get("trigger")
                if trigger == "blacklist":
                    status = "blacklist"
                elif trigger == "not_in_whitelist":
                    status = "not_in_whitelist"
                elif trigger == "whitelist_protected":
                    status = "protected"
                else:
                    status = "processed"
            elif key in fail_by_player:
                failed = fail_by_player[key]
                # 屏蔽词判定要看名字本身(整串星号):失败项上的 blocked 标记只在
                # 异步路径写入,串行路径没有,两条路径必须得出同一结论。
                status = (
                    "blocked" if failed.get("blocked") or is_masked_name(key) else "query_failed"
                )
                reason = failed.get("reason")
            elif is_masked_name(key):
                # 兜底:屏蔽词玩家在任何路径下都不该被算成"查询失败"或"未处理",
                # 即使失败清单漏记(异步/串行两条路径的失败结构不同),名字本身就是证据。
                status = "blocked"
            else:
                status = "unprocessed"
            entry = {"player": pid, "rank": rank, "status": status}
            if reason is not None:
                entry["reason"] = reason
            roster.append(entry)
    return roster


def _online_text(counts, operation_mode):
    """在线成员数:UI 模式界面上没有该聚合值,必须写"不适用"而不是 0/空。"""
    online = counts.get("online_total")
    if online is not None:
        return str(online)
    if operation_mode == "ui":
        return "不适用（UI 模式无此项）"
    return "未统计"


def _render_header(lines, guild_name, cfg, run_meta):
    """第 0 节:头部元信息。计数必须自洽,缺失字段给占位符而不是 0。"""
    counts = run_meta.get("counts") or {}
    mode = str(run_meta.get("operation_mode") or "command")
    capabilities = run_meta.get("capabilities") or []
    level = (cfg.get("log") or {}).get("level")
    window = _window_seconds(run_meta.get("started_at"), run_meta.get("ended_at"))
    lines.append(f"=== {guild_name}公会 运行分析报告 ===")
    lines.append(f"统计日: {today_str()}")
    lines.append(
        f"运行窗口: {_ts_text(run_meta.get('started_at'))} → "
        f"{_ts_text(run_meta.get('ended_at'))}  (总耗时 {_fmt_seconds(window)})"
    )
    lines.append("")
    lines.append(f"执行人: {cfg.get('admin') or '(未填写)'}")
    lines.append(
        f"构建变体: {run_meta.get('build_variant') or _PLACEHOLDER}        "
        f"已启用能力: {', '.join(str(c) for c in capabilities) or _PLACEHOLDER}"
    )
    lines.append(
        f"操作模式: {mode}                       "
        f"日志级别: {_num_text(level)}({_LOG_LEVEL_NAMES.get(level, _PLACEHOLDER)})"
    )
    lines.append("")
    lines.append(
        f"成员总数: {_num_text(counts.get('member_total'))}   "
        f"在线: {_online_text(counts, mode)}   "
        f"实际查询: {_num_text(counts.get('queried'))}   "
        f"参与判定: {_num_text(counts.get('decided'))}"
    )
    lines.append(
        f"查询成功: {_num_text(counts.get('query_ok'))}   "
        f"查询失败: {_num_text(counts.get('query_fail'))}   "
        f"屏蔽词不可查: {_num_text(counts.get('blocked_name'))}"
    )
    lines.append("")


def _render_timings(lines, run_meta):
    """第 1 节:阶段耗时与结果。"""
    lines.append("--- 一、阶段耗时 ---")
    stages = run_meta.get("stage_timings") or []
    lines.append(f"{'阶段':<14}{'耗时':<10}{'成功':<6}{'失败':<6}备注")
    total = 0.0
    for stage in stages:
        seconds = stage.get("seconds")
        try:
            total += float(seconds)
        except (TypeError, ValueError) as e:
            # 阶段没记到秒数时只影响合计;用 warning 保证默认级别下也落盘
            logger.log_warning(f"阶段耗时非数值,不计入合计: {seconds!r} ({e!r})")
        lines.append(
            f"{str(stage.get('name') or _PLACEHOLDER):<14}"
            f"{_fmt_seconds(seconds):<10}"
            f"{stage.get('ok', 0):<6}{stage.get('fail', 0):<6}"
            f"{stage.get('note') or ''}"
        )
    lines.append("-" * 44)
    lines.append(f"{'合计':<14}{_fmt_seconds(total):<10}")
    lines.append("")


def _render_decisions(lines, records, cfg):
    """第 2 节:逐人决策明细(本报告核心,每人多行)。"""
    rank_names = cfg.get("rank_names") or {}
    include_daily = bool((cfg.get("report") or {}).get("include_daily_detail"))
    lines.append("--- 二、逐人决策明细 ---")

    by_rank = {rank: [] for rank in _ANALYSIS_RANK_ORDER}
    for r in records or []:
        by_rank.setdefault(r.get("rank"), []).append(r)

    for rank in list(by_rank):
        items = by_rank[rank]
        if not items:
            continue
        lines.append("")
        lines.append(f"---{_rank_text(rank, rank_names)}（{len(items)}人）---")
        for idx, r in enumerate(items, 1):
            lines.append(f"[{idx}] {r.get('player', '')}")
            weekly = f"    周贡献: {r.get('weekly_total', 0)}"
            if include_daily and r.get("daily"):
                daily = " / ".join(f"{label} {value}" for label, value in r["daily"])
                weekly += f"   (每日: {daily})"
            lines.append(weekly)
            lines.append(
                f"    加入: {r.get('joined_at') or _PLACEHOLDER}"
                f"    上次在线: {r.get('last_online') or _PLACEHOLDER}"
            )
            lines.append(f"    判定: {_decision_text(r, cfg)}")
            lines.append(f"    执行: {_execution_text(r, rank_names)}")
            lines.append("")


def _render_skips(lines, records, cfg, run_meta):
    """第 3 节:失败与跳过清单(每类都给人数 + 名单/分组 + 原因 + 策略)。"""
    rank_names = cfg.get("rank_names") or {}
    roster = run_meta.get("roster") or []
    lines.append("--- 三、失败与跳过 ---")
    lines.append("")

    query_failed = [e for e in roster if e.get("status") == "query_failed"]
    lines.append(f"查询失败(重试耗尽) {len(query_failed)} 人:")
    for entry in query_failed:
        reason = entry.get("reason") or _STATUS_TEXT["query_failed"]
        lines.append(f"  {entry.get('player')} : {reason}")
    lines.append("")

    blocked = [e for e in roster if e.get("status") == "blocked"]
    lines.append(f"屏蔽词玩家 {len(blocked)} 人(名称被游戏替换为全星号,指令不可查):")
    per_rank = {}
    for entry in blocked:
        per_rank[entry.get("rank")] = per_rank.get(entry.get("rank"), 0) + 1
    if per_rank:
        lines.append(
            "  "
            + "    ".join(
                f"{_rank_text(rank, rank_names)}: {num} 人" for rank, num in per_rank.items()
            )
        )
    lines.append(f"  处理: blocked_name_policy={cfg.get('blocked_name_policy')}")
    lines.append("")

    blacklist = [e.get("player") for e in roster if e.get("status") == "blacklist"]
    lines.append(f"黑名单跳过 {len(blacklist)} 人:")
    if blacklist:
        lines.append("  " + "  ".join(str(p) for p in blacklist))
    lines.append("")

    not_whitelisted = [e.get("player") for e in roster if e.get("status") == "not_in_whitelist"]
    if cfg.get("whitelist"):
        lines.append(f"白名单外跳过 {len(not_whitelisted)} 人:")
        if not_whitelisted:
            lines.append("  " + "  ".join(str(p) for p in not_whitelisted))
    else:
        lines.append("白名单外跳过 0 人: (白名单未启用)")
    lines.append("")

    protected = [e for e in roster if e.get("status") == "protected"]
    if protected:
        lines.append(f"保护名单 {len(protected)} 人(免降职与踢出,升职仍生效):")
        lines.append("  " + "  ".join(str(e.get("player")) for e in protected))
        lines.append("")

    # dry-run / 开关关闭 / 二次确认跳过都不是"执行失败",必须按实际执行状态区分,
    # 否则报告会把整批未执行的人误报成失败。
    exec_enabled = bool((cfg.get("switches") or {}).get("execution_enabled", True))
    exec_failed = [
        r
        for r in records or []
        if r.get("error")
        and (r.get("actions") or [])
        and exec_enabled
        and not r.get("blocked_by_switch")
        and not r.get("skipped_by_confirm")
    ]
    lines.append(f"执行失败 {len(exec_failed)} 人:")
    for r in exec_failed:
        lines.append(f"  {r.get('player')} - {_action_text(r.get('actions'))} - {r.get('error')}")
    lines.append("")


def _render_stats(lines, records, cfg):
    """第 4 节:统计汇总(贡献分布 + 等级迁移矩阵 + 操作计数)。"""
    rank_names = cfg.get("rank_names") or {}
    stats = contribution_stats(records)
    pct = percentiles([r.get("weekly_total") for r in records or []])

    lines.append("--- 四、统计汇总 ---")
    lines.append("")
    lines.append("贡献分布:")
    lines.append(f"  合计 {stats['total']}    均值 {stats['mean']}    中位数 {stats['median']}")
    lines.append(
        f"  最高 {stats['max_value']}({stats['max_player']})"
        f"    最低 {stats['min_value']}({stats['min_player']})"
    )
    lines.append(
        f"  分位: P25={_num_text(pct['p25'])}  P50={_num_text(pct['p50'])}"
        f"  P75={_num_text(pct['p75'])}  P90={_num_text(pct['p90'])}"
    )
    lines.append("")

    matrix = rank_transition_matrix(records, _ANALYSIS_RANK_ORDER)
    cols = list(_ANALYSIS_RANK_ORDER) + ["kicked", "unchanged"]
    width = 10
    lines.append("等级迁移（行=原等级, 列=新等级）:")
    lines.append(f"{'':<{width}}" + "".join(f"{_rank_text(c, rank_names):<{width}}" for c in cols))
    for old in _ANALYSIS_RANK_ORDER:
        row = matrix.get(old) or {}
        lines.append(
            f"{_rank_text(old, rank_names):<{width}}"
            + "".join(f"{row.get(c, 0):<{width}}" for c in cols)
        )
    lines.append("")

    counts = action_counts(records)
    lines.append(
        f"操作计数: 升职 {counts['promote']}   降职 {counts['demote']}"
        f"   踢出 {counts['kick']}   无操作 {counts['none']}"
    )
    lines.append("")


def _render_config(lines, cfg, run_meta):
    """第 5 节:配置与规则快照(运行时刻的实际值,不是默认值)。"""
    lines.append("--- 五、配置快照 ---")
    if not (cfg.get("report") or {}).get("include_config_snapshot", True):
        lines.append("  （已按 report.include_config_snapshot=false 关闭）")
        lines.append("")
        return

    thresholds = cfg.get("thresholds") or {}
    lines.append("")
    lines.append("阈值:")
    parts = [f"{key}={thresholds.get(key)}({_THRESHOLD_OPS[key]})" for key in _THRESHOLD_KEYS]
    for i in range(0, len(parts), 3):
        lines.append("  " + "   ".join(parts[i : i + 3]))

    sw = cfg.get("switches") or {}
    switch_marks = (
        ("升职", "promote_enabled"),
        ("降职", "demote_enabled"),
        ("踢出", "kick_enabled"),
        ("执行", "execution_enabled"),
        ("通知", "notify_enabled"),
        ("详细日志", "verbose_log"),
    )
    lines.append(
        "开关: " + "  ".join(f"{label}{'✓' if sw.get(key) else '✗'}" for label, key in switch_marks)
    )
    lines.append(
        f"查询范围: {cfg.get('query_scope')}      跳过职位: {','.join(cfg.get('skip_ranks') or [])}"
    )
    whitelist = cfg.get("whitelist") or []
    whitelist_text = f"{len(whitelist)} 人" if whitelist else "未启用"
    lines.append(
        f"名单: 黑名单 {len(cfg.get('blacklist') or [])} 人 / 白名单 {whitelist_text}"
        f" / 保护名单 {len(cfg.get('whitelist_protected') or [])} 人"
    )

    if cfg.get("rules_enabled"):
        rules = [r for r in (cfg.get("rules") or []) if isinstance(r, dict) and r.get("enabled")]
        lines.append(f"自定义规则: {len(rules)} 条启用")
        for rule in rules:
            lines.append(
                f"  {rule.get('id')} [优先级{rule.get('priority')}] {rule.get('name') or ''}"
            )
    else:
        lines.append("自定义规则: 未启用")

    mode = str(run_meta.get("operation_mode") or "command")
    if mode == "ui":
        state_check = cfg.get("state_check") or {}
        lines.append("时间参数(UI 模式):")
        lines.append(f"  OCR 缓存 {state_check.get('cache_seconds')}s")
    else:
        delays = cfg.get("delays") or {}
        timeouts = cfg.get("timeouts") or {}
        lines.append("时间参数(仅指令模式适用):")
        lines.append(
            f"  命令间隔 {delays.get('min_send_interval')}~{delays.get('max_send_interval')}s"
            f"   响应等待 {timeouts.get('response_wait')}s"
            f"   最大重试 {timeouts.get('max_retries')}"
        )
    lines.append("")


def _render_ui(lines, run_meta):
    """第 6 节:UI 识别运行记录(仅 operation_mode=ui 时输出)。

    具体统计项由运行侧通过 run_meta["ui_sections"] 提供
    (list of {title, lines});本函数只负责排版,不臆造数据。
    """
    lines.append("--- 六、UI识别运行记录 ---")
    sections = run_meta.get("ui_sections") or []
    if not sections:
        lines.append("  （本次运行未提供 UI 识别数据）")
    for section in sections:
        lines.append(f"{section.get('title') or _PLACEHOLDER}:")
        for row in section.get("lines") or []:
            lines.append(f"  {row}")
    lines.append("")


def _render_roster(lines, records, cfg, run_meta):
    """第 7 节:全员名册 + 对账(名册条目数必须等于成员总数)。"""
    rank_names = cfg.get("rank_names") or {}
    roster = run_meta.get("roster") or []
    counts = run_meta.get("counts") or {}
    mode = str(run_meta.get("operation_mode") or "command")
    member_total = counts.get("member_total")
    rec_by_player = {str(r.get("player")): r for r in records or []}
    # 来源必须来自运行侧的真实采集方式,不能按操作模式猜 ——
    # UI 模式读不到公会详情时总数来自逐页累计,那种情况下差值为 0 没有证明力。
    source_key = str(
        counts.get("member_total_source") or ("guild_list_panel" if mode != "ui" else "")
    )
    source = _SOURCE_TEXT.get(source_key, "未知来源")
    has_total = isinstance(member_total, int) and not isinstance(member_total, bool)
    diff = (len(roster) - member_total) if has_total else None

    # 标题里的结论必须与正文同源:只有"可信来源 + 差值为 0"才配写「无遗漏」。
    # 否则标题会在报告最醒目的位置宣称一个尚未证明的事实。
    if has_total and diff == 0:
        title_tail = (
            "无遗漏"
            if source_key in ("guild_list_panel", "guild_detail_tooltip")
            else "未证明无遗漏"
        )
    elif has_total:
        title_tail = f"差值 {diff},见对账"
    else:
        title_tail = "无法对账"

    lines.append(
        f"--- 七、全员名册(共 {len(roster)} 人 = 成员总数 {_num_text(member_total)},"
        f"{title_tail})---"
    )
    lines.append("列: 序号 | 玩家 | 周贡献 | 操作 | 状态")
    lines.append("状态: " + " / ".join(dict.fromkeys(_STATUS_TEXT.values())))

    order = list(_ANALYSIS_RANK_ORDER)
    for entry in roster:
        if entry.get("rank") not in order:
            order.append(entry.get("rank"))

    seq = 1
    for rank in order:
        entries = [e for e in roster if e.get("rank") == rank]
        if not entries:
            continue
        lines.append("")
        lines.append(f"---{_rank_text(rank, rank_names)}({len(entries)}人)---")
        for entry in entries:
            record = rec_by_player.get(str(entry.get("player")))
            weekly = _num_text(record.get("weekly_total")) if record else _PLACEHOLDER
            action = _action_text(record.get("actions")) if record else _PLACEHOLDER
            status = _STATUS_TEXT.get(entry.get("status"), str(entry.get("status") or _PLACEHOLDER))
            lines.append(
                f"  {seq:03d}  {str(entry.get('player')):<20} | "
                f"{weekly:>6} | {action:<6} | {status}"
            )
            seq += 1

    lines.append("")
    lines.append("---对账---")
    lines.append(f"  名册条目合计: {len(roster)}")
    lines.append(f"  成员总数(来源: {source}): {_num_text(member_total)}")
    lines.append(f"  在线成员数: {_online_text(counts, mode)}")
    if has_total:
        if diff != 0:
            verdict = "❌ 报告缺陷"
        elif source_key in ("guild_list_panel", "guild_detail_tooltip"):
            verdict = "✅ 无遗漏"
        elif source_key == "ui_paging_accumulated":
            # 逐页累计的差值为 0 是结构性的,不能当成漏扫证明
            verdict = "⚠ 仅逐页累计,差值为 0 不构成漏扫证明"
        else:
            verdict = "⚠ 来源未知,差值为 0 不构成证明"
        lines.append(f"  差值: {diff} {verdict}")
    else:
        lines.append("  差值: - ❌ 报告缺陷(成员总数缺失,无法对账)")

    by_status = {}
    for entry in roster:
        by_status[entry.get("status")] = by_status.get(entry.get("status"), 0) + 1
    ledger = [f"{label} {by_status.get(key, 0)}" for key, label in _STATUS_LEDGER]
    if by_status.get("unprocessed"):
        ledger.append(f"{_STATUS_TEXT['unprocessed']} {by_status['unprocessed']}")
    lines.append("  其中: " + " / ".join(ledger))
    lines.append("")


def _render_analysis(guild_name, records, cfg, run_meta):
    """按 docs/详细报告规范.md 的七节结构拼出整份报告正文。"""
    lines = []
    _render_header(lines, guild_name, cfg, run_meta)
    _render_timings(lines, run_meta)
    _render_decisions(lines, records, cfg)
    _render_skips(lines, records, cfg, run_meta)
    _render_stats(lines, records, cfg)
    _render_config(lines, cfg, run_meta)
    if str(run_meta.get("operation_mode") or "command") == "ui":
        _render_ui(lines, run_meta)
    _render_roster(lines, records, cfg, run_meta)
    return "\n".join(lines).rstrip() + "\n"


def write_analysis_report(report_dir, guild_name, records, cfg, run_meta):
    """第三份报告:运行分析报告(docs/详细报告规范.md)。

    命名:<日期> <安全化公会名>公会 运行分析报告.txt(与其他报告同目录同日)。
    run_meta 约定键见 main.py 的报告点;缺键按占位符展示,不抛异常。

    报告是流程最后一步,**写入失败绝不能拖垮整次运行** —— 查询结果已经落盘,
    为一份诊断报告让进程异常退出得不偿失,所以这里兜底记 warning 后返回 None。
    """
    try:
        text = _render_analysis(guild_name, records, cfg, run_meta or {})
        _ensure_dir(report_dir)
        fname = f"{today_str()} {_safe_guild_name(guild_name)}公会 运行分析报告.txt"
        fpath = os.path.join(report_dir, fname)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(text)
        logger.trace_file_write(fpath)
        logger.log_info(
            f"[报告] 运行分析报告已写入 | report_kind=analysis "
            f"| path={_report_path_for_log(fpath)} | {_action_counts_text(records)}"
        )
        return fpath
    except Exception as e:
        logger.log_warning(f"运行分析报告写入失败(不影响运行): {e!r}")
        return None
