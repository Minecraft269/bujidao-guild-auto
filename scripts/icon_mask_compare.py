# -*- coding: utf-8 -*-
"""遮罩图标对比图（§4.4 自查）：16px 实际 / 3× 放大 / 参考图并排。

为什么不用浏览器：CSS mask 里的 file:// 位图在 setContent 页面会被拦，
渲染出来是空白（已实测两次）。遮罩本身就是位图，用 PIL 合成既保真又不依赖浏览器。

参考图目录必须由 `--ref` 显式给出（或 ICON_REF_DIR 环境变量）；
**不设任何默认路径** —— 本机路径不入库是纪律第 6 条。
没给参考图时只画「实际 + 3× 放大」两列。

用法：
    python scripts/icon_mask_compare.py --ref <参考图目录> [--out <png>]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ICONS = ROOT / "gui" / "src" / "icons"
OUT = ROOT / "docs" / "images" / "icon-mask-compare.png"

BG = (30, 31, 34, 255)
BOX = (43, 45, 49, 255)
FG = (230, 230, 230, 255)
PAD = 14
SCALE3 = 3
ROWS = (("compact", "小窗"), ("expand", "大窗"))


def tint(mask: Image.Image) -> Image.Image:
    """按前景色着色，模拟 CSS 的 background-color: currentColor。"""
    alpha = mask.convert("RGBA").split()[3]
    out = Image.new("RGBA", mask.size, FG)
    out.putalpha(alpha)
    return out


def on_box(icon: Image.Image, box: int) -> Image.Image:
    canvas = Image.new("RGBA", (box, box), BOX)
    canvas.alpha_composite(icon, ((box - icon.width) // 2, (box - icon.height) // 2))
    return canvas


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--ref",
        type=Path,
        default=Path(os.environ["ICON_REF_DIR"]) if os.environ.get("ICON_REF_DIR") else None,
        help="参考图目录（含 小窗_preview.png / 大窗_preview.png）；不给则只画两列",
    )
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)

    cells: list[list[Image.Image]] = []
    for key, label in ROWS:
        mask_path = ICONS / f"{key}_16.png"
        if not mask_path.exists():
            print(f"缺少遮罩：{mask_path}", file=sys.stderr)
            return 2
        actual = tint(Image.open(mask_path))
        big = actual.resize((actual.width * SCALE3, actual.height * SCALE3), Image.NEAREST)
        row = [on_box(actual, 32), on_box(big, 64)]
        if args.ref is not None:
            ref = args.ref / f"{label}_preview.png"
            if ref.exists():
                row.append(Image.open(ref).convert("RGBA").resize((64, 64), Image.NEAREST))
        cells.append(row)

    cols = max(len(r) for r in cells)
    cell_w, cell_h = 104, 104
    width = PAD * 2 + cell_w * (cols + 1)
    height = PAD * 2 + cell_h * len(cells) + 20
    img = Image.new("RGBA", (width, height), BG)
    x0 = PAD + cell_w
    for i, row in enumerate(cells):
        y = PAD + 10 + i * cell_h
        for j, cell in enumerate(row):
            img.alpha_composite(cell, (x0 + j * cell_w + (cell_w - cell.width) // 2, y))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    img.save(args.out)
    print(f"已生成 {args.out}（{width}×{height}，{cols} 列）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
