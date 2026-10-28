# -*- coding: utf-8 -*-
"""
guild_auto —— 布吉岛公会管理脚本
================================
按关注点分层的包结构(依赖严格单向,禁止反向):

    config/          配置读取与校验        (零业务逻辑)
    domain/          纯业务逻辑            (零 IO)
    application/     编排 domain           (不含硬件交互)
    infrastructure/  硬件交互              (可 mock)
    concurrency/     并发状态机与线程池     (经接口注入,与主流程解耦)
    ui/              可选图形入口           (核心流程不依赖)

⚠ 并发层目录名是 concurrency/ 而不是 async/ —— async 是 Python 保留字,
`from guild_auto.async import x` 会直接 SyntaxError。
"""

__version__ = "2.0.0"
