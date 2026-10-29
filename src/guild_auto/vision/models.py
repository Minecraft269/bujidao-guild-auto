# -*- coding: utf-8 -*-
"""
vision/models.py —— 识别层数据模型(纯数据,零 I/O)
==================================================
两个模型对应两层语义:
  * UIElement  扫掠时某个格子上"看到了什么"(语义 + 坐标 + 置信度)
  * MemberData 成员 tooltip 解析出的六字段 + 7 日明细

**不可读哨兵**:tooltip 缺行、OCR 没认出来、像素采样没做 —— 一律记为
UNREADABLE(字符串"不可读"),**严禁**退化成 0 / False / None 参与判定。
"零贡献"与"没读到"是两件事,混在一起就是拿数据缺失去踢人。
"""

from __future__ import annotations

from dataclasses import dataclass

UNREADABLE = "不可读"
"""字段不可读哨兵。业务代码必须显式判断它,不得当 0 用。"""

DAILY_ROWS = 7
"""成员 tooltip 的每日明细行数(含"今天"行)。这是游戏界面结构,不是业务参数。"""

MEMBER_FIELDS = (
    "player",
    "online",
    "rank",
    "joined_at",
    "last_online",
    "weekly_total",
)
"""成员六字段(与 docs/UI识别模式详解.md §三 一致)。"""


@dataclass(frozen=True)
class Rect:
    """屏幕矩形(像素)。扫掠停留点按格子坐标生成,与图标外观无关。"""

    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2


@dataclass(frozen=True)
class UIElement:
    """一个停留点识别出的元素。kind 取自 tooltip.classify_element 的语义值。"""

    kind: str
    rect: Rect
    text: str
    confidence: float


@dataclass(frozen=True)
class MemberData:
    """成员 tooltip 的完整数据。

    online 由**像素颜色采样**注入(OCR 只能读到"●"),未采样时为 UNREADABLE。
    last_online_relative 是括号里的相对时间(如"2.75天前"),与绝对时间并存。
    """

    player: str = UNREADABLE
    rank: str = UNREADABLE
    joined_at: str = UNREADABLE
    last_online: str = UNREADABLE
    last_online_relative: str = UNREADABLE
    weekly_total: int | str = UNREADABLE
    online: bool | str = UNREADABLE
    daily: tuple[tuple[str, int], ...] = ()

    @property
    def daily_complete(self) -> bool:
        """7 日明细是否齐全(含"今天"行)。缺行同样属于数据不完整。"""
        return len(self.daily) == DAILY_ROWS

    def unreadable_fields(self) -> list[str]:
        """返回不可读的字段名(含 daily 明细不足)。空列表 = 六字段齐全。"""
        missing = [name for name in MEMBER_FIELDS if getattr(self, name) == UNREADABLE]
        if not self.daily_complete:
            missing.append("daily")
        return missing

    def as_record(self) -> dict:
        """转成与指令模式成员记录同构的 dict,额外带 online 与 source。

        weekly_total 保持原值:不可读时仍是哨兵字符串,绝不写成 0。
        下游拿到的 record["unreadable"] 非空时应标"需人工确认",不进判定。
        """
        record: dict[str, object] = {
            "player": self.player,
            "rank": self.rank,
            "joined_at": self.joined_at,
            "last_online": self.last_online,
            "last_online_relative": self.last_online_relative,
            "weekly_total": self.weekly_total,
            "daily": list(self.daily),
            # 记录里 online 用 None 表示"未采样";哨兵留在 unreadable 列表里,信息不丢
            "online": None if self.online == UNREADABLE else self.online,
            "source": "ui",
        }
        missing = self.unreadable_fields()
        if missing:
            record["unreadable"] = missing
        return record
