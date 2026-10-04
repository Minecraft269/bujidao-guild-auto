# -*- coding: utf-8 -*-
"""
parsers.py —— 日志解析器
=========================
按用户提供的日志格式规格严格实现,对玩家名含空格/特殊字符、数字千分位、
输出被多行截断等情况鲁棒。

两个解析器:
  * parse_guild_list   —— /guild list 输出 → 公会名 + 分组玩家 ID + 总数/在线
  * parse_guild_member —— /guild member <ID> 输出 → 玩家信息 + 周贡献
"""
import re

import logger

# 分组标题:"-- 会长 --"(允许任意前导/尾随空白)
_GROUP_TITLE_RE = re.compile(r"^\s*--\s*(.+?)\s*--\s*$")
# 成员总数/在线成员数
_TOTAL_RE = re.compile(r"成员总数\s*[:：]?\s*(\d+)")
_ONLINE_RE = re.compile(r"在线成员数\s*[:：]?\s*(\d+)")
# 周贡献行:"2026-08-08: 1,322 公会经验" / "今天: 0 公会经验"
_WEEKLY_RE = re.compile(r"([\d,]+)\s*公会经验")
# 日期行(用于 daily 明细;也用于剔除"今天"行的可选统计)
_DATE_LINE_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s*[:：]\s*([\d,]+)\s*公会经验")
_TODAY_LINE_RE = re.compile(r"^\s*今天\s*[:：]\s*([\d,]+)\s*公会经验")
# 加入时间 / 上次在线
_JOINED_RE = re.compile(r"加入时间\s*[:：]\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")
_LAST_ONLINE_RE = re.compile(r"上次在线\s*[:：]\s*(.+?)(?:\(|$)")
# 周贡献段标题
_WEEKLY_HEADER_RE = re.compile(r"公会经验周贡献")
# 玩家名黑名单字符:路径分隔符/"●"(条目分隔符)/花括号(format 注入面)/控制字符。
# 注意:真实样本(网易版)存在"带空格 名字",故不能用严格白名单;采用黑名单制——
# 排除可造成 幽灵条目/命令参数错位/路径与格式注入 的危险字符,长度上限 48
_NAME_FORBIDDEN_RE = re.compile(r'[\\/:*?"<>|●{}\[\]\r\n\t\x00-\x1f]')


class ParseError(ValueError):
    """解析失败(缺关键字段、格式不符)。"""


def validate_player_name(pid):
    """校验玩家名安全性;含危险字符或超长(>48)抛 ParseError。
    防伪造面板注入幽灵条目、命令参数错位、报告文件名/命令 format 注入。"""
    s = str(pid or "")
    if not s or len(s) > 48 or _NAME_FORBIDDEN_RE.search(s):
        raise ParseError(f"非法玩家名: {pid!r}")
    return pid


def strip_chat_prefix(line):
    """从日志行提取 [CHAT] 之后的实际内容。
    兼容两种格式:
      [HH:MM:SS] [线程/INFO]: [CHAT] 内容          (标准)
      [158?2026 00:23:35] [Render thread/INFO] [模块/]: [CHAT] 内容  (网易版,[CHAT] 在中间)
    并去掉网易版消息尾部的复制标记 " [C]"。
    """
    idx = line.rfind("[CHAT]")
    content = line[idx + len("[CHAT]"):] if idx >= 0 else line
    content = content.strip()
    # 网易版聊天消息尾部复制标记:如 "消息. [C]"
    if content.endswith(" [C]"):
        content = content[:-4]
    return content.strip()


def _strip_all(line):
    """strip + 去尾部复制标记 ' [C]'/'[C]'(客户端"点击复制"功能,有人开有人关)。"""
    s = line.strip()
    if s.endswith(" [C]"):
        s = s[:-4]
    elif s.endswith("[C]"):
        s = s[:-3]
    return s.strip()


def parse_weekly_exp(lines):
    """统计"公会经验周贡献"段内所有数值之和(含"今天",去千分位;仅统计段内行)。"""
    logger.trace_call("parsers.parse_weekly_exp", kwargs={"lines": len(lines)})
    total = 0
    daily = []  # [(日期, 数值)];今天行记为 ("今天", 数值)
    in_weekly = False
    for raw in lines:
        s = _strip_all(raw)
        if not s:
            continue
        if _WEEKLY_HEADER_RE.search(s):
            in_weekly = True
            continue
        if not in_weekly:
            continue
        # 段结束:遇到下一个标题类行(如 "-----" 或其它键)则停止
        if s.startswith("-") or "加入时间" in s or "上次在线" in s:
            break
        m = _DATE_LINE_RE.match(s)
        if m:
            daily.append((m.group(1), int(m.group(2).replace(",", ""))))
            total += int(m.group(2).replace(",", ""))
            continue
        m = _TODAY_LINE_RE.match(s)
        if m:
            daily.append(("今天", int(m.group(1).replace(",", ""))))
            total += int(m.group(1).replace(",", ""))
            continue
        # 其它含数值的行(如面板新增字段)也计入,与原脚本 get_weekly_exp 一致
        m = _WEEKLY_RE.search(s)
        if m:
            total += int(m.group(1).replace(",", ""))
    logger.trace_return("parsers.parse_weekly_exp", {"total": total, "daily_n": len(daily)})
    return total, daily


# ---------------------------------------------------------------------------
# /guild list 解析
# ---------------------------------------------------------------------------
def parse_guild_list(block_lines, rank_names):
    """解析 /guild list 响应块(分隔线之间的行列表)。

    返回 dict:
      guild_name: str          公会名(标题下第一非空行)
      groups:     {rank_key: [player_id, ...]}   仅含 rank_names 中登记的等级
      total:      int|None     成员总数
      online:     int|None     在线成员数
    解析失败(无公会名/无任何分组)抛 ParseError。
    """
    logger.trace_call("parsers.parse_guild_list", kwargs={"lines": len(block_lines)})
    text = "\n".join(block_lines)
    # 公会名:首个非空行(块首行为分隔线,已被 wait_for_chat_block 剔除)
    lines = [l for l in block_lines if l.strip()]
    if not lines:
        raise ParseError("guild list 输出为空")
    guild_name = _strip_all(lines[0])
    if not guild_name:
        raise ParseError("未能识别公会名")

    # 先拼行:名字可能被截断为两行(上行不以 ● 结尾时与下行拼接)
    # 注意:分组标题行/统计行/公会名行也不以 ● 结尾,必须先单独收尾,不能参与拼接
    merged = []
    i = 0
    n = len(block_lines)
    while i < n:
        line = _strip_all(block_lines[i])
        i += 1
        if not line:
            continue
        if _GROUP_TITLE_RE.match(line) or _TOTAL_RE.search(line) or _ONLINE_RE.search(line) or line == guild_name:
            merged.append(line)
            continue
        # 成员行:以 ● 结尾则完整;否则可能与下行拼接(名字被截断),
        # 直到遇到 ● 结尾、分组标题、统计行或空行
        while not line.rstrip().endswith("●") and i < n:
            nxt = _strip_all(block_lines[i])
            if not nxt:
                break
            if _GROUP_TITLE_RE.match(nxt) or _TOTAL_RE.search(nxt) or _ONLINE_RE.search(nxt):
                break
            line = line + " " + nxt
            i += 1
        merged.append(line)

    rank_to_key = {v: k for k, v in rank_names.items()}
    groups = {key: [] for key in rank_names.keys()}
    total = online = None
    current_rank = None

    for line in merged:
        if line == guild_name:
            continue
        mt = _GROUP_TITLE_RE.match(line)
        if mt:
            title = mt.group(1).strip()
            current_rank = rank_to_key.get(title)
            continue
        if current_rank is None:
            continue
        m = _TOTAL_RE.search(line)
        if m:
            total = int(m.group(1))
            continue
        m = _ONLINE_RE.search(line)
        if m:
            online = int(m.group(1))
            continue
        # 成员行:按 "●" 切分;每个名字过白名单(防伪造面板注入幽灵条目)
        for part in line.split("●"):
            pid = part.strip()
            if pid:
                validate_player_name(pid)
                groups[current_rank].append(pid)

    # 去重保序
    for key in groups:
        seen = set()
        groups[key] = [p for p in groups[key] if not (p in seen or seen.add(p))]

    if not any(groups.values()):
        raise ParseError("guild list 输出中未解析到任何分组玩家")

    result = {
        "guild_name": guild_name,
        "groups": groups,
        "total": total,
        "online": online,
    }
    logger.trace_return("parsers.parse_guild_list",
                        {"guild_name": guild_name, "total": total, "online": online,
                         "groups": {k: len(v) for k, v in groups.items()}})
    return result


# ---------------------------------------------------------------------------
# /guild member 解析
# ---------------------------------------------------------------------------
def parse_guild_member(block_lines, expected_id=None):
    """解析 /guild member <ID> 响应块。

    返回 dict:
      player:      str           玩家名(标题下第二行;优先用 expected_id 校验)
      guild_name:  str           公会名
      joined_at:   str|None      加入时间 "YYYY-MM-DD HH:MM:SS"
      last_online: str|None      上次在线(不含括号内"X天前"部分)
      weekly_total:int           总周贡献(含"今天",去千分位)
      daily:       [(日期, 数值)] 每日明细
    校验:文本中必须出现 expected_id(若提供)与"公会经验周贡献"段,否则抛 ParseError。
    """
    logger.trace_call("parsers.parse_guild_member", kwargs={"lines": len(block_lines), "expected_id": expected_id})
    text = "\n".join(block_lines)
    if expected_id:
        validate_player_name(expected_id)   # 查询目标名也过白名单(防命令参数注入)
        if expected_id not in text:
            raise ParseError(f"响应块中未找到目标玩家 [{expected_id}],可能是输出未完成或捕获了其他内容")

    lines = [l for l in block_lines if l.strip()]
    if len(lines) < 2:
        raise ParseError("guild member 输出过短,缺少公会名/玩家名")

    guild_name = _strip_all(lines[0])
    # 玩家名:优先按 expected_id 定位;否则取第二行(可能被截断,仅作展示)
    player = expected_id if expected_id else _strip_all(lines[1])

    joined = None
    m = _JOINED_RE.search(text)
    if m:
        joined = m.group(1)

    last_online = None
    m = _LAST_ONLINE_RE.search(text)
    if m:
        last_online = m.group(1).strip()

    weekly_total, daily = parse_weekly_exp(block_lines)

    if "公会经验周贡献" not in text:
        raise ParseError(f"[{player}] 输出中缺少'公会经验周贡献'段,解析失败")

    result = {
        "player": player,
        "guild_name": guild_name,
        "joined_at": joined,
        "last_online": last_online,
        "weekly_total": weekly_total,
        "daily": daily,
    }
    logger.trace_return("parsers.parse_guild_member",
                        {"player": player, "weekly_total": weekly_total,
                         "joined_at": joined, "last_online": last_online})
    return result
