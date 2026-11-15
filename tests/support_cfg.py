# -*- coding: utf-8 -*-
"""测试用配置工厂(P0-9)。

`DEFAULT_CONFIG["game_log"]` 是模板占位符(`C:\\Path\\To\\...`);P0-9 起
**占位符是启动校验 error**,父目录不存在也是 error。所以凡是"拿默认配置当
合法配置"的用例,都要把 game_log 换成"父目录必然存在"的合成路径 ——
本模块提供唯一一份,避免每个用例各写一遍、写得还不一致。

注意:断言"默认值本身"的用例(如 test_config_jsonc_guards 里检查默认值字面量)
必须继续直接用 DEFAULT_CONFIG,不要走这里。
"""

import copy
import os
import tempfile

from guild_auto.config.defaults import DEFAULT_CONFIG


def valid_game_log():
    """父目录(系统临时目录)必然存在、文件本身不需要存在的日志路径。"""
    return os.path.join(tempfile.gettempdir(), "guild_auto_synthetic_latest.log")


def default_cfg():
    """DEFAULT_CONFIG 深拷贝 + 可用的 game_log(其余键保持默认)。"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["game_log"] = valid_game_log()
    return cfg
