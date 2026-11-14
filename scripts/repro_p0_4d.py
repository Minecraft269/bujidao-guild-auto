# -*- coding: utf-8 -*-
"""P0-4d 复现/回归脚本：首启锁配置页时，S5 不该把界面一起带走。

背景：`first_run=true` 时 GUI 锁在配置页、其它标签全灰，用户**点不到**「开始」。
旧逻辑 S5 照样等满 `ui.start_timeout`（默认 120s），超时 → 核退出 → 推 ipc-closed
→ Rust 1.5s 后 `app.exit` → **窗口自己消失**。本案要求首启不启动计时。

用法（脚本自己起进程、自己数秒、自己查 tasklist，不需要人盯屏幕）::

    python scripts/repro_p0_4d.py                            # 首启：静置 180s，必须仍存活
    python scripts/repro_p0_4d.py --wait 30                  # 回归烟测（缩短等待）
    python scripts/repro_p0_4d.py --timeout 10 --wait 30     # 非首启：10s 应触发超时

退出码：0 = 符合预期，1 = 不符合。
"""

from __future__ import annotations

import argparse
import copy
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from guild_auto.config import save_config  # noqa: E402
from guild_auto.config.defaults import DEFAULT_CONFIG  # noqa: E402

KEY_PATTERNS = ("S4 UI 入口", "S5 ", "S6 已通知", "ipc-closed", "正常退出", "首次运行")


def _tasklist(pid: int) -> str:
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout.strip()
    return out or "<无输出>"


def _read_log(work: Path) -> str:
    log_file = work / "logs" / "latest.log"
    if not log_file.exists():
        return ""
    try:
        return log_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _key_lines(log_text: str) -> list[str]:
    hits = [ln.strip() for ln in log_text.splitlines() if any(p in ln for p in KEY_PATTERNS)]
    return hits[-14:] or ["<没有命中的关键日志行>"]


def _gui_log_tail(work: Path) -> list[str]:
    """界面侧日志。超时路径故意不合并（界面还在写），所以它留在 gui.log 里。"""
    gui_log = work / "logs" / "gui.log"
    if not gui_log.exists():
        return ["<无 gui.log（已合并或未生成）>"]
    lines = gui_log.read_text(encoding="utf-8", errors="replace").splitlines()
    hits = [ln.strip() for ln in lines if "ipc-closed" in ln or "IPC 已关闭" in ln]
    return (hits or lines)[-6:]


def main() -> int:
    ap = argparse.ArgumentParser(description="P0-4d：首启 S5 不启动计时的复现脚本")
    ap.add_argument("--wait", type=float, default=180.0, help="静置秒数（默认 180）")
    ap.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="预置一份已存在的 config 并置 ui.start_timeout=该值（=非首启，验证可配生效）",
    )
    args = ap.parse_args()

    with tempfile.TemporaryDirectory(prefix="p0_4d_") as tmp:
        work = Path(tmp)
        cfg_path = work / "config.json"
        first_run = args.timeout is None
        if not first_run:
            cfg = copy.deepcopy(DEFAULT_CONFIG)
            cfg["ui"]["start_timeout"] = args.timeout
            save_config(cfg_path, cfg)

        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO / "src")
        env["PYTHONIOENCODING"] = "utf-8"
        cmd = [sys.executable, "-m", "guild_auto.main", "--config", str(cfg_path), "--ui"]
        print(f"[repro] first_run={first_run} 静置={args.wait:.0f}s", flush=True)
        if not first_run:
            print(f"[repro] 预置 ui.start_timeout={args.timeout}s（非首启）", flush=True)
        print(f"[repro] 启动: {' '.join(cmd)}", flush=True)

        started = time.monotonic()
        proc = subprocess.Popen(
            cmd, cwd=str(work), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        gui_pid: int | None = None
        try:
            deadline = started + args.wait
            while time.monotonic() < deadline:
                time.sleep(0.5)
                log_text = _read_log(work)
                if gui_pid is None:
                    m = re.search(r"S5 Gui\.exe 已拉起 pid=(\d+)", log_text)
                    if m:
                        gui_pid = int(m.group(1))
                if proc.poll() is not None:
                    break
            elapsed = time.monotonic() - started
            log_text = _read_log(work)
            core_alive = proc.poll() is None
            gui_alive = gui_pid is not None and "Gui.exe" in _tasklist(gui_pid)
            timed_out = "S5 等待 GUI「开始」失败" in log_text

            print(f"\n[repro] 实测经过 {elapsed:.1f}s（等待上限 {args.wait:.0f}s）", flush=True)
            print(f"[repro] 核进程 PID={proc.pid} → {_tasklist(proc.pid)}", flush=True)
            if gui_pid is not None:
                print(f"[repro] 界面 PID={gui_pid} → {_tasklist(gui_pid)}", flush=True)
            print("\n[repro] 关键日志行:", flush=True)
            for line in _key_lines(log_text):
                print("  " + line, flush=True)
            print("[repro] 界面侧日志（gui.log）:", flush=True)
            for line in _gui_log_tail(work):
                print("  " + line, flush=True)

            # 期望值：首启永不计时；非首启只有静置时间 ≥ 配置超时才该触发。
            if first_run:
                expect_timeout, expect_core_alive = False, True
            elif args.wait >= args.timeout:
                expect_timeout, expect_core_alive = True, False
            else:
                expect_timeout, expect_core_alive = False, True

            # 三种情况界面都必须留着（P0-4d 的核心诉求：超时不带走窗口）
            ok = timed_out == expect_timeout and core_alive == expect_core_alive and gui_alive
            verdict = (
                "通过"
                if ok
                else f"失败（期望 超时={expect_timeout} 核存活={expect_core_alive} 界面存活=True）"
            )
            print(
                f"\n[repro] 结论: 核={'存活' if core_alive else '已退出'} "
                f"界面={'存活' if gui_alive else '已退出'} "
                f"S5超时={'有' if timed_out else '无'} 实测={elapsed:.1f}s → {verdict}",
                flush=True,
            )
            return 0 if ok else 1
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if gui_pid is not None:
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(gui_pid)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )


if __name__ == "__main__":
    sys.exit(main())
