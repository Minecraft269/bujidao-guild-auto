# -*- coding: utf-8 -*-
"""
vision/capabilities.py —— 最小能力判定(阶段 7 扩展为完整能力矩阵)
==================================================================
配置强制要求某项能力、而当前构建没有时,**启动即明确报错**,禁止静默降级
(plan.txt【阶段 7】第 3 条 / docs/流程与架构图.md §八)。

阶段 6 只交付 vision 层本身,主流程仍走指令模式,因此本构建尚不能执行
`operation_mode="ui"`。阶段 7 接线后,把判定改为查询能力注册表即可 ——
本模块只负责"报错"这一个动作,不实现整个矩阵。
"""

from __future__ import annotations

import importlib.util

CAPABILITY_ID = "vision"

# 本构建是否已把 UI 识别模式接入主流程。
# 阶段 6 仅交付 vision 层(导航/扫掠/验证/runner),主流程未接线 → False。
# 若直接放行,用户会以为在跑 UI 模式,实际仍按指令模式发聊天命令 ——
# 静默降级比报错危险,所以这里宁可启动失败。
_UI_PIPELINE_WIRED = False


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
