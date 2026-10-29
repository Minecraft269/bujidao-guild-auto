# -*- coding: utf-8 -*-
"""
vision/bindings.py —— hotbar 按键绑定的运行时解析(零内置默认键值)
==================================================================
`docs/UI识别模式详解.md` §八 的硬约束:
  * 玩家普遍改键(尤其 PVP 玩家),出厂默认值不可信;
  * 只能从**游戏正在写入的那份** options.txt 读取 `key_key.hotbar.<N>`;
  * 解析不到就返回 None,让上层明确失败并走候选键/人工介入,
    **不得**回退到任何"常见默认键"。

本模块只做三件纯事:读文件、按 key 名取行、把 Minecraft 键名翻成按键名。
真正的按键动作由上层注入的适配器完成。
"""

from __future__ import annotations

import os

from guild_auto.infrastructure import logger
from guild_auto.infrastructure.window_input import MC_KEY_TO_PYAUTOGUI

HOTBAR_KEY_PREFIX = "key_key.hotbar."
"""options.txt 中快捷栏绑定的键名前缀,完整键名形如 key_key.hotbar.<N>。"""

_OPTIONS_ENCODINGS = ("utf-8", "gbk", "gb2312")
_MOUSE_PREFIX = "key.mouse."
_BINDING_UNKNOWN = "key.keyboard.unknown"


def parse_options_binding(text: str, slot: int) -> str | None:
    """从 options.txt 文本中取 `key_key.hotbar.<slot>` 的值;没有该键返回 None。

    只做字面匹配,不给任何兜底值 —— 返回 None 就是"这次解析不到",
    由调用方决定换候选键还是暂停人工介入。
    """
    wanted = f"{HOTBAR_KEY_PREFIX}{slot}"
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep or key.strip() != wanted:
            continue
        value = value.strip()
        return value or None
    return None


def read_hotbar_binding(options_path: str, slot: int) -> str | None:
    """读取 options.txt 并返回 hotbar 绑定原值;文件缺失/编码失败/无该键 → None。

    编码兼容 utf-8 / gbk / gb2312(与读日志同一套经验值);三种都失败时记警告后返回
    None,不猜测内容。
    """
    if not options_path:
        logger.log_warning("vision.bindings: 未配置 game_options 路径,无法解析 hotbar 绑定")
        return None
    try:
        with open(options_path, "rb") as f:
            data = f.read()
    except OSError as e:
        logger.log_warning(f"vision.bindings: 读取 options.txt 失败({options_path}): {e}")
        return None

    for enc in _OPTIONS_ENCODINGS:
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        binding = parse_options_binding(text, slot)
        if binding is None:
            logger.log_warning(
                f"vision.bindings: {os.path.basename(options_path)} 中没有 "
                f"{HOTBAR_KEY_PREFIX}{slot},不提供默认键值"
            )
        return binding

    logger.log_warning(f"vision.bindings: options.txt 编码无法识别(已尝试 {_OPTIONS_ENCODINGS})")
    return None


def binding_to_press_key(binding: str | None) -> str | None:
    """Minecraft 绑定原值 → 按键适配器使用的键名;无法映射时返回 None。

    鼠标绑定保持原样(如 `key.mouse.4`):具体按键/按钮由输入适配器解释,
    这里不猜它是左键还是右键。未绑定的槽位(`key.keyboard.unknown`)返回 None。
    """
    value = str(binding or "").strip()
    if not value or value == _BINDING_UNKNOWN:
        return None
    if value.startswith(_MOUSE_PREFIX):
        return value
    return MC_KEY_TO_PYAUTOGUI.get(value)


def hotbar_key(options_path: str, slot: int) -> str | None:
    """读取并翻译一个 hotbar 槽位的按键;任意一步失败都返回 None(不兜底)。"""
    binding = read_hotbar_binding(options_path, slot)
    key = binding_to_press_key(binding)
    if binding is not None and key is None:
        logger.log_warning(f"vision.bindings: 无法映射绑定 {binding!r} 为按键名")
    return key
