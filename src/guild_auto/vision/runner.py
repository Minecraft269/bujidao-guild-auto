# -*- coding: utf-8 -*-
"""
vision/runner.py —— 定位 → 扫掠采集 → 转 records
=================================================
串联导航、扫掠与解析,产出与指令模式**同结构**的成员记录(额外带 online)。

失败纪律:任何一步失败都返回 ok=False 并说明原因,绝不继续点击更深的界面
(不盲点);导航失败、界面不对、扫掠不完整都算失败。

依赖注入:navigator / sweeper / open_guild / online_sampler / member_parser,
因此本层可在没有真实屏幕与 OCR 的环境里被完整测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from guild_auto.infrastructure import logger
from guild_auto.vision.navigator import NavigationError
from guild_auto.vision.tooltip import (
    KIND_GUILD_DETAIL,
    KIND_MEMBER,
    TITLE_GUILD,
    parse_guild_detail,
    parse_member_tooltip,
)

if TYPE_CHECKING:  # 仅用于类型注解
    from collections.abc import Callable

    from guild_auto.vision.models import MemberData, UIElement
    from guild_auto.vision.navigator import Navigator
    from guild_auto.vision.sweep import SweepResult, Sweeper


@dataclass(frozen=True)
class CollectionResult:
    """一次 UI 采集的结果。ok=False 时 records 为空,reason 说明失败点。"""

    ok: bool
    records: tuple[dict, ...] = ()
    reason: str = ""
    key_used: str = ""
    sweep: SweepResult | None = None
    # 公会详情 tooltip 里的「人数: [N/M]」中的 N。
    # None = 读不到(未注入读取器/界面上没找到公会详情物品/OCR 无该行)——
    # 上层必须据此退回「逐页累计」并在报告注明来源,**不能**拿记录条数冒充。
    member_total: int | None = None


class VisionRunner:
    """把导航、扫掠、解析串成一次完整采集。"""

    def __init__(
        self,
        *,
        navigator: Navigator,
        sweeper: Sweeper,
        open_guild: Callable[[], None] | None = None,
        online_sampler: Callable[[UIElement], bool | None] | None = None,
        member_parser: Callable[..., MemberData] = parse_member_tooltip,
        guild_detail_reader: Callable[[UIElement], str | None] | None = None,
    ):
        self._navigator = navigator
        self._sweeper = sweeper
        self._open_guild = open_guild
        # 在线状态靠像素颜色采样;未注入时记 UNREADABLE,不猜离线
        self._online_sampler = online_sampler
        self._member_parser = member_parser
        # 公会详情 tooltip 读取器(悬停 book → 截图 → OCR)。
        # 未注入时成员总数不可知,上层退回逐页累计并注明来源。
        self._guild_detail_reader = guild_detail_reader

    def _read_member_total(self, sweep) -> int | None:
        """从扫掠结果里找「公会详情」元素,悬停读 tooltip 并解析人数。

        任何一步失败都返回 None(而不是拿扫到的记录条数充当总数)——
        那会让报告的对账变成自证循环,跨页漏扫也发现不了。
        """
        if self._guild_detail_reader is None:
            return None
        element = next((e for e in sweep.elements if e.kind == KIND_GUILD_DETAIL), None)
        if element is None:
            logger.log_warning(
                "vision.runner: 扫掠未找到公会详情物品,成员总数改为逐页累计(报告将注明来源)"
            )
            return None
        try:
            text = self._guild_detail_reader(element)
        except (RuntimeError, OSError, ValueError, KeyError) as e:
            # 注意不能 catch 太宽:ui_mode 的 _RescanRequired 刻意不继承 RuntimeError,
            # 必须穿过这里冒到重扫循环 —— 否则暂停恢复后的重扫信号会被吞掉,
            # 用旧画面继续采集。
            logger.log_warning(f"vision.runner: 读取公会详情失败,总数改为逐页累计: {e!r}")
            return None
        if not text:
            return None
        total = parse_guild_detail(text)
        if total is None:
            logger.log_warning(
                "vision.runner: 公会详情 tooltip 无「人数: [N/M]」行,总数改为逐页累计"
            )
        return total

    def collect(self) -> CollectionResult:
        try:
            key = self._navigator.enter_guild_ui(self._open_guild)
        except NavigationError as e:
            logger.log_warning(f"vision.runner: 进入公会界面失败,停止采集(不盲点): {e}")
            return CollectionResult(ok=False, reason=f"进入公会界面失败:{e}")

        # 导航后重新全扫,确认真的在公会系统界面再继续
        screen = self._navigator.current_screen()
        if screen != TITLE_GUILD:
            logger.log_warning(
                f"vision.runner: 导航后界面不是公会系统(实际 {screen}),停止采集(不盲点)"
            )
            return CollectionResult(
                ok=False,
                reason=f"导航后界面不是公会系统(实际: {screen}),停止采集(不盲点)",
                key_used=key,
            )

        try:
            sweep = self._sweeper.sweep()
        except (ValueError, RuntimeError) as e:
            logger.log_warning(f"vision.runner: 扫掠失败,停止采集(不盲点): {e}")
            return CollectionResult(ok=False, reason=f"扫掠失败:{e}", key_used=key)

        if not sweep.complete:
            reason = (
                f"扫掠不完整:预期 {sweep.expected_cells} 格,仅识别到语义 "
                f"{sweep.recognized_cells} 格(缺口 {sweep.missing_cells})"
            )
            logger.log_warning(f"vision.runner: {reason},需重扫后再采集")
            return CollectionResult(ok=False, reason=reason, key_used=key, sweep=sweep)

        records = []
        for element in sweep.elements:
            if element.kind != KIND_MEMBER:
                continue  # 翻页/返回/组队钻石等无关元素跳过
            online = self._online_sampler(element) if self._online_sampler else None
            member = self._member_parser(element.text, online=online)
            records.append(member.as_record())

        member_total = self._read_member_total(sweep)

        logger.log_info(
            f"vision.runner: UI 采集完成 {len(records)} 人 "
            f"(按键 {key},停留点 {len(sweep.points)},识别语义 {sweep.recognized_cells} 格,"
            f"成员总数来源={'公会详情 tooltip' if member_total is not None else '逐页累计'})"
        )
        return CollectionResult(
            ok=True,
            records=tuple(records),
            key_used=key,
            sweep=sweep,
            member_total=member_total,
        )
