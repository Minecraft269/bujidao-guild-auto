# -*- coding: utf-8 -*-
"""
vision/sweep.py —— 鼠标扫掠定位与采集(定位与采集合一)
=====================================================
`docs/UI识别模式详解.md` §八·五:
  * 停留点按**格子坐标网格**生成,不按"看到像什么"决定(材质包会改模型);
  * 每个停留点:悬停 → 等 tooltip 渲染(≥120ms,不得跳过)→ 截图 → OCR;
  * 扫掠结束用「预期格子数 vs 实际识别到语义的格数」对账,缺口超阈值判不完整。

所有外部动作(hover/capture/ocr/sleep)都由调用方注入,本模块零真实设备依赖。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from guild_auto.infrastructure import logger
from guild_auto.vision.models import Rect, UIElement
from guild_auto.vision.tooltip import KIND_UNKNOWN, classify_element

if TYPE_CHECKING:  # 仅用于类型注解,运行期不需要
    from collections.abc import Callable
    from typing import Any

MIN_STAY_MS = 120
"""停留时长下限:太短 tooltip 来不及渲染,会漏数据。低于此值直接拒绝执行。"""


@dataclass(frozen=True)
class GridSpec:
    """格子网格描述。step_ratio=1.0 时步长等于格子宽高(每格一次)。"""

    origin_x: int
    origin_y: int
    cell_width: int
    cell_height: int
    columns: int
    rows: int
    step_ratio: float = 1.0


@dataclass(frozen=True)
class SweepResult:
    """一次扫掠的完整结果 + 完整性自检结论。"""

    elements: tuple[UIElement, ...]
    points: tuple[Rect, ...]
    expected_cells: int
    recognized_cells: int
    missing_cells: int
    complete: bool


def grid_points(spec: GridSpec) -> tuple[Rect, ...]:
    """按行优先生成停留点(每个点是一个格子的矩形,悬停用其中心)。

    步长 = 格子宽高 × step_ratio:1.0 恰好覆盖每格,越小越密(重叠采样),
    不允许 >1(会留缝,直接漏格)。
    """
    if spec.columns <= 0 or spec.rows <= 0:
        raise ValueError(f"扫掠网格行列必须为正整数,当前: {spec.columns}x{spec.rows}")
    if not 0 < spec.step_ratio <= 1:
        raise ValueError(f"扫掠步长比例必须在 (0, 1],当前: {spec.step_ratio}")
    step_x = spec.cell_width * spec.step_ratio
    step_y = spec.cell_height * spec.step_ratio
    points = []
    for row in range(spec.rows):
        for col in range(spec.columns):
            points.append(
                Rect(
                    x=spec.origin_x + round(col * step_x),
                    y=spec.origin_y + round(row * step_y),
                    width=spec.cell_width,
                    height=spec.cell_height,
                )
            )
    return tuple(points)


def check_completeness(
    expected_cells: int, recognized_cells: int, *, max_missing_ratio: float = 0.0
) -> tuple[bool, int]:
    """完整性对账:返回 (是否完整, 缺口格数)。

    max_missing_ratio 是允许的缺口比例(0 = 一格都不能少)。缺口按
    "预期格子数 - 识别到语义的格数"计算,识别不到语义的格子(空槽)也算缺口 ——
    宁可重扫,不可拿漏掉的成员去做踢人判定。
    """
    if expected_cells < 0:
        raise ValueError(f"预期格子数不能为负: {expected_cells}")
    if not 0 <= max_missing_ratio <= 1:
        raise ValueError(f"允许缺口比例必须在 [0, 1],当前: {max_missing_ratio}")
    missing = max(expected_cells - recognized_cells, 0)
    allowed = int(expected_cells * max_missing_ratio)
    return missing <= allowed, missing


class Sweeper:
    """一次界面扫掠。注入 hover / capture / ocr / sleep,运行期可替换为真实适配器。"""

    def __init__(
        self,
        *,
        spec: GridSpec,
        expected_cells: int,
        stay_ms: int,
        hover: Callable[[tuple[int, int]], None],
        capture: Callable[[], Any],
        ocr: Callable[[Any], tuple[str, float]],
        sleep: Callable[[float], None] = time.sleep,
        low_confidence: float = 1.0,
        max_missing_ratio: float = 0.0,
    ):
        if stay_ms < MIN_STAY_MS:
            raise ValueError(
                f"停留时长 {stay_ms}ms 低于下限 {MIN_STAY_MS}ms:tooltip 来不及渲染会漏数据"
            )
        if not 0 <= low_confidence <= 1:
            raise ValueError(f"低置信度阈值必须在 [0, 1],当前: {low_confidence}")
        self.spec = spec
        self.expected_cells = expected_cells
        self.stay_ms = stay_ms
        self.hover = hover
        self.capture = capture
        self.ocr = ocr
        self.sleep = sleep
        self.low_confidence = low_confidence
        self.max_missing_ratio = max_missing_ratio

    def _read(self, rect: Rect) -> UIElement:
        """一个停留点:悬停 → 等渲染 → 截图 → OCR → 语义分类。"""
        started = time.monotonic()
        self.hover(rect.center)
        self.sleep(self.stay_ms / 1000.0)

        # 截图/OCR 是长阻塞调用:进入前后各记一条,卡住时能定位到第几个点、哪一步
        logger.log_info(f"vision.sweep: 截图开始(停留点 {rect.center},截图适配器无超时参数)")
        capture_started = time.monotonic()
        shot = self.capture()
        logger.log_info(
            f"vision.sweep: 截图返回(耗时 {(time.monotonic() - capture_started) * 1000:.1f}ms,"
            f"停留点 {rect.center})"
        )

        logger.log_info(f"vision.sweep: OCR 开始(停留点 {rect.center},超时由 OCR 适配器负责)")
        ocr_started = time.monotonic()
        text, confidence = self.ocr(shot)
        logger.log_info(
            f"vision.sweep: OCR 返回(耗时 {(time.monotonic() - ocr_started) * 1000:.1f}ms,"
            f"文本 {len(str(text or ''))} 字,置信度 {float(confidence):.3f})"
        )

        element = UIElement(
            kind=classify_element(text),
            rect=rect,
            text=str(text or ""),
            confidence=float(confidence),
        )
        logger.log_debug(
            f"vision.sweep: 停留点 {rect.center} kind={element.kind} "
            f"confidence={element.confidence:.3f} "
            f"elapsed={(time.monotonic() - started) * 1000:.1f}ms"
        )
        return element

    def _probe(self, rect: Rect) -> UIElement:
        """读取一格;低置信度再扫一次(两次取置信度更高者)。"""
        element = self._read(rect)
        if element.confidence < self.low_confidence:
            original = element.confidence
            logger.log_warning(
                f"vision.sweep: 停留点 {rect.center} 置信度 {original:.3f} "
                f"低于阈值 {self.low_confidence:.3f},触发二次扫掠"
            )
            retry = self._read(rect)
            if retry.confidence > element.confidence:
                element = retry
            logger.log_warning(
                f"vision.sweep: 停留点 {rect.center} 二次扫掠完成"
                f"(原置信度 {original:.3f},重扫后 {retry.confidence:.3f},"
                f"采用 {element.confidence:.3f})"
            )
        return element

    def sweep(self) -> SweepResult:
        started = time.monotonic()
        logger.log_info(
            f"vision.sweep: 扫掠开始(网格 {self.spec.columns}x{self.spec.rows},"
            f"expected_cells={self.expected_cells}, stay_ms={self.stay_ms},"
            f"低置信度阈值={self.low_confidence:.3f})"
        )
        points = grid_points(self.spec)
        elements = tuple(self._probe(rect) for rect in points)
        recognized = sum(1 for element in elements if element.kind != KIND_UNKNOWN)
        complete, missing = check_completeness(
            self.expected_cells, recognized, max_missing_ratio=self.max_missing_ratio
        )
        logger.log_info(
            f"vision.sweep: 扫掠结束(总耗时 {(time.monotonic() - started) * 1000:.1f}ms,"
            f"识别语义 {recognized} 格,缺口 {missing} 格,complete={complete})"
        )
        return SweepResult(
            elements=elements,
            points=points,
            expected_cells=self.expected_cells,
            recognized_cells=recognized,
            missing_cells=missing,
            complete=complete,
        )
