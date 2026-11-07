# -*- coding: utf-8 -*-
"""Trace 第 6 级埋点的真实产出与脱敏（Phase B 新增代码的分支覆盖）。

为什么需要：埋点只在 level=6 时才执行，普通测试跑在 4 级下这些分支永远不覆盖 ——
分母被 +1817 行稀释后覆盖率会掉。这里显式开到 6 级跑一遍，既覆盖分支，
又守住两条硬规矩：行格式可 grep、不出现本机绝对路径/真实玩家名。
"""

from __future__ import annotations

import copy
import json

import pytest

from guild_auto.config.defaults import DEFAULT_CONFIG
from guild_auto.config.loader import load_config, save_config
from guild_auto.config.schema import validate_config
from guild_auto.infrastructure import logger

_GLOBALS = ("_logger", "_log_dir", "_current_level", "_enabled", "_initialized")


@pytest.fixture()
def t6(tmp_path):
    """把 logger 开到 6 级并指向临时目录；结束恢复全局，避免污染其它测试。"""
    saved = {name: getattr(logger, name) for name in _GLOBALS}
    logger.init_logger(str(tmp_path), level=6, enabled=True)
    try:
        yield tmp_path / "latest.log"
    finally:
        for name, value in saved.items():
            setattr(logger, name, value)


def _trace_lines(path):
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    return [ln for ln in text.splitlines() if "TRACE" in ln]


def test_validate_config_emits_enter_exit_with_decision(t6):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["switches"]["promote_enabled"] = "yes"  # 类型错误 -> 决策点分支
    issues = validate_config(cfg)
    assert issues, "前提：这份配置必须有问题"

    tags = [ln for ln in _trace_lines(t6) if "[TRACE][config.schema.validate_config]" in ln]
    assert any("phase=ENTER" in ln for ln in tags), f"缺 ENTER：{tags}"
    assert any("phase=EXIT" in ln for ln in tags), f"缺 EXIT：{tags}"
    assert any("rules=" in ln for ln in tags), f"EXIT 未带命中规则名：{tags}"


def test_config_io_trace_redacts_absolute_path(t6, tmp_path):
    path = tmp_path / "cfg.json"
    save_config(path, copy.deepcopy(DEFAULT_CONFIG))
    loaded = load_config(path)
    assert loaded["log"]["level"] == DEFAULT_CONFIG["log"]["level"]

    traces = _trace_lines(t6)
    assert traces, "配置读写必须留下 trace"
    joined = "\n".join(traces)
    assert str(tmp_path) not in joined, "trace 里出现了绝对路径，违反脱敏要求"


def test_trace_is_off_below_level_6(tmp_path):
    """级别低于 6 时不得产生 TRACE 行（默认关闭，量控第一条）。"""
    saved = {name: getattr(logger, name) for name in _GLOBALS}
    logger.init_logger(str(tmp_path), level=4, enabled=True)
    try:
        validate_config(copy.deepcopy(DEFAULT_CONFIG))
        text = (tmp_path / "latest.log").read_text(encoding="utf-8", errors="replace")
        assert "[TRACE]" not in text
    finally:
        for name, value in saved.items():
            setattr(logger, name, value)


def test_config_roundtrip_is_unchanged_by_instrumentation(t6, tmp_path):
    """埋点不得改变返回值：保存后读回，关键值必须原样。"""
    path = tmp_path / "roundtrip.json"
    save_config(path, copy.deepcopy(DEFAULT_CONFIG))
    back = load_config(path)
    assert json.loads(json.dumps(back, ensure_ascii=False))["ui"]["enabled"] is True


def test_log_watcher_keeps_full_raw_lines_with_rate_limit(t6, tmp_path):
    """日志钩子读到的原文必须可追溯：完整行进 TRACE，并带 items/emitted/dropped 聚合行。"""
    from guild_auto.infrastructure.log_watcher import LogWatcher

    game = tmp_path / "game.log"
    game.write_text("", encoding="utf-8")
    w = LogWatcher(str(game), encoding="utf-8", poll_interval_ms=10)
    try:
        w.open(wait_seconds=1)
        with game.open("a", encoding="utf-8") as f:
            f.write("[公会] 样例玩家 周贡献 800\n[系统] 另一条样例\n")
        lines = w.read_new_lines(block=False)
        assert lines, "应当读到刚写入的行"
        assert "样例玩家" in "".join(lines)

        traces = [ln for ln in _trace_lines(t6) if "log_watcher.read_new_lines" in ln]
        assert any("raw=" in ln for ln in traces), f"缺逐行原文：{traces}"
        assert any("items=" in ln for ln in traces), f"缺聚合行：{traces}"
    finally:
        w.close()


def test_report_parse_error_writes_block_and_context_at_error_level(t6, tmp_path):
    """解析失败要能在默认级别看到现场：响应块与上下文行无条件写 ERROR。"""
    from guild_auto.domain.parsers import ParseError
    from guild_auto.infrastructure.log_watcher import LogWatcher

    game = tmp_path / "game.log"
    game.write_text("上一行\n", encoding="utf-8")
    w = LogWatcher(str(game), encoding="utf-8", poll_interval_ms=10)
    try:
        w.open(wait_seconds=1)
        with game.open("a", encoding="utf-8") as f:
            f.write("块行1\n块行2\n")
        w.read_new_lines(block=False)
        w.report_parse_error(ParseError("周贡献行缺失"), block=["块行1", "块行2"])

        text = t6.read_text(encoding="utf-8", errors="replace")
        assert "report_parse_error" in text
        assert "parse_error=ParseError" in text
        assert "block[1/2]" in text
    finally:
        w.close()
