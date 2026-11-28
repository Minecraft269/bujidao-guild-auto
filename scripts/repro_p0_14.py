#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""repro_p0_14.py —— 三种分发形态的启动压测（GOALS P0-14 第 4 条）

对同一个 exe 连续启动 N 次（`exe --help`：走完 PyInstaller 引导 + 解包 + Python
启动 + argparse，然后立即退出），统计：

  * 启动失败次数     —— 退出码非 0 或输出命中 `Failed to load Python DLL` / `PYI-`
                        / `LoadLibrary`（PyInstaller 引导程序的原始失败文案）；
  * 残留目录数       —— 压测前后 `--watch-dir` 下 `_MEI*` 目录的**新增**个数
                        （1/2 组看 TEMP 或 --runtime-tmpdir 目录；3 组 onedir 预期 0）；
  * 首次响应时间     —— 进程创建到第一行输出的耗时（含 onefile 解包），以及总耗时。

只读观测：不结束任何进程、不改任何系统设置、不清理被测残留（留着给诊断脚本复核）。

用法：
    python scripts/repro_p0_14.py --label "onefile+TEMP" --exe path\\a.exe --watch-dir %TEMP%
    python scripts/repro_p0_14.py --label "onedir" --exe path\\b.exe --watch-dir %TEMP%

退出码：0 = 全部成功；1 = 有失败（数字照常打印）。
"""

from __future__ import annotations

import argparse
import os
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

FAIL_MARKERS = ("Failed to load Python DLL", "LoadLibrary", "[PYI-")


def mei_dirs(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    return {p.name for p in root.iterdir() if p.name.startswith("_MEI") and p.is_dir()}


def launch(exe: Path, timeout: float) -> dict:
    """启动一次 `exe --help`，返回 {rc, first_s, total_s, output, ok}。"""
    started = time.monotonic()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [str(exe), "--help"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(exe.parent),
        env=env,
    )
    lines: list[str] = []
    first_at: list[float] = []

    def _read() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            if not first_at:
                first_at.append(time.monotonic() - started)
            lines.append(raw.decode("utf-8", errors="replace").rstrip())

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    timed_out = False
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        rc = proc.wait()
    reader.join(timeout=5)
    total = time.monotonic() - started
    output = "\n".join(lines)
    failed = timed_out or rc != 0 or any(marker in output for marker in FAIL_MARKERS)
    return {
        "rc": rc,
        "first_s": first_at[0] if first_at else total,
        "total_s": total,
        "output": output,
        "ok": not failed,
        "timed_out": timed_out,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="三种分发形态的启动压测（P0-14）")
    ap.add_argument("--label", required=True, help="形态名称，只用于输出")
    ap.add_argument("--exe", required=True, type=Path, help="被测 exe")
    ap.add_argument("--watch-dir", required=True, type=Path, help="统计 _MEI* 新增的目录")
    ap.add_argument("--launches", type=int, default=10, help="连续启动次数（默认 10，至少 1）")
    ap.add_argument("--timeout", type=float, default=120.0, help="单次启动超时秒数（默认 120）")
    args = ap.parse_args(argv)

    if not args.exe.is_file():
        print(f"exe 不存在: {args.exe}", file=sys.stderr)
        return 2
    if args.launches < 1:
        ap.error("--launches 必须 ≥ 1")
    if args.timeout <= 0:
        ap.error("--timeout 必须 > 0")
    before = mei_dirs(args.watch_dir)
    results = []
    for i in range(1, args.launches + 1):
        r = launch(args.exe, args.timeout)
        results.append(r)
        flag = "OK  " if r["ok"] else "FAIL"
        print(
            f"  #{i:<2} {flag} rc={r['rc']:<3} 首行={r['first_s']:.2f}s 总耗时={r['total_s']:.2f}s"
        )
        if not r["ok"]:
            for line in r["output"].splitlines()[:4]:
                print(f"        | {line}")
    after = mei_dirs(args.watch_dir)
    new_dirs = sorted(after - before)
    failures = [r for r in results if not r["ok"]]
    firsts = [r["first_s"] for r in results]
    totals = [r["total_s"] for r in results]
    extra = ",".join(new_dirs)
    print("\n" + "=" * 72)
    print(f"形态: {args.label}")
    print(f"exe : {args.exe}")
    print(f"启动失败次数: {len(failures)} / {args.launches}")
    print(f"残留目录数(新增): {len(new_dirs)}  {('(' + extra + ')') if extra else ''}")
    print(f"watch-dir 现有 _MEI*: {len(after)}")
    print(
        f"首次响应时间: 中位 {statistics.median(firsts):.2f}s "
        f"(最小 {min(firsts):.2f}s / 最大 {max(firsts):.2f}s)"
    )
    print(
        f"总耗时:       中位 {statistics.median(totals):.2f}s "
        f"(最小 {min(totals):.2f}s / 最大 {max(totals):.2f}s)"
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
