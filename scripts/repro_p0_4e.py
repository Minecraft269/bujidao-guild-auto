# -*- coding: utf-8 -*-
"""P0-4e 复现/回归脚本：RPC 写被读阻塞 + 核无响应时的界面反馈。

验两件事（都不靠人盯屏幕，**也不碰鼠标/不抢前台**）：
  1. 冷启动后第一次 RPC 的往返耗时 —— 旧实现里 `BufReader::read_line` 会挂住
     同一个文件对象，把后续 WriteFile 排队，首发请求要等满 15s 才超时；
  2. 核**不应答** pause 时，关窗模态是否**立刻**出现（旧实现要等满 15s 才建模态，
     用户看到的就是「第一次点 × 没反应，点第二次才弹」）。

做法：本进程起一个真 `IpcServer` 当假核（`pause` 故意永不返回），
再把真 `Gui.exe` 拉起来接这个管道，然后用 `PostMessage(WM_CLOSE)` 触发关闭请求
—— 这是窗口消息，不动光标、不抢焦点。

「核死了要不要停倒数」不由本脚本自动化：那需要真的点「开始」按钮，属界面操作，
按协议 G 归用户手动验证（脚本这边已用 ipc-closed 证明前端会执行 abort）。

用法::

    python scripts/repro_p0_4e.py

退出码：0 = 符合预期，1 = 不符合。
"""

from __future__ import annotations

import argparse
import ctypes
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from guild_auto.ui.ipc import IpcServer  # noqa: E402

GUI_EXE = REPO / "gui" / "src-tauri" / "target" / "release" / "Gui.exe"
FIRST_RPC_BUDGET_MS = 1000  # 冷启动首次 RPC 的验收上限
MODAL_BUDGET_S = 1.0  # 点了 × 之后模态必须出现的上限
MODAL_LINE = "关窗请求：运行中，先暂停"


def _ts(line: str) -> float | None:
    """从 gui.log 行里取相对时间戳 [+12.345s]。"""
    m = re.match(r"\[\+(\d+\.\d+)s\]", line.strip())
    return float(m.group(1)) if m else None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _find_window(title_part: str) -> int:
    user32 = ctypes.windll.user32
    found = ctypes.c_void_p(0)

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _cb(hwnd, _lparam):
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        if title_part in buf.value and user32.IsWindowVisible(hwnd):
            found.value = hwnd
            return False
        return True

    user32.EnumWindows(_cb, 0)
    return found.value or 0


def main() -> int:
    ap = argparse.ArgumentParser(description="P0-4e：RPC 阻塞与核无响应时的界面反馈")
    ap.add_argument(
        "--abort-only",
        action="store_true",
        help="只验「核退出 → 前端立刻收掉依赖核的界面动作」（不需要 state=running）",
    )
    args = ap.parse_args()

    if not GUI_EXE.exists():
        print(f"[repro] 找不到 {GUI_EXE}（先 cargo build --release）", flush=True)
        return 1

    with tempfile.TemporaryDirectory(prefix="p0_4e_") as tmp:
        work = Path(tmp)
        (work / "logs").mkdir()
        gui_log = work / "logs" / "gui.log"

        # ── 假核：pause 永不返回，模拟「核无响应」 ──────────────────────
        srv = IpcServer({})
        srv.pipe_name = "bujidao-" + srv.token
        srv.path = "\\\\.\\pipe\\" + srv.pipe_name
        release = threading.Event()

        def _hang(_params):
            release.wait(60)  # 故意挂住：界面上不该被它卡住
            raise TimeoutError("假核：pause 永不返回")

        srv._handlers.update(
            {
                "hello": lambda p: {
                    "variant": "Lite-Async-Gui-Vision",
                    "capabilities": ["core", "async", "gui", "vision"],
                    "plugin_version": "1.0.0",
                    "config_schema_version": 1,
                    "first_run": False,
                    "log_level": 6,
                    "start_mode": "normal",
                },
                "config": lambda p: {"fields": []},
                "rules": lambda p: {},
                "save_config": lambda p: {},
                "state": lambda p: {
                    "state": "running",
                    "stage": "执行中",
                    "done": 1,
                    "total": 5,
                    "elapsed_ms": 1000,
                    "player": "合成玩家A",
                    "pause_reason": "",
                },
                "gui_log": lambda p: {},
                "pause": _hang,
            }
        )
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        proc = subprocess.Popen(
            [str(GUI_EXE), "--pipe", srv.pipe_name, "--token", srv.token],
            cwd=str(work),
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if "已连接核心" in _read(gui_log):
                    break
                time.sleep(0.2)
            time.sleep(2.5)  # 让 boot 那串 RPC（config/rules/state）跑完

            hwnd = _find_window("布吉岛公会管理脚本")
            if not hwnd:
                print("[repro] 找不到 Gui 窗口", flush=True)
                return 1

            if args.abort_only:
                # 核退出 → 前端必须立刻收掉依赖核的界面动作（P0-4e 目标 3 的界面半边）
                print("[repro] 模式=核退出后中止界面动作", flush=True)
                srv.send_event("ipc-closed", {"pid": 0, "reason": "normal"})
                abort_ts = None
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    hit = [ln for ln in _read(gui_log).splitlines() if "界面动作已中止" in ln]
                    if hit:
                        abort_ts = _ts(hit[-1])
                        break
                    time.sleep(0.05)
                closed_ts = None
                hits = [ln for ln in _read(gui_log).splitlines() if "IPC 已关闭" in ln]
                if hits:
                    closed_ts = _ts(hits[-1])
                delta = (
                    round(abort_ts - closed_ts, 3)
                    if (abort_ts is not None and closed_ts is not None)
                    else None
                )
                print(f"[repro] 前端「IPC 已关闭」t={closed_ts}s", flush=True)
                print(f"[repro] 前端「界面动作已中止」t={abort_ts}s 延迟={delta}s", flush=True)
                for ln in _read(gui_log).splitlines():
                    if "界面动作已中止" in ln or "IPC 已关闭" in ln:
                        print("  " + ln.strip(), flush=True)
                ok = delta is not None and delta <= MODAL_BUDGET_S
                print(f"\n[repro] 结论: 中止延迟={delta}s → {'通过' if ok else '失败'}", flush=True)
                return 0 if ok else 1

            # 前端不把 rpc("state") 的返回值写进 S.state（只认事件推送），
            # 所以必须推一条 state 事件，否则关窗时 S.state.state 仍是 idle
            srv.send_event(
                "state",
                {
                    "state": "running",
                    "stage": "执行中",
                    "done": 1,
                    "total": 5,
                    "elapsed_ms": 1000,
                    "player": "合成玩家A",
                    "pause_reason": "",
                },
            )
            time.sleep(0.6)

            text = _read(gui_log)
            elapsed = [int(m) for m in re.findall(r"RPC method=\S+ id=\d+ 耗时=(\d+)ms", text)]
            pipe_nowait_failed = "设置 PIPE_NOWAIT 失败" in text
            first = elapsed[0] if elapsed else None

            print(f"[repro] 冷启动 RPC 次数={len(elapsed)} 各次耗时(ms)={elapsed}", flush=True)
            print(
                f"[repro] 首次 RPC 耗时={first}ms（验收 <{FIRST_RPC_BUDGET_MS}ms） "
                f"最大={max(elapsed) if elapsed else None}ms",
                flush=True,
            )
            print(
                f"[repro] PIPE_NOWAIT 失败日志="
                f"{'有（复现到旧根因）' if pipe_nowait_failed else '无（已消失）'}",
                flush=True,
            )

            # ── 点 ×：PostMessage(WM_CLOSE)，量模态出现时间（不动光标/不抢焦点）──
            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE

            modal_ts = None
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                hit = [ln for ln in _read(gui_log).splitlines() if MODAL_LINE in ln]
                if hit:
                    modal_ts = _ts(hit[-1])
                    break
                time.sleep(0.1)

            rust_lines = [ln for ln in _read(gui_log).splitlines() if "关闭动作=询问前端" in ln]
            rust_ts = _ts(rust_lines[-1]) if rust_lines else None
            delta = (
                round(modal_ts - rust_ts, 3)
                if (modal_ts is not None and rust_ts is not None)
                else None
            )
            print(f"[repro] Rust「关闭动作=询问前端」t={rust_ts}s", flush=True)
            print(f"[repro] 前端「{MODAL_LINE}」t={modal_ts}s", flush=True)
            print(
                f"[repro] 模态出现延迟={delta}s（验收 ≤{MODAL_BUDGET_S}s，pause 至今未应答）",
                flush=True,
            )
            for ln in _read(gui_log).splitlines():
                if MODAL_LINE in ln or "正在暂停" in ln or "暂停失败" in ln:
                    print("  " + ln.strip(), flush=True)

            ok = (
                first is not None
                and first < FIRST_RPC_BUDGET_MS
                and not pipe_nowait_failed
                and delta is not None
                and delta <= MODAL_BUDGET_S
            )
            print(
                f"\n[repro] 结论: 首次RPC={first}ms "
                f"PIPE_NOWAIT失败={'是' if pipe_nowait_failed else '否'} "
                f"模态延迟={delta}s → {'通过' if ok else '失败'}",
                flush=True,
            )
            return 0 if ok else 1
        finally:
            release.set()
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            srv.stop()


if __name__ == "__main__":
    sys.exit(main())
