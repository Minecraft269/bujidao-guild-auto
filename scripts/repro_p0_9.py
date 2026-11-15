# -*- coding: utf-8 -*-
"""P0-9 最小复现/验收脚本 —— 启动期配置预检 + 看门狗与长等待冲突

直接跑:
    python scripts/repro_p0_9.py

照抄实机同一份日志（2026-11-15 11:17–11:19）的两件事：
  1. game_log 是模板占位符，validate_config 却返回 errors=0 → 放行到 S8 干等 60 秒
  2. 等待期间看门狗固定 30s 阈值 < 60s 等待 → 误报「启动卡在 S7」+ 转储线程栈

三项断言（末尾 PASS/FAIL 汇总，退出码 0/1）：
  1 占位符配置 → 启动 ≤2 秒报 error、点出 game_log 键，且**不进 S8 等待**
  2 真实但暂缺的路径 → 允许等待；等待期间日志里 grep '看门狗' 为空、无线程栈转储（连跑三次）
  3 真实存在的路径 → 正常启动（回归）

看门狗阈值在这里按比例压缩（0.5s 心跳 / 1.5s 等待，真实是 30s / 60s）：
压缩只是为了让脚本 5 秒内跑完，冲突关系（阈值 < 等待）与实机完全相同。
"""

import os
import sys
import tempfile
import time
import traceback
from types import SimpleNamespace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from guild_auto import main as main_mod  # noqa: E402
from guild_auto.config import load_config, save_config  # noqa: E402
from guild_auto.infrastructure import logger  # noqa: E402
from guild_auto.infrastructure.log_watcher import LogWatcher  # noqa: E402


class _Exit(Exception):
    def __init__(self, code):
        super().__init__(f"exit({code})")
        self.code = code


def _exit_fn(code):
    """真的抛 —— 类构造 Exit(1) 只是造了个异常对象,不会中断流程。"""
    raise _Exit(code)


def _args(config_path):
    return SimpleNamespace(
        config=config_path,
        admin=None,
        log=None,
        options=None,
        dry_run=False,
        debug=False,
        debug_skip=None,
        ui=None,
    )


def _wait_log_lines(log_text):
    """看门狗相关行 + faulthandler 线程栈痕迹（转储会写出 'Thread 0x...' 或 'File "...'）。"""
    hits = [ln for ln in log_text.splitlines() if "看门狗" in ln]
    stack = [ln for ln in log_text.splitlines() if ln.startswith("Thread 0x") or 'File "' in ln]
    return hits, stack


# ---------------------------------------------------------------------------
# 1) 占位符配置:启动 ≤2 秒报 error,且不进 S8 等待
# ---------------------------------------------------------------------------
def check_placeholder_rejected_fast():
    d = tempfile.mkdtemp(prefix="p0_9_placeholder_")
    cfg_path = os.path.join(d, "config.json")
    cfg = load_config(cfg_path)  # 默认配置(含占位符 game_log)
    save_config(cfg_path, cfg)

    printed = []
    t0 = time.monotonic()
    try:
        main_mod.prepare_config(
            _args(cfg_path), console_fn=lambda *a, **k: printed.append(str(a[0])), exit_fn=_exit_fn
        )
        code = None
    except _Exit as e:
        code = e.code
    took = time.monotonic() - t0
    text = "\n".join(printed)
    assert code == 1, f"占位符配置应 exit(1)，实际 exit({code})"
    assert took <= 2.0, f"启动判断用了 {took:.2f}s（要求 ≤2s）"
    assert "game_log" in text, "错误提示没有点出 game_log 键名"
    assert "占位符" in text, f"错误提示没说明是占位符: {text[:300]}"
    # "不进 S8 等待"的直接证据:全程没有等待行
    log_hits = [ln for ln in printed if "S8 日志等待打开" in ln]
    assert not log_hits, f"仍然进入了 S8 等待: {log_hits}"
    return f"exit(1) 用时 {took:.2f}s；提示: {[ln for ln in printed if '占位符' in ln][0][:90]}"


# ---------------------------------------------------------------------------
# 2) 真实但不存在的路径:允许等待，期间看门狗不得误报/转储（三次）
# ---------------------------------------------------------------------------
def _run_wait_with_watchdog(wait_seconds=1.5):
    d = tempfile.mkdtemp(prefix="p0_9_wait_")
    log_dir = os.path.join(d, "logs")
    logger.init_logger(log_dir=log_dir, level=4, enabled=True)
    log_path = os.path.join(log_dir, "latest.log")
    game_log = os.path.join(d, "game", "latest.log")  # 父目录存在、文件暂缺
    os.makedirs(os.path.dirname(game_log))

    wd = main_mod._StartupWatchdog(log_path, interval=0.5, stall_seconds=0.5, start_seconds=0.5)
    wd.start()
    wd.mark("S7 运行环境构建(build_runtime)")
    t0 = time.monotonic()
    try:
        LogWatcher(game_log).open(wait_seconds=wait_seconds)
        outcome = "opened"
    except FileNotFoundError:
        outcome = "timeout"
    finally:
        wd.reach_s9()
    took = time.monotonic() - t0
    body = ""
    if os.path.exists(log_path):
        body = open(log_path, encoding="utf-8", errors="replace").read()
    return outcome, took, body


def check_wait_allowed_without_false_alarm():
    details = []
    for i in (1, 2, 3):
        outcome, took, body = _run_wait_with_watchdog()
        hits, stack = _wait_log_lines(body)
        assert outcome == "timeout", f"第{i}次:父目录存在、文件暂缺应当允许等待到超时"
        assert 1.0 <= took < 6.0, f"第{i}次:等待时长异常 {took:.2f}s"
        assert not hits, f"第{i}次:等待期间看门狗仍写日志: {hits}"
        assert not stack, f"第{i}次:等待期间仍转储了线程栈（{len(stack)} 行）"
        details.append(f"#{i} {took:.2f}s 看门狗行=0 栈行=0")
    return "; ".join(details)


# ---------------------------------------------------------------------------
# 3) 真实存在的路径:正常启动（回归）
# ---------------------------------------------------------------------------
def check_existing_file_opens():
    d = tempfile.mkdtemp(prefix="p0_9_ok_")
    logger.init_logger(log_dir=os.path.join(d, "logs"), level=4, enabled=True)
    game_log = os.path.join(d, "latest.log")
    with open(game_log, "wb") as f:
        f.write("公会面板\n".encode("gbk"))
    t0 = time.monotonic()
    w = LogWatcher(game_log)
    w.open(wait_seconds=1.0)
    took = time.monotonic() - t0
    w.close()
    assert w.encoding, "正常文件没能确定编码"
    return f"打开成功 用时 {took:.2f}s 编码={w.encoding}"


CHECKS = [
    ("占位符 → ≤2s error 且不进 S8 等待", check_placeholder_rejected_fast),
    ("真实暂缺路径 → 允许等待，看门狗零误报（三次）", check_wait_allowed_without_false_alarm),
    ("真实存在路径 → 正常启动", check_existing_file_opens),
]


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    for _n in ("log_warning", "log_error", "log_critical", "log_exception", "log_debug"):
        setattr(logger, _n, lambda *a, **k: None)

    results = []
    for name, fn in CHECKS:
        t0 = time.time()
        try:
            detail = fn()
        except AssertionError as e:
            results.append((name, "fail", str(e)))
        except Exception:
            results.append((name, "fail", traceback.format_exc(limit=3)))
        else:
            results.append((name, "pass", f"{detail} ({time.time() - t0:.1f}s)"))

    print()
    for name, status, detail in results:
        print(f"[{status.upper()}] {name}: {detail}")
    failed = [r for r in results if r[1] == "fail"]
    print(f"\n{len(results) - len(failed)} passed / {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
