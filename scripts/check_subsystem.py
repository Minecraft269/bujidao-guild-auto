# -*- coding: utf-8 -*-
"""断言 exe 的 Windows 子系统（PE 可选头 Subsystem 字段）。

为什么需要：gui 变体双击后不该弹控制台窗口，但那取决于 PyInstaller 是否带了
`--noconsole`，产物本身看不出差别。这里直接读 PE 头，把「看起来对」变成「可断言」。

用法：
    python scripts/check_subsystem.py --exe dist/xxx.exe --expect gui
    python scripts/check_subsystem.py --exe dist/xxx.exe --expect console

Subsystem 取值：2 = WINDOWS_GUI（无控制台），3 = WINDOWS_CUI（有控制台）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# IMAGE_SUBSYSTEM_WINDOWS_GUI / IMAGE_SUBSYSTEM_WINDOWS_CUI
SUBSYSTEM_NAMES = {2: "gui", 3: "console"}


def _ensure_utf8_stdout() -> None:
    """CI 的 Windows 控制台默认 cp1252，打印中文会 UnicodeEncodeError。

    本项目已踩过两次（package.py、gui_snapshot.py），这是第三次：
    断言逻辑没问题，是 print 把整个步骤判成失败。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            try:
                reconfigure(encoding="ascii", errors="replace")
            except (ValueError, OSError):
                pass


def read_subsystem(path: Path) -> int:
    """读 PE 可选头里的 Subsystem（uint16）。文件不对就抛 ValueError。"""
    with path.open("rb") as f:
        head = f.read(0x40)
        if len(head) < 0x40 or head[:2] != b"MZ":
            raise ValueError(f"不是 PE 文件（缺 MZ 头）: {path}")
        offset = int.from_bytes(head[0x3C:0x40], "little")
        f.seek(offset)
        if f.read(4) != b"PE\0\0":
            raise ValueError(f"不是 PE 文件（缺 PE 签名）: {path}")
        # 4 字节签名 + 20 字节 COFF 头之后是可选头；Subsystem 在可选头偏移 0x44
        f.seek(offset + 4 + 20 + 0x44)
        raw = f.read(2)
        if len(raw) != 2:
            raise ValueError(f"PE 可选头不完整: {path}")
        return int.from_bytes(raw, "little")


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True, type=Path)
    ap.add_argument("--expect", required=True, choices=("gui", "console"))
    args = ap.parse_args(argv)

    if not args.exe.exists():
        print(f"找不到 exe：{args.exe}", file=sys.stderr)
        return 2
    value = read_subsystem(args.exe)
    actual = SUBSYSTEM_NAMES.get(value, f"unknown({value})")
    if actual != args.expect:
        print(
            f"子系统不符：{args.exe.name} 期望 {args.expect}，实际 {actual}"
            f"（Subsystem={value}）。gui 变体打包需加 --noconsole。",
            file=sys.stderr,
        )
        return 1
    print(f"子系统正确：{args.exe.name} = {actual}（Subsystem={value}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
