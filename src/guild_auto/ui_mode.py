# -*- coding: utf-8 -*-
"""
ui_mode.py —— UI 识别模式的查询阶段(US-007 主流程接线)
===================================================================
把 vision 层的「导航 → 扫掠 → 解析」接进主流程,产出与指令模式**同构**的
成员记录(额外带 online),并把 UI 运行记录交给第三份报告第六节。

两条硬纪律:
  1. **不静默降级**:没有 OCR/截图适配器、导航失败、扫掠不完整 —— 一律抛
     UiCollectionError 或 AdapterError,绝不"退回去发聊天命令"。
  2. **不拿缺失当零**:字段不可读(UNREADABLE)的成员转成失败项、不进判定,
     与指令模式"查询失败"同一口径。

依赖注入:adapters / runner 都可替换,因此本模块可被测试完全驱动,不碰真实屏幕。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from guild_auto.application import runtime as runtime_mod
from guild_auto.application.pause import wait_if_paused
from guild_auto.config import console
from guild_auto.domain.parsers import is_masked_name
from guild_auto.infrastructure import logger
from guild_auto.vision.adapters import build_default_adapters
from guild_auto.vision.bindings import hotbar_key
from guild_auto.vision.navigator import NavigationError, Navigator
from guild_auto.vision.runner import VisionRunner
from guild_auto.vision.sweep import GridSpec, Sweeper
from guild_auto.vision.tooltip import KIND_GUILD_DETAIL

MAX_RESCANS = 3
"""暂停恢复后连续重扫的上限:超过就停下报错,不无限循环。"""

CRITICAL_FIELDS = ("player", "rank", "joined_at", "last_online", "weekly_total", "daily")
"""与指令模式同构所必需的字段。online 是 UI 模式独有,缺了不算失败。"""

UNKNOWN_RANK = "unknown"
"""界面身份无法映射到 rank_names 时名册归入的组(报告会原样展示,不丢人)。"""

UI_GUILD_NAME_UNKNOWN = "未识别"
"""UI 模式无法从界面可靠读出公会名且配置留空时的展示值(不是业务数据)。"""


class UiCollectionError(RuntimeError):
    """UI 采集失败(导航/扫掠/适配器)。上层必须让它冒泡,不得改走指令模式。"""


class _RescanRequired(Exception):
    """暂停恢复后本次扫掠作废:界面可能已被用户改动,必须重新全扫。

    刻意不继承 RuntimeError —— VisionRunner 只在扫掠阶段捕获
    ValueError/RuntimeError,这个信号必须穿过它冒到本模块的重扫循环。
    """


@dataclass
class UiRunStats:
    """一次 UI 查询的运行记录(第三份报告第六节的数据源)。"""

    key_used: str = ""
    key_presses: list[str] = field(default_factory=list)
    ocr_calls: int = 0
    ocr_confidence_sum: float = 0.0
    low_confidence_calls: int = 0
    low_confidence_threshold: float = 0.0
    navigation_enters: int = 0
    rescans: int = 0
    skipped_notify_execute: bool = True

    def record_ocr(self, confidence: float) -> None:
        """记录一次 OCR 调用与置信度(低置信度次数供报告展示)。"""
        value = float(confidence)
        self.ocr_calls += 1
        self.ocr_confidence_sum += value
        if value < self.low_confidence_threshold:
            self.low_confidence_calls += 1

    def sections(self) -> list[dict]:
        """转成 reporter._render_ui 认识的 {title, lines} 结构。"""
        average = self.ocr_confidence_sum / self.ocr_calls if self.ocr_calls else 0.0
        pressed = " → ".join(self.key_presses) if self.key_presses else "(未按任何键)"
        auto = runtime_mod.auto_pause_stats()
        sections = [
            {
                "title": "按键试探",
                "lines": [
                    f"尝试按键: {pressed}(共 {len(self.key_presses)} 次)",
                    f"成功键: {self.key_used or '无'}",
                    f"成功键已缓存,本次会话复用 {1 if self.key_used else 0} 次",
                ],
            },
            {
                "title": "OCR 识别",
                "lines": [
                    f"总识别 {self.ocr_calls} 次   平均置信度 {average:.2f}",
                    f"低置信度(<{self.low_confidence_threshold:g}) "
                    f"{self.low_confidence_calls} 次(扫掠中会二次确认)",
                ],
            },
            {
                "title": "界面导航",
                "lines": [f"进入公会界面 {self.navigation_enters} 次   重新全扫 {self.rescans} 次"],
            },
            {
                "title": "自动暂停",
                "lines": [
                    f"触发 {auto['triggers']} 次(检测到用户键鼠活动)   恢复 {auto['resumes']} 次"
                ],
            },
        ]
        if self.skipped_notify_execute:
            sections.append(
                {
                    "title": "通知与执行",
                    "lines": [
                        "本次跳过通知/执行:UI 模式仅实现 查询→报告(不发送 /gc、不执行升/降/踢)",
                    ],
                }
            )
        return sections


@dataclass(frozen=True)
class UiQueryOutcome:
    """UI 查询阶段交给 main() 的结果:名册、记录、失败项与运行统计。"""

    guild: dict
    records: list
    failures: list
    stats: UiRunStats


def _grid_spec(vision_cfg) -> GridSpec:
    """按配置构建扫掠网格(坐标与分辨率相关,由用户实机标定)。"""
    grid = vision_cfg["grid"]
    return GridSpec(
        origin_x=int(grid["origin_x"]),
        origin_y=int(grid["origin_y"]),
        cell_width=int(grid["cell_width"]),
        cell_height=int(grid["cell_height"]),
        columns=int(grid["columns"]),
        rows=int(grid["rows"]),
        step_ratio=float(vision_cfg["sweep_gap_ratio"]),
    )


def build_pipeline(cfg, adapters, stats: UiRunStats, gate) -> VisionRunner:
    """把配置 + 适配器组装成 VisionRunner。

    gate 在每个真实动作(按键/悬停/截图/OCR)前执行:暂停时阻塞,恢复后若
    有"需重扫"标志则抛 _RescanRequired,让整次扫掠作废重来。
    """
    vision_cfg = cfg["vision"]
    low_confidence = float(vision_cfg["low_confidence"])
    stats.low_confidence_threshold = low_confidence

    def _press(key):
        gate()
        stats.key_presses.append(str(key))
        adapters.press(key)

    def _capture():
        gate()
        return adapters.capture()

    def _ocr(image):
        gate()
        text, confidence = adapters.ocr(image)
        stats.record_ocr(confidence)
        return text, confidence

    def _hover(point):
        gate()
        adapters.hover(point)

    def _scan_title():
        """全屏 OCR 取第一行非空文本作为界面标题(标题在界面顶部,行优先)。"""
        text, _confidence = _ocr(_capture())
        for line in str(text or "").splitlines():
            if line.strip():
                return line.strip()
        return ""

    def _make_sweeper():
        return Sweeper(
            spec=_grid_spec(vision_cfg),
            expected_cells=int(vision_cfg["grid"]["expected_cells"]),
            stay_ms=int(vision_cfg["stay_ms"]),
            hover=_hover,
            capture=_capture,
            ocr=_ocr,
            sleep=adapters.sleep,
            low_confidence=low_confidence,
            max_missing_ratio=float(vision_cfg["grid"]["max_missing_ratio"]),
        )

    def _open_guild():
        """在「装扮与个人设定」界面扫掠找公会入口,识别到「公会详情」才点击。

        禁止按位置盲点:格子混有无关物品(组队钻石),必须先悬停读 tooltip。
        """
        result = _make_sweeper().sweep()
        for element in result.elements:
            if element.kind == KIND_GUILD_DETAIL:
                gate()
                adapters.click(element.rect.center)
                return
        raise NavigationError(
            "在「装扮与个人设定」界面未找到公会入口(未识别到「公会详情」tooltip),不盲点"
        )

    def _read_guild_detail(element):
        """悬停「公会详情」物品 → 等 tooltip 渲染 → 截图 → OCR,返回 tooltip 文本。

        这是成员总数**唯一可信来源**(「人数: [N/M]」)。读不到时返回 None,
        由 runner 记 warning,上层退回逐页累计并在报告注明来源 ——
        绝不能拿扫到的记录条数冒充总数,那会让对账变成自证循环。
        """
        _hover(element.rect.center)
        adapters.sleep(int(vision_cfg["stay_ms"]) / 1000.0)
        text, _confidence = _ocr(_capture())
        return text

    navigator = Navigator(
        first_key=hotbar_key(cfg["game_options"], int(vision_cfg["hotbar_slot"])),
        press=_press,
        scan_title=_scan_title,
        sleep=adapters.sleep,
        escape=adapters.escape,
        max_misclick=int(vision_cfg["max_misclick"]),
    )
    return VisionRunner(
        navigator=navigator,
        sweeper=_make_sweeper(),
        open_guild=_open_guild,
        guild_detail_reader=_read_guild_detail,
    )


def _make_gate():
    """动作门:暂停时阻塞;恢复后若需要重扫,抛出信号让本次扫掠作废。"""

    def gate():
        wait_if_paused()
        if runtime_mod.consume_rescan_flag():
            raise _RescanRequired

    return gate


def _collect_with_rescan(runner, adapters, stats: UiRunStats):
    """采集;因暂停恢复而要求重扫时,重置窗口后从头再来。"""
    for _attempt in range(MAX_RESCANS + 1):
        stats.navigation_enters += 1
        try:
            return runner.collect()
        except _RescanRequired:
            stats.rescans += 1
            logger.log_warning(
                f"UI 模式:暂停恢复,第 {stats.rescans} 次重新全扫确认界面(不沿用暂停前的界面状态)"
            )
            console("  暂停恢复:重置窗口并重新全扫确认界面...")
            try:
                adapters.escape()  # 关掉暂停期间用户可能打开的界面
            except RuntimeError as e:
                # Esc 失败不该打断重扫:下一次扫掠会重新读标题确认界面
                logger.log_warning(f"UI 恢复重置(按 Esc)失败,仍继续重扫: {e!r}")
            wait_if_paused()
    raise UiCollectionError(f"自动暂停/恢复过于频繁:连续重扫 {MAX_RESCANS} 次仍未完成 UI 查询")


def _build_outcome(
    cfg, ui_records, stats: UiRunStats, member_total: int | None = None
) -> UiQueryOutcome:
    """把 UI 采集记录转成与指令模式同构的 records + 名册 + 失败项。

    member_total 必须是**外部可验证**的来源:公会详情 tooltip 的「人数: [N/M]」。
    读不到(None)时才退回逐页累计,并如实标注来源 —— 报告据此判断对账是否可信。
    """
    rank_names = cfg["rank_names"]
    display_to_rank = {str(display): rank for rank, display in rank_names.items()}
    skip_ranks = set(cfg["skip_ranks"])
    scope = cfg["query_scope"]

    groups: dict[str, list[str]] = {rank: [] for rank in rank_names}
    records, failures = [], []
    online_sampled = False
    online_total = 0

    for raw in ui_records:
        record = dict(raw)
        player = str(record.get("player") or "")
        display_rank = str(record.get("rank") or "")
        rank = display_to_rank.get(display_rank)
        group_rank = rank or UNKNOWN_RANK
        groups.setdefault(group_rank, []).append(player)

        if isinstance(record.get("online"), bool):
            online_sampled = True
            online_total += 1 if record["online"] else 0

        if is_masked_name(player):
            failures.append(
                {
                    "player": player,
                    "rank": group_rank,
                    "blocked": True,
                    "reason": "屏蔽词玩家: 界面名字为星号,身份不可读,需人工确认",
                }
            )
            continue
        if rank is None:
            failures.append(
                {
                    "player": player,
                    "rank": group_rank,
                    "reason": f"无法识别的公会身份: 界面显示 {display_rank!r}",
                }
            )
            continue

        missing = [name for name in CRITICAL_FIELDS if name in (record.get("unreadable") or [])]
        if missing:
            failures.append(
                {
                    "player": player,
                    "rank": rank,
                    "reason": f"UI 识别字段不可读: {', '.join(missing)},需人工确认",
                }
            )
            continue

        record["rank"] = rank
        # 跳过职位 / 查询范围外:名册仍保留(第 7 节必须零遗漏),但不进判定
        if rank in skip_ranks or (scope != "all" and rank != scope):
            continue
        records.append(record)

    guild_name = str(cfg.get("guild_name") or "").strip() or UI_GUILD_NAME_UNKNOWN
    if member_total is None:
        # 退回逐页累计:数值等于本次扫到的成员条数,对账因此不能发现跨页漏扫,
        # 报告必须把这个事实写出来(来源标注),而不是假装是服务器人数。
        total = len(ui_records)
        total_source = "ui_paging_accumulated"
    else:
        total = member_total
        total_source = "guild_detail_tooltip"
    guild = {
        "guild_name": guild_name,
        "total": total,
        "total_source": total_source,
        "online": online_total if online_sampled else None,
        "groups": groups,
    }
    return UiQueryOutcome(guild=guild, records=records, failures=failures, stats=stats)


def stage_query_ui(rt, cfg, *, adapters=None, runner=None) -> UiQueryOutcome:
    """UI 模式查询阶段:导航 → 扫掠 → 解析 → 同构 records。

    rt 只为与其它 stage_* 保持同一调用形状(暂停/确认等统一走 runtime 单例)。
    adapters/runner 可注入:测试用假实现驱动,不碰真实屏幕。
    失败一律抛 UiCollectionError / AdapterError,调用方不得降级到指令模式。
    """
    if adapters is None:
        adapters = build_default_adapters(cfg, on_simulated_input=runtime_mod.note_simulated_input)
    stats = UiRunStats()
    gate = _make_gate()
    if runner is None:
        runner = build_pipeline(cfg, adapters, stats, gate)

    runtime_mod.set_auto_pause_guard(True)
    try:
        result = _collect_with_rescan(runner, adapters, stats)
    finally:
        runtime_mod.set_auto_pause_guard(False)

    if not result.ok:
        raise UiCollectionError(f"UI 识别查询失败:{result.reason}")
    stats.key_used = result.key_used
    console(f"UI 识别查询完成: {len(result.records)} 条成员记录")
    return _build_outcome(cfg, result.records, stats, result.member_total)
