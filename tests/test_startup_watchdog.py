# -*- coding: utf-8 -*-
"""启动看门狗（日志规范 §6.3）：卡住 30s 必须能打线程栈并在日志里点名阶段。

看门狗本身不许把异常抛进主流程，也不许在退出时把已排队的栈转储取消掉 ——
这两条都靠测试锁住。
"""

from __future__ import annotations

import time

import pytest

from guild_auto import main as main_mod


@pytest.fixture()
def log_path(tmp_path):
    return tmp_path / "latest.log"


def test_mark_and_reach_s9(log_path):
    wd = main_mod._StartupWatchdog(log_path, interval=10, stall_seconds=30, start_seconds=30)
    wd.mark("S4 UI 入口决策")
    assert wd._stage == "S4 UI 入口决策"
    wd.reach_s9()
    assert wd._reached_s9 is True


def test_dump_thread_stack_writes_file(log_path):
    wd = main_mod._StartupWatchdog(log_path, interval=10)
    log_path.write_text("seed\n", encoding="utf-8")
    wd._dump_thread_stack()
    assert wd._dump_scheduled is True
    time.sleep(0.3)  # 0.1s 后 faulthandler 落盘
    import faulthandler

    faulthandler.cancel_dump_traceback_later()
    body = log_path.read_text(encoding="utf-8", errors="replace")
    assert "seed" in body and len(body) > len("seed\n")  # 栈已追加


def test_stop_keeps_scheduled_dump(log_path, monkeypatch):
    """已排队的转储不能被 stop 取消（否则卡住证据就丢了）。"""
    called = []
    monkeypatch.setattr(
        main_mod.faulthandler, "cancel_dump_traceback_later", lambda: called.append(1)
    )
    wd = main_mod._StartupWatchdog(log_path)
    wd._dump_scheduled = True
    wd.stop()
    assert called == []


def test_stop_cancels_when_not_scheduled(log_path, monkeypatch):
    called = []
    monkeypatch.setattr(
        main_mod.faulthandler, "cancel_dump_traceback_later", lambda: called.append(1)
    )
    main_mod._StartupWatchdog(log_path).stop()
    assert called == [1]


def test_run_reports_stall_and_slow_start(log_path, monkeypatch):
    """stall/慢启动都要记日志；重复巡检同一阶段只报一次。"""
    warnings, errors = [], []
    monkeypatch.setattr(main_mod.logger, "log_warning", lambda m: warnings.append(m))
    monkeypatch.setattr(main_mod.logger, "log_error", lambda m: errors.append(m))
    monkeypatch.setattr(main_mod, "console", lambda *a, **k: None)
    monkeypatch.setattr(main_mod._StartupWatchdog, "_dump_thread_stack", lambda self: None)

    wd = main_mod._StartupWatchdog(log_path, interval=0.01, stall_seconds=0.0, start_seconds=0.0)
    wd.start()
    time.sleep(0.08)
    wd.stop()

    assert any("[看门狗]" in w and "无进展" in w for w in warnings)
    assert any("启动卡在" in e for e in errors)
    # 去重：同一阶段只报一次
    assert sum("无进展" in w for w in warnings) == 1


def test_run_swallows_own_exception(log_path, monkeypatch):
    """看门狗自身异常必须被吞掉并留痕，绝不冒到主流程。"""
    warnings = []
    monkeypatch.setattr(main_mod.logger, "log_warning", lambda m: warnings.append(m))

    wd = main_mod._StartupWatchdog(log_path, interval=0.01, stall_seconds=0.0, start_seconds=99)

    def boom():
        raise RuntimeError("看门狗内部炸了")

    wd._dump_thread_stack = boom  # type: ignore[method-assign]
    wd.start()
    time.sleep(0.08)
    wd.stop()
    assert any("自身异常" in w for w in warnings)
