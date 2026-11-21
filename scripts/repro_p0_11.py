#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
repro_p0_11.py —— 启动期致命错误的可见反馈验收脚本（GOALS P0-11）
=================================================================
场景固定为「非首启 + game_log 占位符」：目录里已有 config.json（默认值就是占位符），
启动后 validate_config 报 error，prepare_config 走 exit(1)。

三种期望：
    --expect dialog   双击等价启动（Start-Process -PassThru，无标准句柄）→ 必须出现可见
                      MessageBox，关闭后退出码仍为 1；
    --expect none     无弹窗（旧代码对照 / 非交互场景）→ 不得出现弹窗且进程自行退出 1；
    --expect console  纯 CLI 变体 → stdout 里有「[错误]」与 game_log，退出码 1，且无弹窗。

用法：
    python scripts/repro_p0_11.py --exe <exe路径> --expect dialog
"""

from __future__ import annotations

import argparse
import ctypes
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
sys.path.insert(0, str(SRC))

from guild_auto.config import ensure_default_config  # noqa: E402

DIALOG_KEYWORD = "启动失败"
WM_CLOSE = 0x0010


def _seed_placeholder_config(workdir: Path) -> Path:
    """写一份出厂默认配置（game_log 即占位符）+ 已存在 ⇒ created=False。"""
    cfg_path = workdir / "config.json"
    ensure_default_config(str(cfg_path))
    assert cfg_path.exists(), "未能生成 config.json"
    return cfg_path


def _find_dialog_window() -> tuple[int, int] | None:
    """枚举顶层可见窗口，找标题含「启动失败」的 MessageBox，返回 (hwnd, pid)。"""
    user32 = ctypes.windll.user32
    found: list[tuple[int, int]] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        if DIALOG_KEYWORD in buf.value:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.append((int(hwnd), int(pid.value)))
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return found[0] if found else None


def _start_double_click(exe: Path, workdir: Path, code_file: Path) -> subprocess.Popen:
    """用 Start-Process -PassThru 启动 = 双击等价：不继承标准句柄，且能拿到退出码。

    Start-Process 默认走 ShellExecute（UseShellExecute=true），因此子进程
    stdin/stdout/stderr 全为 NULL；`-PassThru` 的 Process 对象在 WaitForExit
    后可读 ExitCode（PyInstaller 单文件引导进程会透传子进程退出码）。
    """
    script = (
        f"$p = Start-Process -FilePath '{exe}' -WorkingDirectory '{workdir}' -PassThru; "
        f"$p.WaitForExit(); $p.ExitCode | Out-File -Encoding ascii -FilePath '{code_file}'"
    )
    return subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _read_exit_code(code_file: Path, timeout: float) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if code_file.exists():
            text = code_file.read_text(encoding="ascii", errors="replace").strip()
            if text:
                try:
                    return int(text)
                except ValueError:
                    return None
        time.sleep(0.2)
    return None


def _print(msg: str) -> None:
    print(msg, flush=True)


def run_dialog(exe: Path, workdir: Path, timeout: float) -> int:
    code_file = workdir / "exitcode.txt"
    launcher = _start_double_click(exe, workdir, code_file)
    deadline = time.monotonic() + timeout
    hit = None
    while time.monotonic() < deadline:
        hit = _find_dialog_window()
        if hit:
            break
        time.sleep(0.2)
    if not hit:
        _print(f"FAIL: {timeout:.0f}s 内未出现可见的「{DIALOG_KEYWORD}」弹窗")
        launcher.kill()
        return 1
    hwnd, pid = hit
    _print(f"OK: 找到可见弹窗 hwnd={hwnd} pid={pid}")
    ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    code = _read_exit_code(code_file, timeout)
    try:
        launcher.wait(timeout=10)
    except subprocess.TimeoutExpired:
        launcher.kill()
    if code != 1:
        _print(f"FAIL: 弹窗关闭后退出码={code}（期望 1）")
        return 1
    _print("OK: 关闭弹窗后进程退出码=1")
    return 0


def run_none(exe: Path, workdir: Path, timeout: float) -> int:
    proc = subprocess.Popen(
        [str(exe)],
        cwd=workdir,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + timeout
    hit = None
    while time.monotonic() < deadline:
        hit = _find_dialog_window()
        if hit or proc.poll() is not None:
            break
        time.sleep(0.2)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _print("FAIL: 进程超时未退出（疑似在等弹窗）")
        proc.kill()
        return 1
    if hit:
        _print(f"FAIL: 不该弹窗却出现了弹窗 hwnd={hit[0]}（重定向句柄场景不得阻塞）")
        return 1
    if proc.returncode != 1:
        _print(f"FAIL: 退出码={proc.returncode}（期望 1）")
        return 1
    _print("OK: 无弹窗，进程自行退出，退出码=1")
    return 0


def _decode_console(raw: bytes) -> str:
    """控制台输出可能混合两种编码：普通行走控制台代码页(GBK)，
    UnicodeEncodeError 的兜底行走 UTF-8。两种都解一遍再拼起来断言。"""
    return raw.decode("gbk", errors="replace") + "\n" + raw.decode("utf-8", errors="replace")


def run_console(exe: Path, workdir: Path, timeout: float) -> int:
    proc = subprocess.run(
        [str(exe)],
        cwd=workdir,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    out = _decode_console(proc.stdout or b"")
    hit = _find_dialog_window()
    if hit:
        _print(f"FAIL: 纯 CLI 变体弹了窗 hwnd={hit[0]}")
        return 1
    if proc.returncode != 1:
        _print(f"FAIL: 退出码={proc.returncode}（期望 1）")
        return 1
    missing = [n for n in ("[错误]", "game_log") if n not in out]
    if missing:
        _print(f"FAIL: 控制台输出缺少 {missing}；实际输出尾部：{out[-300:]}")
        return 1
    _print("OK: 纯 CLI 无弹窗、控制台含 [错误] 与 game_log、退出码=1")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P0-11 启动期错误可见反馈验收")
    ap.add_argument("--exe", required=True, type=Path)
    ap.add_argument("--expect", required=True, choices=("dialog", "none", "console"))
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args(argv)

    exe = args.exe.resolve()
    if not exe.is_file():
        _print(f"FAIL: 找不到 exe: {exe}")
        return 1
    with tempfile.TemporaryDirectory(prefix="p0_11_", ignore_cleanup_errors=True) as tmp:
        workdir = Path(tmp)
        _seed_placeholder_config(workdir)
        _print(f"场景: 非首启 + game_log 占位符 | exe={exe.name} | expect={args.expect}")
        handler = {"dialog": run_dialog, "none": run_none, "console": run_console}[args.expect]
        return handler(exe, workdir, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
