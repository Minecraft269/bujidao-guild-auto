# -*- coding: utf-8 -*-
"""logger 早启动：init 之前的日志不能丢（43 条复核缺口的根因）。

日志目录来自配置，而配置要 S1/S2 才读出来 —— init_logger 之前打的每一行
原本直接 return 丢掉，于是「启动阶段无法落盘」。这里锁住回灌行为。
"""

from __future__ import annotations

import pytest

from guild_auto.infrastructure import logger

_GLOBALS = ("_logger", "_log_dir", "_current_level", "_enabled", "_initialized")


@pytest.fixture()
def fresh_logger(tmp_path):
    """隔离 logger 全局与 prelog 缓冲，测试间互不影响。"""
    saved = {name: getattr(logger, name) for name in _GLOBALS}
    saved_prelog = list(logger._PRELOG)
    saved_dropped = logger._PRELOG_DROPPED
    logger._logger = None
    logger._initialized = False
    logger._PRELOG.clear()
    logger._PRELOG_DROPPED = 0
    try:
        yield tmp_path
    finally:
        for name, value in saved.items():
            setattr(logger, name, value)
        logger._PRELOG[:] = saved_prelog
        logger._PRELOG_DROPPED = saved_dropped


def test_lines_before_init_are_replayed_in_order(fresh_logger):
    logger.log_info("[S0] 进程启动")
    logger.log_warning("[S2] 配置校验")
    logger.init_logger(str(fresh_logger), level=4, enabled=True)
    logger.log_info("init 之后")

    text = (fresh_logger / "latest.log").read_text(encoding="utf-8")
    assert "[prelog +" in text, "init 之前的日志必须回灌"
    assert text.index("[S0] 进程启动") < text.index("[S2] 配置校验") < text.index("init 之后")


def test_prelog_buffer_is_bounded(fresh_logger, monkeypatch):
    """长时间未初始化时缓冲必须有上限，并记录丢弃数。"""
    monkeypatch.setattr(logger, "_PRELOG_MAX", 5)
    for i in range(8):
        logger.log_info(f"line-{i}")
    logger.init_logger(str(fresh_logger), level=4, enabled=True)

    text = (fresh_logger / "latest.log").read_text(encoding="utf-8")
    assert "已丢弃最早 3 条" in text
    # 保留的是**最早**的 5 条（启动阶段最需要的恰恰是 S0 那几条），后来的丢弃
    assert "line-0" in text and "line-4" in text
    assert "line-5" not in text and "line-7" not in text
