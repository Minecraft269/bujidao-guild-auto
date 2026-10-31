# -*- coding: utf-8 -*-
"""config/loader.py 的错误路径（覆盖率缺口，CI 门禁 98% 要这些真分支）。

这些分支平时只在「磁盘写不进去 / 日志系统本身坏了」时走到 ——
正是最不能静默兜底的地方：写盘失败必须让上层知道，日志不可用必须留痕到 stderr。
"""

from __future__ import annotations

import io
import sys

import pytest

from guild_auto.config import loader as L
from guild_auto.config.defaults import DEFAULT_CONFIG


def _unwritable_path(tmp_path):
    """构造一个「父路径是文件」的路径：open 会抛 NotADirectoryError（OSError 子类）。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    return blocker / "config.json"


def test_ensure_default_config_write_failure_raises_and_reports(tmp_path, monkeypatch):
    """首次生成配置写盘失败：必须抛出，且控制台要说出「写失败」而不是静默。"""
    seen = []
    monkeypatch.setattr(L, "console", lambda msg, level="info": seen.append((level, msg)))

    with pytest.raises(OSError):
        L.ensure_default_config(_unwritable_path(tmp_path))

    assert seen, "写盘失败必须留下控制台痕迹"
    assert seen[0][0] == "error"
    assert "写入失败" in seen[0][1]


def test_save_config_write_failure_raises_and_reports(tmp_path, monkeypatch):
    """保存配置写盘失败：UI 会以为已保存，必须明确报「磁盘配置未更新」。"""
    seen = []
    monkeypatch.setattr(L, "console", lambda msg, level="info": seen.append((level, msg)))

    with pytest.raises(OSError):
        L.save_config(_unwritable_path(tmp_path), DEFAULT_CONFIG)

    assert seen and seen[0][0] == "error"
    assert "未更新" in seen[0][1]


def test_console_falls_back_to_stderr_when_logger_broken(monkeypatch):
    """logger 本身不可用（导入/初始化异常）→ 写 stderr，不能递归再调 logger。"""
    from guild_auto.infrastructure import logger as logger_mod

    def _boom(*_a, **_k):
        raise RuntimeError("日志系统坏了")

    monkeypatch.setattr(logger_mod, "log_info", _boom)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)

    L.console("只走控制台", level="info")

    text = buf.getvalue()
    assert "[console]" in text
    assert "日志系统不可用" in text


def test_console_survives_when_stderr_also_unavailable(monkeypatch):
    """连 stderr 都写不了时：安静返回，不抛（否则启动会因日志问题直接崩）。"""
    from guild_auto.infrastructure import logger as logger_mod

    def _boom(*_a, **_k):
        raise RuntimeError("日志系统坏了")

    class _BrokenStderr:
        def write(self, _s):
            raise OSError("stderr 已关闭")

    monkeypatch.setattr(logger_mod, "log_info", _boom)
    monkeypatch.setattr(sys, "stderr", _BrokenStderr())

    L.console("无处可写", level="info")  # 不抛即通过
