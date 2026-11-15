# -*- coding: utf-8 -*-
"""托盘五档图标的守卫（P0-4b）。

三处真源必须同步，任何一处漂移都会让托盘重新变糊或直接透明：
  · scripts/make_tray_icons.py 的 SIZES（谁生成）
  · gui/src-tauri/src/main.rs 的 TRAY_SIZES（谁按 DPI 选）
  · gui/src-tauri/icons/ 里实际落盘的 tray-NN.png（谁被 include_bytes! 编进去）
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from PIL import Image

REPO = Path(__file__).resolve().parents[1]
ICONS = REPO / "gui" / "src-tauri" / "icons"
MAIN_RS = REPO / "gui" / "src-tauri" / "src" / "main.rs"
SCRIPTS = REPO / "scripts"

EXPECTED_SIZES = (16, 20, 24, 32, 48)


def _script_module():
    """把 scripts/ 挂上 import 路径再取模块（scripts/ 不是包）。"""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import make_tray_icons  # noqa: PLC0415  (路径必须先插)

    return make_tray_icons


def test_icons_dir_has_exactly_the_five_tiers():
    """icons/ 里 tray-*.png 只能是这五张，旧的 idle/running/paused/failed 全部作废。"""
    names = sorted(p.name for p in ICONS.glob("tray-*.png"))
    assert names == [f"tray-{s}.png" for s in EXPECTED_SIZES]


def test_each_tier_has_matching_pixel_size():
    """每档的像素尺寸必须等于文件名里的数字，否则 include_bytes! 里的图会被拉伸。"""
    for size in EXPECTED_SIZES:
        with Image.open(ICONS / f"tray-{size}.png") as im:
            assert im.size == (size, size), f"tray-{size}.png 实际是 {im.size}"
            assert im.mode == "RGBA", f"tray-{size}.png 模式是 {im.mode}"


def test_script_sizes_match_rust_sizes():
    """生成端的 SIZES 与 Rust 的 TRAY_SIZES 是同一组数，写死两边就必须有测试焊住。"""
    script = _script_module()
    assert tuple(script.SIZES) == EXPECTED_SIZES

    text = MAIN_RS.read_text(encoding="utf-8")
    m = re.search(r"const TRAY_SIZES: \[u32; \d+\] = \[([^\]]+)\];", text)
    assert m, "main.rs 缺 TRAY_SIZES 常量"
    assert tuple(int(x) for x in m.group(1).split(",") if x.strip()) == EXPECTED_SIZES


def test_rust_no_longer_references_old_tray_icons():
    """四态/计数图已删，源码里再出现任何一条都是会 404 的死引用。"""
    text = MAIN_RS.read_text(encoding="utf-8")
    for old in ("tray-idle", "tray-running", "tray-paused", "tray-failed"):
        assert old not in text, f"main.rs 仍引用 {old}"


def test_render_outputs_only_the_five_and_cleans_stale(tmp_path):
    """脚本跑完目录里只剩五张：旧的 tray-failed-3.png 必须被清掉。"""
    script = _script_module()
    stale = tmp_path / "tray-failed-3.png"
    stale.write_bytes(b"not a real png")

    written = script.render(ICONS / "icon.png", tmp_path)

    assert [p.name for p in written] == [f"tray-{s}.png" for s in EXPECTED_SIZES]
    assert sorted(p.name for p in tmp_path.glob("*.png")) == [
        f"tray-{s}.png" for s in EXPECTED_SIZES
    ]
    for size in EXPECTED_SIZES:
        with Image.open(tmp_path / f"tray-{size}.png") as im:
            assert im.size == (size, size)


def test_source_icon_untouched():
    """icon.png 是唯一真源，必须还是 256×256（禁改本体）。"""
    with Image.open(ICONS / "icon.png") as im:
        assert im.size == (256, 256)


def test_cli_requires_out_dir(capsys):
    """输出目录必须显式给：不许脚本自己猜本机路径（纪律第 6 条）。"""
    script = _script_module()
    with pytest.raises(SystemExit) as exc:
        script.main(["--src", str(ICONS / "icon.png")])
    assert exc.value.code != 0
    assert "--out" in capsys.readouterr().err
