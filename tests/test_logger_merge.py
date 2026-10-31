# -*- coding: utf-8 -*-
"""双日志聚合：logger.merge_gui_log()。

用户要求「对外只留 latest.log」：GUI 仍写自己的 gui.log 作中间文件，
由核按字节偏移增量并进 latest.log，每行前缀 [gui]。
这里锁住：增量（不重复并）、无文件安静跳过、禁用时不写、读失败留痕但不抛。
"""

from __future__ import annotations

import pytest

from guild_auto.infrastructure import logger as L


@pytest.fixture()
def logdir(tmp_path, monkeypatch):
    """把 logger 指向临时目录，并把合并偏移归零（模块级全局，测试间要隔离）。"""
    monkeypatch.setattr(L, "_log_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(L, "_enabled", True, raising=False)
    monkeypatch.setattr(L, "_GUI_MERGE_OFFSET", 0, raising=False)
    return tmp_path


def test_merge_appends_with_gui_prefix_and_is_incremental(logdir):
    (logdir / "gui.log").write_text("[+0.1s][INFO][ipc] hello\n", encoding="utf-8")

    assert L.merge_gui_log() == 1
    merged = (logdir / "latest.log").read_text(encoding="utf-8")
    assert "[gui] [+0.1s][INFO][ipc] hello" in merged

    # 增量：没有新行时再调一次不得重复写入
    assert L.merge_gui_log() == 0
    assert (logdir / "latest.log").read_text(encoding="utf-8").count("[gui]") == 1

    # 追加一行后只并新增的那一行
    with (logdir / "gui.log").open("a", encoding="utf-8") as f:
        f.write("[+0.2s][INFO][ipc] bye\n")
    assert L.merge_gui_log() == 1
    assert "[gui] [+0.2s][INFO][ipc] bye" in (logdir / "latest.log").read_text(encoding="utf-8")


def test_merge_drop_removes_intermediate_file(logdir):
    """drop=True：合并后删掉 gui.log —— 用户要求「对外只留 latest.log」。"""
    (logdir / "gui.log").write_text("[+0.1s][INFO] x\n", encoding="utf-8")

    assert L.merge_gui_log(drop=True) == 1
    assert not (logdir / "gui.log").exists()
    assert "[gui] [+0.1s][INFO] x" in (logdir / "latest.log").read_text(encoding="utf-8")


def test_merge_without_gui_log_is_noop(logdir):
    assert L.merge_gui_log() == 0
    assert not (logdir / "latest.log").exists()


def test_merge_skips_when_logging_disabled(logdir, monkeypatch):
    monkeypatch.setattr(L, "_enabled", False, raising=False)
    (logdir / "gui.log").write_text("x\n", encoding="utf-8")
    assert L.merge_gui_log() == 0


def test_merge_reports_read_failure_without_raising(logdir, monkeypatch):
    """gui.log 读不了（占用/权限）时：不抛异常，但要有一条 WARNING。"""
    warned: list[str] = []
    monkeypatch.setattr(L, "log_warning", warned.append, raising=False)
    (logdir / "gui.log").write_text("x\n", encoding="utf-8")

    real_open = open

    def _boom(path, *a, **k):
        if str(path).endswith("gui.log"):
            raise OSError("占用中")
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", _boom)
    assert L.merge_gui_log() == 0
    assert warned and "gui.log" in warned[0]
