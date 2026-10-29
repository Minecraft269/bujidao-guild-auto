# -*- coding: utf-8 -*-
"""python -m guild_auto 的入口(等价于 guild-auto 命令)。

必须走 main.run_cli():它装着 excepthook 与 Ctrl+C 中断菜单循环。
此前这里直接调 main(),导致 python -m 启动时 Ctrl+C 直接冒泡、
拿不到「回车退出 / c 继续」菜单(只有 PyInstaller exe 有)。
"""

import sys

from guild_auto.main import run_cli

if __name__ == "__main__":
    sys.exit(run_cli())
