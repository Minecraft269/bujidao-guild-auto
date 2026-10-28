# -*- coding: utf-8 -*-
"""
config 层 —— 配置读取与校验(零业务逻辑)
========================================
对外统一从这里取,调用方不必知道内部拆成了三个文件:

  defaults.py  DEFAULT_CONFIG / CONFIG_COMMENTS / KEY_ALIASES —— 默认值与中文注释(单一真源)
  schema.py    VALID_SCOPES / VALID_NOTIFY_MODES / validate_config —— 结构契约与校验规则
  loader.py    ensure_default_config / load_config / save_config /
               apply_cli_overrides / console —— JSONC 解析、落盘、命令行覆盖

历史调用方写的是 `from config import console` 这种平铺形式,
现在一律 `from guild_auto.config import console` —— 本文件保证名字不变。
"""

from guild_auto.config.defaults import (
    CONFIG_COMMENTS,
    DEFAULT_CONFIG,
    KEY_ALIASES,
)
from guild_auto.config.loader import (
    _dump_jsonc,
    _strip_jsonc,
    apply_cli_overrides,
    console,
    ensure_default_config,
    load_config,
    save_config,
)
from guild_auto.config.schema import (
    VALID_NOTIFY_MODES,
    VALID_SCOPES,
    validate_config,
)

__all__ = [
    "CONFIG_COMMENTS",
    "DEFAULT_CONFIG",
    "KEY_ALIASES",
    "VALID_SCOPES",
    "VALID_NOTIFY_MODES",
    "validate_config",
    "ensure_default_config",
    "load_config",
    "save_config",
    "apply_cli_overrides",
    "console",
    "_dump_jsonc",
    "_strip_jsonc",
]
