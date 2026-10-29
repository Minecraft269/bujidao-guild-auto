# -*- coding: utf-8 -*-
"""
vision/capabilities.py —— 最小能力判定(阶段 7 扩展为完整能力矩阵)
==================================================================
配置强制要求某项能力、而当前构建没有时,**启动即明确报错**,禁止静默降级
(plan.txt【阶段 7】第 3 条 / docs/流程与架构图.md §八)。

US-007 已把 UI 识别模式接进主流程(ui_mode.py),本模块只负责
"能力是否具备"的判定与报错,不实现具体流程。
"""

from __future__ import annotations

import importlib.util

CAPABILITY_ID = "vision"

# 本构建是否已把 UI 识别模式接入主流程。
# US-007 已接线:main() 在 operation_mode="ui" 时走 ui_mode.py
# (导航 → 扫掠 → 解析 → 与指令模式同构的 records)。因此这里放行,
# 构建裁剪掉 vision 模块时 find_spec 仍会失败,能力判定继续有效。
_UI_PIPELINE_WIRED = True


class CapabilityError(RuntimeError):
    """配置要求的能力在当前构建中不可用。"""


def has_vision_capability() -> bool:
    """本构建能否执行 UI 识别模式。

    最小判定 = 主流程已接线 且 vision 层入口可导入(构建裁剪后导入失败即无能力)。
    """
    if not _UI_PIPELINE_WIRED:
        return False
    return importlib.util.find_spec("guild_auto.vision.runner") is not None


def ensure_operation_mode_supported(mode: str, *, vision: bool | None = None) -> None:
    """校验 operation_mode 与构建能力是否匹配;不匹配抛 CapabilityError。

    vision 参数供测试/上层显式指定能力,省略时用 has_vision_capability()。
    """
    if mode == "command":
        return
    if mode != "ui":
        raise CapabilityError(f"operation_mode 取值非法: {mode!r},应为 'command' 或 'ui'")
    available = has_vision_capability() if vision is None else bool(vision)
    if not available:
        raise CapabilityError(
            '当前构建不含 UI 识别模式(vision):配置 operation_mode="ui" 无法执行。'
            '请把 operation_mode 改为 "command",或使用含 vision 能力且已接线的构建'
        )
