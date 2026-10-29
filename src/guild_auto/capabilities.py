# -*- coding: utf-8 -*-
"""
capabilities.py —— 构建能力注册表与变体命名(plan.txt【阶段 7】)
================================================================
本模块是中立层:只依赖标准库,不被 domain / application 反向依赖,
也不 import ui / vision / concurrency —— 它只回答"本构建包含哪些能力",
不碰任何具体实现。

为什么用注册表而不是枚举:变体 = 能力的任意子集(2^n 种)。若把 8 种组合
写死成常量,新增能力(如 web 远程控制)就要同时改代码和 CI 矩阵。这里
只维护"注册表 + 固定拼接顺序"两张表,新增能力 = 加一条注册 + 在顺序表
末尾追加标识,变体名与构建矩阵自动扩展,已有变体名不会变。

能力清单来源优先级:
1. 运行时环境变量 GUILD_AUTO_CAPABILITIES(如 "core,async");
2. 打包时写入的 guild_auto/_build_capabilities.py 常量(见 scripts/build-variants.py);
3. 都没设置 = 源码运行,默认全能力。
"""

from __future__ import annotations

import os
from itertools import combinations
from typing import Iterator

ENV_VAR = "GUILD_AUTO_CAPABILITIES"

# 基础变体名:仅 core 能力时使用;追加能力按 _APPEND_ORDER 拼接后缀。
BASE_VARIANT_NAME = "Lite"
CORE = "core"

# 能力标识 -> 拼接后缀。core 是必选基础能力,不产生后缀(空串)。
# 新增能力:在这里加一项 + 在 _APPEND_ORDER 末尾加标识,别处无需改动。
_CAPABILITIES: dict[str, str] = {
    CORE: "",
    "async": "Async",
    "gui": "Gui",
    "vision": "Vision",
}

# 固定拼接顺序 —— 拼接名是唯一权威标识。顺序一旦发布就不能改,
# 否则 Lite-Async-Gui 会突然改名,构建产物与日志横幅对不上。
_APPEND_ORDER: tuple[str, ...] = ("async", "gui", "vision")


class CapabilityError(RuntimeError):
    """配置要求的能力在当前构建中不可用(或能力标识非法)。"""


def _build_time_capabilities() -> str | None:
    """读取打包时写入的能力常量;源码运行没有该模块,返回 None。

    生成的模块只有一行 CAPABILITIES 常量(见 scripts/build-variants.py),
    因此 ImportError 只可能是"没打包过",不会掩盖真实的模块内部错误。
    """
    try:
        from guild_auto._build_capabilities import CAPABILITIES
    except ImportError:
        return None
    return str(CAPABILITIES)


def parse_capabilities(raw: str | None) -> tuple[str, ...]:
    """把 "core,async" 解析成按注册顺序排列的能力元组。

    raw=None 表示"未指定":源码运行,返回全部已注册能力。
    raw="" 表示"只含必选能力":返回仅 core。
    未知标识直接报错 —— 拼错 "asycn" 若被静默忽略,产物会悄悄少一项能力。
    """
    if raw is None:
        return tuple(_CAPABILITIES)
    wanted = {part.strip().lower() for part in raw.split(",") if part.strip()}
    unknown = sorted(wanted - set(_CAPABILITIES))
    if unknown:
        raise CapabilityError(
            f"未知能力标识: {','.join(unknown)};"
            f"已注册能力: {','.join(_CAPABILITIES)}(检查环境变量 {ENV_VAR})"
        )
    # core 必选:即使清单里没写也自动补上,保证最小变体始终可用。
    return tuple(cap for cap in _CAPABILITIES if cap == CORE or cap in wanted)


def enabled_capabilities() -> tuple[str, ...]:
    """本构建实际包含的能力(有序)。"""
    raw = os.environ.get(ENV_VAR)
    if raw is None:
        raw = _build_time_capabilities()
    return parse_capabilities(raw)


def build_variant_name(caps: tuple[str, ...] | None = None) -> str:
    """按固定顺序拼接变体名:core→Lite,其余按 async→gui→vision 追加。

    例:Lite / Lite-Async / Lite-Gui / Lite-Vision / Lite-Async-Gui-Vision。
    caps 省略时取当前构建已启用能力。
    """
    enabled = set(enabled_capabilities() if caps is None else caps)
    unknown = sorted(enabled - set(_CAPABILITIES))
    if unknown:
        raise CapabilityError(f"未知能力标识: {','.join(unknown)}")
    parts = [BASE_VARIANT_NAME]
    for cap in _APPEND_ORDER:
        if cap in enabled:
            parts.append(_CAPABILITIES[cap])
    return "-".join(parts)


def has(cap: str) -> bool:
    """当前构建是否包含某项能力。"""
    return cap in enabled_capabilities()


def require(cap: str, feature_name: str) -> None:
    """配置要求了某项能力而当前构建没有时抛 CapabilityError。

    错误信息同时给出"缺哪个能力"和"哪个功能要它" —— 只说"不支持"无法让
    用户判断该换配置还是换构建。禁止静默降级:静默降级会让用户以为在跑
    UI 识别 / 异步模式,实际走的是另一条流程。
    """
    if not has(cap):
        raise CapabilityError(
            f"当前构建({build_variant_name()})不含 {feature_name} 所需的能力 {cap!r},"
            f"无法继续。已启用能力: {','.join(enabled_capabilities())}"
        )


def variant_meta() -> dict[str, object]:
    """启动横幅与第三份报告共用的构建元信息 —— 同一处生成,避免两处漂移。"""
    caps = enabled_capabilities()
    return {"build_variant": build_variant_name(caps), "capabilities": list(caps)}


# ── 可选别名(plan.txt 阶段 7 第 1.3 条)────────────────────────────────
# 别名只服务于"人类一眼看出哪个是全功能版",**不是权威标识**:
# 产物名 / artifact / 启动横幅一律用拼接名。别名表在这里是逻辑别名而非
# 写死的字符串——`full` 在调用时按当前注册表计算,新增能力后自动包含。
_ALIASES = ("full",)


def aliases() -> dict[str, str]:
    """别名 → 权威拼接名(大小写不敏感)。供构建脚本/文档查询。"""
    full_caps = tuple(_CAPABILITIES)
    return {name: build_variant_name(full_caps) for name in _ALIASES}


def resolve_variant_alias(name: str) -> str:
    """把别名解析成权威拼接名。

    未登记的别名**直接报错**,不得静默把原值返回 —— 否则 `--variant typo`
    会一路带到构建产物名里,却没人发现变体名是错的。
    """
    key = str(name or "").strip().lower()
    table = aliases()
    if key in table:
        return table[key]
    raise CapabilityError(
        f"未知变体别名: {name!r};已登记别名: {','.join(sorted(table))}(别名不是权威标识,拼接名才是)"
    )


def build_banner() -> str:
    """启动横幅:构建变体 + 已启用能力清单。"""
    caps = enabled_capabilities()
    return f"构建变体: {build_variant_name(caps)}    已启用能力: {','.join(caps)}"


def iter_variants() -> Iterator[tuple[str, tuple[str, ...]]]:
    """枚举全部变体(2^n 种),供构建矩阵生成器使用。

    产出顺序与拼接顺序一致:先短后长,同长度按 _APPEND_ORDER 字典序。
    """
    for size in range(len(_APPEND_ORDER) + 1):
        for combo in combinations(_APPEND_ORDER, size):
            caps = (CORE, *combo)
            yield build_variant_name(caps), caps
