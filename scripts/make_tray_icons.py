#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 icon.png 生成托盘五档 PNG（P0-4b）。

背景：托盘一直只塞一张 16×16 的图，Windows 在 125%/150% DPI 下把它拉伸，
16px 的小图标糊成一团。改成「按 DPI 出五档、Rust 取最近档」：

    逻辑 16px × 100% / 125% / 150% / 200% / 300% → 16 / 20 / 24 / 32 / 48

只用 LANCZOS 从原图缩放，不手绘、不做剪影版/去文字版 —— 源图是唯一真源。
输出目录必须由参数显式给（本项目纪律第 6 条：源码里不得出现本机绝对路径）；
脚本会先清掉输出目录里已有的 `tray-*.png`，保证结果只有这五张。

    python scripts/make_tray_icons.py --out gui/src-tauri/icons
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image

#: 托盘图标五档物理边长（像素）。与 gui/src-tauri/src/main.rs 的 TRAY_SIZES 同源，
#: 改这里必须同步改那边，并由 tests/test_tray_icons.py 守卫。
SIZES: tuple[int, ...] = (16, 20, 24, 32, 48)

#: 仓库内默认源图（相对路径，不是本机绝对路径）。
DEFAULT_SRC = Path("gui/src-tauri/icons/icon.png")


def render(src: Path, out_dir: Path) -> list[Path]:
    """把 src 缩成 SIZES 五档写进 out_dir，返回写出的路径（顺序与 SIZES 一致）。"""
    with Image.open(src) as raw:
        base = raw.convert("RGBA")
    if base.size != (256, 256):
        print(f"警告：源图是 {base.size[0]}×{base.size[1]}，预期 256×256，仍按 LANCZOS 缩放")

    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in sorted(out_dir.glob("tray-*.png")):
        stale.unlink()

    written: list[Path] = []
    for size in SIZES:
        dst = out_dir / f"tray-{size}.png"
        base.resize((size, size), Image.Resampling.LANCZOS).save(dst)
        written.append(dst)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 icon.png 生成托盘五档 PNG（LANCZOS）")
    parser.add_argument(
        "--src", type=Path, default=DEFAULT_SRC, help="源图路径（默认 %(default)s）"
    )
    parser.add_argument("--out", type=Path, required=True, help="输出目录（必填）")
    args = parser.parse_args(argv)

    if not args.src.is_file():
        print(f"源图不存在：{args.src}", file=sys.stderr)
        return 1

    written = render(args.src, args.out)
    for size, path in zip(SIZES, written):
        print(f"{path.as_posix()}  {size}×{size}  {path.stat().st_size} 字节")
    return 0


if __name__ == "__main__":
    sys.exit(main())
