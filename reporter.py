# -*- coding: utf-8 -*-
"""
reporter.py —— 报告输出
========================
职责:按原脚本风格输出两类报告(见 docs/业务规则继承清单.md §5 与用户提供的参考文件):
  * <日期> <公会名>公会 所有玩家详细贡献.txt
      格式:---高活跃--- / ---中等活跃--- / ---低活跃--- / ---入门成员---
            玩家:<ID> 周贡献为:<数值> 执行操作:<操作>
  * <公会名>_<日期>.txt(群公告)
      格式:被升职（N人）/ 被踢出（N人）/ 被降职（N人)+ ID 空格分隔 + 联系人
输出目录:配置 report_dir(相对脚本运行目录,自动创建)。
"""
import os
import re
from datetime import date

import logger

# 文件名安全化:公会名来自日志解析,替换路径分隔符与 Windows 非法字符,
# 防止 "..\..\evil" 之类名字把报告写到 report_dir 之外
_UNSAFE_FNAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def _safe_guild_name(guild_name):
    cleaned = _UNSAFE_FNAME_RE.sub("_", str(guild_name or "")).strip(". ")
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

    lines = [f"=== {guild_name}公会 所有玩家详细贡献(统计日 {today_str()}) ===",
             f"执行人: {admin or '(未填写)'}", ""]
    total_weekly = sum(r.get("weekly_total", 0) for r in records)
    lines.append(f"统计玩家数: {len(records)}   周贡献合计: {total_weekly}")
    lines.append("")
    for rk in _RANK_ORDER:
        items = grouped.get(rk) or []
        if not items:
            continue
        lines.append(f"---{_RANK_SECTION[rk]}---")
        for r in items:
            lines.append(f"玩家:{r.get('player','')} 周贡献为:{r.get('weekly_total',0)} "
                         f"执行操作:{action_display(r.get('actions'))}")
        lines.append("")

    with open(fpath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    logger.trace_file_write(fpath)
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
    return fpath
