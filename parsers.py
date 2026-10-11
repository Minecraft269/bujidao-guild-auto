# -*- coding: utf-8 -*-
"""
parsers.py —— 游戏日志 → 公会数据 的纯解析层
=============================================
接口契约(全部为纯函数:无 I/O、无全局可变状态,不 import pyautogui,可直接单元测试)
-------------------------------------------------------------------------------
输入一律是**日志行列表**(str),由 log_watcher.LogWatcher.wait_for_chat_block()
返回的面板块(已剥掉首尾分隔线,保留中间空行);输出是 dict / int / list[str]。
任何解析失败一律抛 ParseError,异常消息为中文。

  parse_guild_list(block_lines, rank_names) -> dict
      /guild list 面板 →
          {"guild_name": str,
           "groups":     {rank_key: [玩家ID, ...]},   键取自 rank_names,保序去重
           "total":      int | None,   面板声明的"成员总数"
           "online":     int | None}   面板声明的"在线成员数"
  parse_guild_member(block_lines, expected_id=None) -> dict
      /guild member <ID> 面板 →
          {"player": str, "guild_name": str,
           "joined_at": str | None, "last_online": str | None,
           "weekly_total": int, "daily": [(日期或"今天", 数值), ...]}
  parse_weekly_exp(lines) -> (int, [(标签, int)])
      业务规则 §4:只统计"公会经验周贡献"段内**所有** "数值 公会经验" 行求和
      (含"今天"行;数值带千分位逗号如 1,322,去逗号后累加)。
  validate_player_name(pid) -> str
  strip_chat_prefix(line) -> str   提取 [CHAT] 之后的正文(actions.py 用于播报回显)
  ParseError(ValueError)          解析失败;消息一律中文

分层边界(硬约束)
----------------
本模块**只做解析**,不含任何业务判定:
  * §1 阈值矩阵(升/降/踢判定与边界)、§2 命令格式、§3 通知文案 全部在
    actions.py / config.json;此处不得出现任何阈值常量或操作分支。
  * §7 配置化:本模块唯一的业务参数是玩家名长度上限 MAX_NAME_LEN
    (与游戏 ID 上限一致);命令格式/文案/延时等一律由 config.json 传入或不在此层。

安全(信任边界不可省)
--------------------
玩家名走**黑名单制**校验:真实样本(网易版)存在"带空格 名字",严格白名单会误杀。
拒绝 路径分隔符 / Windows 非法字符 / ● 条目分隔符 / 花括号(format 注入面) /
方括号 / 控制字符,长度上限 MAX_NAME_LEN。用途:防伪造面板注入幽灵条目、
命令参数错位、报告文件名穿越与 format 注入。

例外:网易客户端会把敏感词(玩家名/聊天/公会名)统一替换成一整串星号
(真实样本长度 3~704 不等,同一玩家不同次渲染长度都不同)。这是真实存在的
UI 显示而非注入,由 is_masked_name 单独放行 —— 否则"中等活跃成员"整段被遮成
一行 704 星就能把整个花名册炸掉,该段 78 人连查询都轮不到。
"""

import re

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
CHAT_TAG = "[CHAT]"        # 日志中聊天内容的标记
BULLET = "●"              # /guild list 面板的条目分隔符
MAX_NAME_LEN = 48          # 玩家 ID 长度上限(与游戏一致)
# 整串星号(网易屏蔽词)名的长度上限。704 是**真实观测到**的最大值
# (一次真实运行里,中等活跃段约 78 人名字被抹平连成一片的字符数),
# 取 4096 留足余量;超过此值视为非真实面板产物,拒绝。
MASKED_NAME_MAX_LEN = 4096
WEEKLY_HEADER = "公会经验周贡献"   # 周贡献段标题

# 分组标题:"-- 会长 --"(允许任意前导/尾随空白)
_GROUP_TITLE_RE = re.compile(r"^\s*--\s*(.+?)\s*--\s*$")
# 成员总数 / 在线成员数(半角与全角冒号都认)
_TOTAL_RE = re.compile(r"成员总数\s*[:：]?\s*(\d+)")
_ONLINE_RE = re.compile(r"在线成员数\s*[:：]?\s*(\d+)")
# 周贡献段标题
_WEEKLY_HEADER_RE = re.compile(WEEKLY_HEADER)
# 周贡献明细行:"2026-08-08: 1,322 公会经验" / "今天: 0 公会经验"
_DATE_LINE_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s*[:：]\s*([\d,]+)\s*公会经验")
_TODAY_LINE_RE = re.compile(r"^\s*今天\s*[:：]\s*([\d,]+)\s*公会经验")
# 段内其它数值行(面板新增字段)——§4 要求段内所有 "数值 公会经验" 行都计入
_ANY_EXP_RE = re.compile(r"([\d,]+)\s*公会经验")
# 加入时间 / 上次在线(上次在线截到换行或括号前,避免把后续字段吞进值里)
_JOINED_RE = re.compile(r"加入时间\s*[:：]\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")
_LAST_ONLINE_RE = re.compile(r"上次在线\s*[:：]\s*([^\n(]*)")
# 玩家名黑名单:路径分隔符 / Windows 非法字符 / ● / 花括号 / 方括号 / 控制字符
# 注意:*(星号)刻意保留 —— 只有"整串纯星号"由 is_masked_name 放行,
# "*x*"/"a*b" 这类混串仍在这里被拒,glob 通配符注入不会因此破防。
_NAME_FORBIDDEN_RE = re.compile(r'[\\/:*?"<>|●{}\[\]\r\n\t\x00-\x1f]')
# 网易屏蔽词产物:敏感词被客户端整串替换成 \* 星号(长度 3~704 不定)
_MASKED_NAME_RE = re.compile(r"^\*+$")


class ParseError(ValueError):
    """解析失败(缺关键字段、格式不符、面板被截断)。消息为中文。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _strip_all(line):
    """strip + 剥离网易版聊天消息尾部的"点击复制"标记。

    标记有两种写法(" [C]" 带空格 / "[C]" 不带空格),客户端该功能有人开有人关,
    两种都必须能剥,否则 "[C]" 会被当成正文内容送进命令或报告。
    """
    s = str(line).strip()
    if s.endswith(" [C]"):
        s = s[:-4]
    elif s.endswith("[C]"):
        s = s[:-3]
    return s.strip()


def _to_int(raw, line):
    """面板数值 → int,去掉千分位逗号。

    非法数值(如 ",, 公会经验")抛 ParseError 而不是裸 ValueError:
    ParseError 是 ValueError 子类,调用方的 `except ParseError` 才拦得住,
    否则一段坏数据能穿透查询循环把流程炸掉。
    """
    try:
        return int(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        raise ParseError(f"面板数值无法解析为整数: {line!r}") from None


def is_masked_name(s):
    """整串都是 * = 网易屏蔽词占位(名字被客户端遮住,可见但不可读)。

    这是**合法但不可用**的名字:服务器端认得这个人,只是不肯把名字回显。
    必须与"玩家名本来就含 *"区分 —— 后者(如 "*x*"/"a*b")仍由黑名单拒绝,
    所以 * 继续留在 _NAME_FORBIDDEN_RE 里,glob 注入仍被挡住。
    """
    return bool(s) and _MASKED_NAME_RE.match(str(s)) is not None


def validate_player_name(pid):
    """校验玩家名安全性;空 / 整串星号(网易屏蔽词,放行但有上限) / 超长 / 危险字符。

    整串星号名要放行:704 星是网易把一整段名单抹平后的**真实**产物,
    不是注入,不能让它去撞 MAX_NAME_LEN(48)就被枪毙 —— 否则整个公会
    名单解析崩溃。返回原串让调用方带着真实原因失败(标"需人工确认"),
    而不是在发命令之前就被当成非法输入。

    但放行不等于无上限:星号名**仍**受 MASKED_NAME_MAX_LEN 约束。
    少了这条,任意长的星号串(如从伪造日志喂进 200KB 星号)会一路
    进到命令行参数与日志文件里。这是独立审查指出的真实缺口。
    """
    s = "" if pid is None else str(pid)
    if not s:
        raise ParseError("玩家名为空")
    if is_masked_name(s):
        if len(s) > MASKED_NAME_MAX_LEN:
            raise ParseError(
                f"屏蔽词占位名异常超长({len(s)}>{MASKED_NAME_MAX_LEN}): "
                f"疑似非真实面板产物,已拒绝")
        return s                      # 屏蔽词占位名:合法但不可用,原样放行
    if len(s) > MAX_NAME_LEN:
        raise ParseError(f"玩家名超长({len(s)}>{MAX_NAME_LEN}): {pid!r}")
    bad = _NAME_FORBIDDEN_RE.search(s)
    if bad:
        raise ParseError(f"玩家名含非法字符 {bad.group()!r}(防注入): {pid!r}")
    return s


def strip_chat_prefix(line):
    """提取日志行中 [CHAT] 之后的正文,兼容两种日志格式:

      [HH:MM:SS] [线程/INFO]: [CHAT] 内容                    (标准 Java 版)
      [158月2026 00:23:35] [Render thread/INFO] [模块/]: [CHAT] 内容  (网易版)
    无 [CHAT] 标记时原样返回(仍剥离复制标记)。
    """
    idx = line.rfind(CHAT_TAG)
    return _strip_all(line[idx + len(CHAT_TAG):] if idx >= 0 else line)


# ---------------------------------------------------------------------------
# 周贡献求和(业务规则 §4)
# ---------------------------------------------------------------------------
def parse_weekly_exp(lines):
    """统计"公会经验周贡献"段内所有 "数值 公会经验" 行之和,返回 (total, daily)。

    §4 口径(原样继承,含缺陷语义):
      * 只统计"公会经验周贡献"段**内**的行(标题之前、终止符之后都不算);
      * 含"今天"行;
      * 数值带千分位逗号(1,322)先去逗号再累加;
      * 段内非日期、非今天的其它数值行也计入(面板新增字段,与原脚本一致)。

    daily: [(日期 或 "今天", 数值), ...],只含能识别出标签的行(供报告展示)。

    **数据缺失与真实零贡献必须区分**:若段内标题存在却一行有效数值都解析不出
    (全部被网易遮成 *** / ??? 之类),抛 ParseError 而不是返回 0。
    否则 0 会被当成真实周贡献 —— entry 玩家 0 < 3500 直接判定踢出,
    mid 玩家 0 < 5000 判定降职。宁可漏一人,不可错踢一人(独立审查指出)。

    注:这不改 §4 的计算口径,只补"解析不出时不得伪装成 0"这一层保护 ——
    继承清单规定的是怎么加总,没规定加不出东西时该假装有结果。
    """
    total = 0
    daily = []
    in_section = False
    parsed_any = False
    for raw in lines:
        s = _strip_all(raw)
        if not s:
            continue
        if _WEEKLY_HEADER_RE.search(s):        # 进入周贡献段
            in_section = True
            continue
        if not in_section:                     # 段之前的行一律不看
            continue
        if s.startswith("-") or "加入时间" in s or "上次在线" in s:
            break                              # 段结束:分隔线 / 段外字段
        md = _DATE_LINE_RE.match(s)
        if md:
            label, number = md.group(1), md.group(2)
        else:
            mt = _TODAY_LINE_RE.match(s)
            if mt:
                label, number = "今天", mt.group(1)
            else:
                mf = _ANY_EXP_RE.search(s)     # 段内其它数值行:只进总数
                if not mf:
                    continue
                label, number = None, mf.group(1)
        value = _to_int(number, s)
        total += value
        parsed_any = True
        if label is not None:
            daily.append((label, value))
    if in_section and not parsed_any:
        raise ParseError(
            "周贡献段存在但一行有效数值都没解析到(可能被网易屏蔽词遮成 *** 或 ???): "
            "无法区分「真实零贡献」与「数据缺失」,不做任何升/降/踢判定,需人工确认")
    return total, daily


# ---------------------------------------------------------------------------
# /guild list
# ---------------------------------------------------------------------------
def _join_truncated_names(rows, is_structural):
    """把被游戏换行截断的成员行接回成整行。

    成员行以 ● 结尾 = 完整,直接收;不以 ● 结尾 = 名字被面板折行,续行在下一行,
    继续接到"以 ● 结尾 / 空行 / 结构行(公会名、分组标题、统计行)/ 块尾"为止。
    结构行与空行永远不会并进名字里。
    """
    out = []
    pending = ""
    for row in rows:                          # rows 已剔除空行
        if is_structural(row):                # 结构行:打断续行,但先保住未收齐的名字
            if pending:
                out.append(pending)
                pending = ""
            out.append(row)
            continue
        pending = f"{pending} {row}" if pending else row
        if pending.endswith(BULLET):           # 收齐了,含续行本身也带 ● 的情况
            out.append(pending)
            pending = ""
    if pending:                               # 块尾的残缺名字(面板被截断)
        out.append(pending)
    return out


def parse_guild_list(block_lines, rank_names):
    """解析 /guild list 响应块(分隔线之间的行列表)。

    返回 dict: {"guild_name", "groups", "total", "online"},见模块 docstring。
    无公会名 / 未登记的分组标题 / 一个玩家都没解析到 → ParseError。
    """
    rows = [r for r in (_strip_all(l) for l in block_lines) if r]   # 剔除空行与复制标记残留
    if not rows:
        raise ParseError("guild list 输出为空:未捕获到公会面板内容")
    guild_name = rows[0]

    title_to_key = {str(v).strip(): k for k, v in (rank_names or {}).items()}

    def is_structural(row):
        return (row == guild_name or _GROUP_TITLE_RE.match(row)
                or _TOTAL_RE.search(row) or _ONLINE_RE.search(row))

    groups = {key: [] for key in (rank_names or {})}
    total = online = None
    current = None
    for row in _join_truncated_names(rows, is_structural):
        if row == guild_name:
            continue
        m = _GROUP_TITLE_RE.match(row)
        if m:
            current = title_to_key.get(m.group(1).strip())   # 未登记的职位 → None
            continue
        if current is None:
            continue
        m = _TOTAL_RE.search(row)
        if m:
            total = int(m.group(1))
            continue
        m = _ONLINE_RE.search(row)
        if m:
            online = int(m.group(1))
            continue
        # 成员行:按 ● 切分,每个名字过黑名单(防伪造面板注入幽灵条目)
        for part in row.split(BULLET):
            pid = part.strip()
            if pid:
                groups[current].append(validate_player_name(pid))

    if not any(groups.values()):
        raise ParseError("guild list 输出中未解析到任何玩家(分组标题与 rank_names 不匹配?)")
    return {
        "guild_name": guild_name,
        # 保序去重(面板异常时可能重复出现同一名字)
        "groups": {key: list(dict.fromkeys(ids)) for key, ids in groups.items()},
        "total": total,
        "online": online,
    }


# ---------------------------------------------------------------------------
# /guild member
# ---------------------------------------------------------------------------
def parse_guild_member(block_lines, expected_id=None):
    """解析 /guild member <ID> 响应块。

    返回 dict: {"player", "guild_name", "joined_at", "last_online",
                "weekly_total", "daily"},见模块 docstring。
    expected_id 非空时:先过黑名单校验(查询目标名也可能是被注入的),再要求它确实
    出现在面板里——否则很可能是面板输出未完成,或误捕获了别人的面板。

    硬安全线(约束:破坏性操作不得建在猜测上):expected_id 是网易屏蔽词占位名
    (整串星号)时**必然抛 ParseError**,即使面板里出现了同样长度的星号也不例外。
    "谁"在这个面板上根本不可读,拿它的周贡献去升/降/踢就是猜 —— 宁可漏一人,
    不可错踢一人。上层据此把该玩家标为需人工确认,而不是当成有效记录走判定。
    """
    rows = [_strip_all(l) for l in block_lines]
    rows = [r for r in rows if r]

    if expected_id:
        pid = validate_player_name(expected_id)
        if is_masked_name(pid):
            raise ParseError(
                f"玩家名被网易屏蔽词替换为星号({len(pid)} 个 *),无法确认身份:"
                f"不做任何升/降/踢判定,需人工确认")
        if not any(pid in row for row in rows):
            raise ParseError(
                f"响应块中未找到目标玩家 [{pid}]:面板输出未完成,或误捕获了其他内容")
    if len(rows) < 2:
        raise ParseError(f"guild member 输出过短(仅 {len(rows)} 行),缺少公会名/玩家名")
    if not any(_WEEKLY_HEADER_RE.search(r) for r in rows):
        who = expected_id or rows[1]
        raise ParseError(f"[{who}] 输出中缺少'{WEEKLY_HEADER}'段,无法计算周贡献")

    guild_name = rows[0]
    # 玩家名:优先用 expected_id(它已被面板证实存在);否则取第二行(可能被折行截断,
    # 仅作展示用途——调用方应始终传 expected_id)
    player = expected_id or rows[1]

    m = _JOINED_RE.search("\n".join(rows))
    joined_at = m.group(1) if m else None
    m = _LAST_ONLINE_RE.search("\n".join(rows))
    last_online = m.group(1).strip() if m and m.group(1).strip() else None

    weekly_total, daily = parse_weekly_exp(rows)
    return {
        "player": player,
        "guild_name": guild_name,
        "joined_at": joined_at,
        "last_online": last_online,
        "weekly_total": weekly_total,
        "daily": daily,
    }