# -*- coding: utf-8 -*-
"""P0-8 最小复现/验收脚本 —— 启动等待不可中断

直接跑:
    python scripts/repro_p0_8.py

场景照抄实机（2026-11-15 11:17–11:19）：game_log 指向不存在的文件 → 进入 S8
日志等待（wait_seconds=60）→ 期间发 stop → 旧代码等满 60.1s 才 FileNotFoundError
崩溃，GUI 收到的 ipc-closed 还是 reason=normal。

四项独立断言，末尾打印 PASS/FAIL/SKIPPED 汇总（退出码 0/1/2 同 repro_p0_3）：
  1 停止 → 退出等待 ≤1s（真 LogWatcher + GUI「停止」同一条 RuntimeController.stop）
  2 旧代码对照 → 同样发 stop 后仍等满 60s（≥50s），**结果与 1 反转**
  3 暂停不导致退出、不消耗预算，恢复后继续等到超时（日志为证）
  4 崩溃路径 ipc-closed 的 reason=config_error（不是 normal），且日志说真话
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unittest.mock as mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from guild_auto.application import runtime as runtime_mod  # noqa: E402
from guild_auto.infrastructure import logger  # noqa: E402
from guild_auto.infrastructure.log_watcher import LogWaitInterrupted, LogWatcher  # noqa: E402
from guild_auto import main as main_mod  # noqa: E402

# P0-8 开工时的 origin/main —— 旧实现（硬睡 1.0s、不看任何标志）就在这个提交里。
# 写死提交号而不是 origin/main：合并后 origin/main 就是新代码，对照会自己消失。
OLD_REV = os.environ.get("P0_8_OLD_REV", "5dc675f4f26165fda6e5c85f603691b887adec2b")
OLD_PATH = "src/guild_auto/infrastructure/log_watcher.py"
WAIT_S = 60.0  # 实机默认值；旧代码对照必须跑满它才能证明「没有退出出口」
# 0.55s 故意落在 0.1s 检查切片的中间：耗时测出来是「半个切片」，而不是在
# 边界上恰好 0.000s（那样看着像测量坏了，虽然结论一样是 ≤1s）。
STOP_AFTER_S = 0.55

LOGS = []  # 捕获到的日志行（证据）


class Skipped:
    def __init__(self, reason):
        self.reason = reason


def _capture_logger():
    """日志写控制台会淹掉断言输出；这里捕获到 LOGS，只回放关键行。"""

    def _make(tag):
        def _rec(msg, *_a, **_kw):
            LOGS.append(f"[{tag}] {msg}")

        return _rec

    for name, tag in (
        ("log_info", "INFO"),
        ("log_warning", "WARNING"),
        ("log_error", "ERROR"),
        ("log_exception", "CRITICAL"),
        ("log_critical", "CRITICAL"),
        ("log_debug", "DEBUG"),
        ("log_trace", "TRACE"),
        ("trace_code_location", "TRACE"),
        ("trace_return", "TRACE"),
        ("trace_file_read", "TRACE"),
        ("trace_call", "TRACE"),
    ):
        if hasattr(logger, name):
            setattr(logger, name, _make(tag))


def _missing_log():
    return os.path.join(tempfile.mkdtemp(prefix="p0_8_"), "latest.log")


def _run_open_in_thread(watcher, **kwargs):
    """后台线程里跑 open()，返回 (线程, 结果字典)。异常本身就是被测结果，不抛。"""
    box = {}

    def _run():
        try:
            watcher.open(**kwargs)
            box["exc"] = None
        except BaseException as e:
            box["exc"] = e
        box["end"] = time.monotonic()

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    return th, box


# ---------------------------------------------------------------------------
# 1) 停止 → 退出等待 ≤1s（新代码）
# ---------------------------------------------------------------------------
def check_stop_interrupts():
    from guild_auto.ui.binding import RuntimeController

    runtime_mod.reset_state()
    LOGS.clear()
    watcher = LogWatcher(_missing_log())
    th, box = _run_open_in_thread(watcher, wait_seconds=WAIT_S, stop_check=runtime_mod.is_paused)
    time.sleep(STOP_AFTER_S)
    stopped_at = time.monotonic()
    RuntimeController().stop()  # 与 GUI「停止」按钮完全同一条路径
    th.join(10.0)
    assert not th.is_alive(), "stop 后 10s 仍未退出等待（信号没有出口）"
    took = box["end"] - stopped_at
    assert isinstance(box["exc"], LogWaitInterrupted), f"退出原因应为用户停止，实际 {box['exc']!r}"
    assert took <= 1.0, f"stop→退出耗时 {took:.2f}s（要求 ≤1s）"
    stop_log = [ln for ln in LOGS if "被用户停止" in ln]
    return f"stop→退出 {took:.3f}s；日志: {stop_log[0] if stop_log else '<缺>'}"


# ---------------------------------------------------------------------------
# 2) 旧代码对照：同一场景、同样发 stop → 仍等满 WAIT_S（结果反转）
# ---------------------------------------------------------------------------
def _load_old_watcher():
    src = subprocess.run(
        ["git", "show", f"{OLD_REV}:{OLD_PATH}"],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if src.returncode != 0:
        return None, f"git show {OLD_REV[:8]}:{OLD_PATH} 失败: {src.stderr.strip()[:200]}"
    if "LogWaitInterrupted" in src.stdout:
        return None, f"{OLD_REV[:8]} 已含修复（不是旧代码），请用 P0_8_OLD_REV 指定修复前提交"
    fd, tmp = tempfile.mkstemp(suffix="_old_log_watcher.py")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(src.stdout)
    spec = importlib.util.spec_from_file_location("old_log_watcher_p0_8", tmp)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, ""


def check_old_code_still_hangs():
    from guild_auto.ui.binding import RuntimeController

    mod, err = _load_old_watcher()
    if mod is None:
        return Skipped(err)
    runtime_mod.reset_state()
    watcher = mod.LogWatcher(_missing_log())
    th, box = _run_open_in_thread(watcher, wait_seconds=WAIT_S)
    time.sleep(STOP_AFTER_S)
    stopped_at = time.monotonic()
    RuntimeController().stop()
    th.join(WAIT_S + 30)
    assert not th.is_alive(), "旧代码对照没能在超时后结束（前提失效）"
    took = box["end"] - stopped_at
    assert isinstance(box["exc"], FileNotFoundError), (
        f"旧代码在 stop 后应一直等到超时 FileNotFoundError，实际 {box['exc']!r}"
    )
    assert took >= 50.0, (
        f"旧代码 stop→退出只用了 {took:.2f}s —— 对照失效（它本应无视 stop 等满 {WAIT_S:.0f}s）"
    )
    return f"旧代码 stop→退出 {took:.1f}s（≈剩余等待，无视 stop）；新代码 ≤1s —— 结果反转"


# ---------------------------------------------------------------------------
# 3) 暂停：挂起等待、不消耗预算、不导致退出；恢复后继续等（日志为证）
# ---------------------------------------------------------------------------
def check_pause_then_resume():
    runtime_mod.reset_state()
    LOGS.clear()
    watcher = LogWatcher(_missing_log())
    t0 = time.monotonic()
    # 0.3s 起暂停 1.2s（用真实 runtime 暂停标志，与 GUI「暂停」按钮同一开关）
    threading.Timer(0.3, lambda: runtime_mod.set_paused(True)).start()
    threading.Timer(1.5, lambda: runtime_mod.set_paused(False)).start()

    th, box = _run_open_in_thread(watcher, wait_seconds=2.0, stop_check=runtime_mod.is_paused)
    time.sleep(1.6)  # 已过 2.0s 预算，但因为暂停过，此时必须还在等
    still_waiting = th.is_alive() and box.get("exc") is None
    th.join(10.0)
    took = time.monotonic() - t0
    assert still_waiting, (
        f"暂停期间/刚恢复就退出了（exc={box.get('exc')!r}）——暂停被当成了停止或消耗了预算"
    )
    assert isinstance(box["exc"], FileNotFoundError), f"恢复后应等到超时，实际 {box['exc']!r}"
    assert "游戏日志不存在" in str(box["exc"])
    assert took >= 2.6, f"总耗时 {took:.2f}s——暂停的 1.2s 消耗了 2.0s 预算"
    timeout_log = [ln for ln in LOGS if "等待游戏日志超时" in ln]
    return (
        f"暂停 1.2s 未退出，总耗时 {took:.2f}s(预算 2.0s+暂停 1.2s)；"
        f"日志: {timeout_log[0] if timeout_log else '<缺>'}"
    )


# ---------------------------------------------------------------------------
# 4) 崩溃路径：reason=config_error（不是 normal），日志不打「意外关闭」
# ---------------------------------------------------------------------------
def check_crash_reason_is_honest():
    runtime_mod.reset_state()
    LOGS.clear()
    exc = FileNotFoundError("游戏日志不存在: <synthetic>/latest.log")
    reason, _text = main_mod._classify_exit(exc)
    assert reason == "config_error", f"日志缺失应归为 config_error，实际 {reason}"

    bridge = mock.Mock()
    bridge.exit_reason = "normal"  # 旧默认值：正是实机那次发出去的东西
    server = mock.Mock()
    server.connected = True
    with (
        mock.patch.object(main_mod, "_gui_bridge", return_value=bridge),
        mock.patch.object(main_mod, "main", side_effect=exc),
        mock.patch.object(main_mod, "console"),
    ):
        try:
            main_mod.run_cli()
        except SystemExit as e:
            code = e.code
        else:
            raise AssertionError("run_cli 未退出")
    assert code == 1, f"配置错误退出码应为 1，实际 {code}"
    assert bridge.exit_reason == "config_error", f"界面仍会收到 {bridge.exit_reason!r}"
    main_mod._gui_notify_core_exit(server, bridge)
    payload = server.send_event.call_args[0][1]
    assert payload["reason"] == "config_error", f"ipc-closed 报文 reason={payload['reason']!r}"
    close_log = [ln for ln in LOGS if "ipc-closed" in ln and "config_error" in ln]
    stamp_log = [ln for ln in LOGS if "退出原因" in ln]
    assert close_log, "缺少「已通知 GUI ipc-closed」日志行"
    bad = [ln for ln in LOGS if "意外关闭" in ln]
    assert not bad, f"配置错误仍被打成意外关闭: {bad}"
    return (
        f"reason={payload['reason']} code={code}；"
        f"日志: {stamp_log[0] if stamp_log else '<缺>'} | {close_log[0]}"
    )


CHECKS = [
    ("停止 → 退出等待 ≤1s", check_stop_interrupts),
    ("旧代码对照 ≥50s（结果反转）", check_old_code_still_hangs),
    ("暂停挂起、不耗预算、恢复后继续", check_pause_then_resume),
    ("崩溃路径 reason=config_error", check_crash_reason_is_honest),
]


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    _capture_logger()
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
            if isinstance(detail, Skipped):
                results.append((name, "skip", detail.reason))
            else:
                results.append((name, "pass", f"{detail} ({time.time() - t0:.1f}s)"))
        finally:
            runtime_mod.reset_state()

    print()
    for name, status, detail in results:
        print(f"[{status.upper()}] {name}: {detail}")
    passed = sum(1 for r in results if r[1] == "pass")
    failed = [r for r in results if r[1] == "fail"]
    skipped = sum(1 for r in results if r[1] == "skip")
    print(f"\n{passed} passed / {len(failed)} failed / {skipped} skipped")
    if failed:
        return 1
    if skipped:
        print("存在未验证项(SKIPPED):本次不算全过，退出码 2")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
