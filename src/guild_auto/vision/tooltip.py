# -*- coding: utf-8 -*-
"""
vision/tooltip.py —— tooltip 纯解析 + 界面/元素语义分类
=======================================================
材质包会改变全部图标外观,但 **界面标题文字与 tooltip 文字不变**
(服务器下发)。因此本模块只按文字判断语义,**不看图标、不看像素、不做模板匹配**。

对外接口(全部纯函数):
  parse_member_tooltip(text, online=...) -> MemberData
  parse_guild_detail(text) -> int | None
  classify_title(title) -> "settings" | "guild" | "member_detail" | "confirm" | "unknown"
  classify_element(tooltip_text) -> 语义值(见下方 KIND_* 常量)
"""

from __future__ import annotations

import re

from guild_auto.vision.models import DAILY_ROWS, UNREADABLE, MemberData

# ── 界面标题 → 所处界面 ────────────────────────────────────────────────────
TITLE_SETTINGS = "settings"
TITLE_GUILD = "guild"
TITLE_MEMBER_DETAIL = "member_detail"
TITLE_CONFIRM = "confirm"
TITLE_UNKNOWN = "unknown"

_TITLE_MAP = {
    "装扮与个人设定": TITLE_SETTINGS,
    "公会系统": TITLE_GUILD,
    "确认界面": TITLE_CONFIRM,
}

# ── tooltip 语义 ───────────────────────────────────────────────────────────
KIND_MEMBER = "member"
KIND_NEXT_PAGE = "next_page"
KIND_PREV_PAGE = "prev_page"
KIND_BACK = "back"
KIND_GUILD_DETAIL = "guild_detail"
KIND_PROMOTE = "promote"
KIND_DEMOTE = "demote"
KIND_KICK = "kick"
KIND_CONFIRM = "confirm"
KIND_CANCEL = "cancel"
KIND_PARTY = "party"  # 组队钻石:无关物品,必须排除
KIND_UNKNOWN = "unknown"

# ── 解析用正则(只认文字) ──────────────────────────────────────────────────
_ITEM_ID_RE = re.compile(r"^minecraft:[a-z0-9_.]+$")
_RANK_RE = re.compile(r"公会身份\s*[:：]\s*([^\n]+?)\s*$", re.MULTILINE)
_JOINED_RE = re.compile(r"首次加入\s*[:：]\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")
# 绝对时间 + 括号内相对时间(半角/全角括号都认);相对时间可缺
_LAST_ONLINE_RE = re.compile(
    r"上次在线\s*[:：]\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})"
    r"\s*(?:[（(]\s*([^）)]*?)\s*[）)])?"
)
_WEEKLY_RE = re.compile(r"公会经验周贡献\s*[:：]\s*([\d,]+)\s*公会经验")
_DAILY_TODAY_RE = re.compile(r"^今天\s*[:：]\s*([\d,]+)\s*公会经验")
_DAILY_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*[:：]\s*([\d,]+)\s*公会经验")
_GUILD_COUNT_RE = re.compile(r"人数\s*[:：]\s*[\[［]\s*(\d+)\s*/\s*\d+\s*[\]］]")


def _lines(text: str) -> list[str]:
    """拆行并去掉空行;同时剔除 tooltip 末尾的 `minecraft:<id>` 行。"""
    out = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or _ITEM_ID_RE.match(line):
            continue
        out.append(line)
    return out


def _to_int(raw: str) -> int | None:
    """去掉千分位逗号转 int;非法值返回 None(交给 UNREADABLE,不写 0)。"""
    try:
        return int(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_member_tooltip(text: str, online: bool | None = None) -> MemberData:
    """解析成员 tooltip 的六字段 + 7 日明细。

    online 必须由像素颜色采样提供(OCR 只读到"●"):传 None 表示未采样,
    结果记为 UNREADABLE —— 不能把"没采样"当成离线。
    缺字段一律 UNREADABLE,禁止当 0。
    """
    lines = _lines(text)
    joined = "\n".join(lines)

    player = lines[0] if lines else UNREADABLE

    rank_match = _RANK_RE.search(joined)
    rank = rank_match.group(1) if rank_match else UNREADABLE

    joined_match = _JOINED_RE.search(joined)
    joined_at = joined_match.group(1) if joined_match else UNREADABLE

    online_match = _LAST_ONLINE_RE.search(joined)
    if online_match:
        last_online = online_match.group(1)
        relative = (online_match.group(2) or "").strip() or UNREADABLE
    else:
        last_online = UNREADABLE
        relative = UNREADABLE

    weekly_match = _WEEKLY_RE.search(joined)
    weekly: int | str = UNREADABLE
    if weekly_match:
        parsed = _to_int(weekly_match.group(1))
        if parsed is not None:
            weekly = parsed

    daily: list[tuple[str, int]] = []
    for line in lines:
        today_match = _DAILY_TODAY_RE.match(line)
        if today_match:
            value = _to_int(today_match.group(1))
            if value is not None:
                daily.append(("今天", value))
            continue
        date_match = _DAILY_DATE_RE.match(line)
        if date_match:
            value = _to_int(date_match.group(2))
            if value is not None:
                daily.append((date_match.group(1), value))

    return MemberData(
        player=player,
        rank=rank,
        joined_at=joined_at,
        last_online=last_online,
        last_online_relative=relative,
        weekly_total=weekly,
        online=UNREADABLE if online is None else bool(online),
        daily=tuple(daily[:DAILY_ROWS]),
    )


def parse_guild_detail(text: str) -> int | None:
    """取公会详情 tooltip 里 `人数: [N/M]` 的 N;读不到返回 None。

    None 表示"不可读",由上层退回逐页成员计数并在报告注明来源 —— 不得当 0。
    """
    match = _GUILD_COUNT_RE.search(str(text or ""))
    if not match:
        return None
    return _to_int(match.group(1))


def classify_title(title: str | None) -> str:
    """按界面标题判定所处界面。

    标题是成员详情界面时内容就是玩家名(屏蔽词玩家为全星号),因此
    "已知标题之外的非空文本"一律视为成员详情;空标题/其它 ...界面 视为未知。
    """
    text = str(title or "").strip()
    if not text:
        return TITLE_UNKNOWN
    if text in _TITLE_MAP:
        return _TITLE_MAP[text]
    if text.endswith("界面"):
        return TITLE_UNKNOWN
    return TITLE_MEMBER_DETAIL


def classify_element(tooltip_text: str) -> str:
    """按 tooltip 文字判定元素语义。顺序即优先级,禁止按图标/像素判断。

    两个易错点:
      * 确认界面的 tooltip 同时含"踢出"(请确定您要踢出此用户),
        必须先判 confirm 再判 kick,否则会把确认按钮当成踢出按钮;
      * "返回公会界面"必须先于"返回",否则返回上级会被当成确认界面的取消。
    """
    text = str(tooltip_text or "")
    if not text.strip():
        return KIND_UNKNOWN
    if "公会身份" in text or "公会经验周贡献" in text:
        return KIND_MEMBER
    if "组队" in text:
        return KIND_PARTY
    if "返回公会界面" in text:
        return KIND_BACK
    if "上一页" in text:
        return KIND_PREV_PAGE
    if "下一页" in text:
        return KIND_NEXT_PAGE
    if "人数" in text or "公会详情" in text:
        return KIND_GUILD_DETAIL
    if "点击确认" in text or "请确定" in text:
        return KIND_CONFIRM
    if "点击返回" in text:
        return KIND_CANCEL
    if "升职" in text:
        return KIND_PROMOTE
    if "降职" in text:
        return KIND_DEMOTE
    if "踢出" in text:
        return KIND_KICK
    return KIND_UNKNOWN
