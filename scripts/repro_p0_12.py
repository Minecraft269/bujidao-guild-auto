#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
repro_p0_12.py —— 首启 UI 判定顺序验收（GOALS P0-12）
=====================================================
模式：
    lite-first-run    无 Gui.exe 目录跑 Lite 首启：日志不含「即将打开配置页」、含「按回车退出」，
                      进程在等（交互终端）；不出现 S8 等待。验证后结束进程。
    gui-no-exe        gui 变体放进无 Gui.exe 目录：同样停机（非交互直接退出 0），不进 S8。
    lite-ui           Lite 传 --ui：能力错误、退出码 1。
    gui-with-exe      有 Gui.exe 的目录跑 gui 变体首启：仍进配置页（P0-9b 回归），进程存活。
    console-s8        非首启控制台模式（ui.enabled=false）：S8 等待文案为「按 Ctrl+C 取消」，
                      且不含「点「停止」」。

用法：
    python scripts/repro_p0_12.py --exe <exe> --mode lite-first-run [--gui <Gui.exe>]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from guild_auto.config import ensure_default_config, load_config  # noqa: E402


def _print(msg: str) -> None:
    print(msg, flush=True)


def _seed(workdir: Path, *, created: bool, ui_enabled: bool = True, game_log: str | None = None):
    cfg_path = workdir / "config.json"
    if not created:
        ensure_default_config(str(cfg_path))
        cfg = load_config(str(cfg_path))
        cfg["ui"]["enabled"] = ui_enabled
        if game_log is not None:
            cfg["game_log"] = game_log
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg_path


def _launch(exe: Path, workdir: Path, tag: str) -> tuple[subprocess.Popen, Path, Path]:
    pid_file = workdir / f"pid-{tag}.txt"
    code_file = workdir / f"exit-{tag}.txt"
    script = (
        f"$p = Start-Process -FilePath '{exe}' -WorkingDirectory '{workdir}' -PassThru; "
        f"$p.Id | Out-File -Encoding ascii -FilePath '{pid_file}'; "
        f"$p.WaitForExit(); $p.ExitCode | Out-File -Encoding ascii -FilePath '{code_file}'"
    )
    ps = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return ps, pid_file, code_file


def _read_int(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="ascii", errors="replace").strip()
        return int(text) if text else None
    except (OSError, ValueError):
        return None


def _wait_for(path: Path, needle: str, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
            if needle in text:
                return text
        time.sleep(0.2)
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    out = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ 'YES' }} else {{ 'NO' }}",
        ],
        capture_output=True,
        text=True,
    )
    return "YES" in (out.stdout or "")


def _kill_tree(pid: int | None) -> None:
    if not pid:
        return
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)


def _log(workdir: Path) -> Path:
    return workdir / "logs" / "latest.log"


def _assert(cond: bool, msg: str) -> bool:
    _print(("OK: " if cond else "FAIL: ") + msg)
    return cond


def mode_lite_first_run(exe: Path, workdir: Path, timeout: float) -> int:
    _seed(workdir, created=True)
    _ps, pid_file, _code = _launch(exe, workdir, "lite")
    text = _wait_for(_log(workdir), "按回车退出", timeout)
    pid = _read_int(pid_file)
    ok = True
    ok &= _assert("即将打开配置页" not in text, "日志不含「即将打开配置页」")
    ok &= _assert("按回车退出" in text, "日志含「按回车退出」")
    ok &= _assert("S8 日志等待打开" not in text, "未进入 S8 等待")
    ok &= _assert(_alive(pid), f"进程仍在等（pid={pid}）")
    _kill_tree(pid)
    return 0 if ok else 1


def mode_gui_no_exe(exe: Path, workdir: Path, timeout: float) -> int:
    _seed(workdir, created=True)
    _ps, pid_file, code_file = _launch(exe, workdir, "gui")
    pid = _read_int(pid_file)
    deadline = time.monotonic() + timeout
    code = None
    while time.monotonic() < deadline:
        code = _read_int(code_file)
        if code is not None:
            break
        time.sleep(0.2)
    text = (
        _log(workdir).read_text(encoding="utf-8", errors="replace")
        if _log(workdir).exists()
        else ""
    )
    ok = True
    ok &= _assert("即将打开配置页" not in text, "日志不含「即将打开配置页」")
    ok &= _assert("未找到 Gui.exe" in text, "日志含「未找到 Gui.exe」")
    ok &= _assert("S8 日志等待打开" not in text, "未进入 S8 等待")
    ok &= _assert(code == 0, f"非交互安全退出（退出码={code}）")
    _kill_tree(pid)
    return 0 if ok else 1


def mode_lite_ui(exe: Path, workdir: Path, timeout: float) -> int:
    _seed(workdir, created=True)
    proc = subprocess.run(
        [str(exe), "--ui"],
        cwd=workdir,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    raw = proc.stdout or b""
    out = raw.decode("gbk", errors="replace") + "\n" + raw.decode("utf-8", errors="replace")
    ok = True
    ok &= _assert(proc.returncode == 1, f"退出码=1（实际 {proc.returncode}）")
    ok &= _assert("gui" in out, "输出含 gui 能力错误")
    _print("输出尾部: " + out[-200:].replace("\n", " | "))
    return 0 if ok else 1


def mode_gui_with_exe(exe: Path, workdir: Path, gui: Path | None, timeout: float) -> int:
    if gui is None or not gui.is_file():
        _print("FAIL: gui-with-exe 需要 --gui <Gui.exe>")
        return 1
    # 与 dist/ 的真实布局一致：核 exe 与 Gui.exe 必须同目录（binding 只搜 exe 同目录）
    local_exe = workdir / exe.name
    shutil.copy2(exe, local_exe)
    shutil.copy2(gui, workdir / gui.name)
    _seed(workdir, created=True)
    _ps, pid_file, _code = _launch(local_exe, workdir, "guiok")
    text = _wait_for(_log(workdir), "S4 决定=拉起 GUI", timeout)
    pid = _read_int(pid_file)
    ok = True
    ok &= _assert("即将打开配置页" in text, "日志含「即将打开配置页」")
    ok &= _assert("S4 决定=拉起 GUI" in text, "日志已进入 GUI 拉起阶段")
    ok &= _assert(_alive(pid), f"进程存活（pid={pid}）")
    _kill_tree(pid)
    return 0 if ok else 1


def mode_console_s8(exe: Path, workdir: Path, timeout: float) -> int:
    _seed(workdir, created=False, ui_enabled=False, game_log=str(workdir / "game.log"))
    proc = subprocess.Popen(
        [str(exe)],
        cwd=workdir,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    text = _wait_for(_log(workdir), "等待游戏日志", timeout)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    proc.kill()
    ok = True
    ok &= _assert("等待游戏日志" in text, "进入 S8 等待")
    ok &= _assert("按 Ctrl+C 取消" in text, "控制台词为「按 Ctrl+C 取消」")
    ok &= _assert("点「停止」" not in text, "控制台日志不含「点「停止」」")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P0-12 首启 UI 判定顺序验收")
    ap.add_argument("--exe", required=True, type=Path)
    ap.add_argument(
        "--mode",
        required=True,
        choices=("lite-first-run", "gui-no-exe", "lite-ui", "gui-with-exe", "console-s8"),
    )
    ap.add_argument("--gui", type=Path, default=None)
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args(argv)
    exe = args.exe.resolve()
    if not exe.is_file():
        _print(f"FAIL: 找不到 exe: {exe}")
        return 1
    with tempfile.TemporaryDirectory(prefix="p0_12_", ignore_cleanup_errors=True) as tmp:
        workdir = Path(tmp)
        _print(f"mode={args.mode} exe={exe.name}")
        return {
            "lite-first-run": lambda: mode_lite_first_run(exe, workdir, args.timeout),
            "gui-no-exe": lambda: mode_gui_no_exe(exe, workdir, args.timeout),
            "lite-ui": lambda: mode_lite_ui(exe, workdir, args.timeout),
            "gui-with-exe": lambda: mode_gui_with_exe(exe, workdir, args.gui, args.timeout),
            "console-s8": lambda: mode_console_s8(exe, workdir, args.timeout),
        }[args.mode]()


if __name__ == "__main__":
    raise SystemExit(main())
