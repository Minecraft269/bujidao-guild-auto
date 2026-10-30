# -*- coding: utf-8 -*-
"""package.py —— 组装发布产物（GUI设计规范 §9 / §9.1）。

产物（全部落在 dist/）：
    布吉岛公会管理脚本-<拼接名>[-Full].exe   核（唯一入口）
    Gui.exe                                  4 个含 gui 的变体共用**同一份**
    启动.cmd                                 便携包入口，指向核 exe
    latest.json                              自动更新清单（按变体分列）
    <名字>.zip                               便携包

为什么 Gui.exe 只放一份：4 个含 gui 变体共用一份界面（规范前置约束），
按变体复制 4 份既浪费体积，又会让版本漂移。

用法：
    python scripts/package.py --core <核exe> --gui <Gui.exe> --variant Lite-Async-Gui-Vision
                              [--full-suffix -Full] [--out dist] [--skip-zip]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import zipfile
from datetime import date
from pathlib import Path

LAUNCH_CMD = """@echo off
rem 便携包入口：只指向核 exe（Gui.exe 不单独给快捷方式，§9）
setlocal
cd /d "%~dp0"
start "" "{core_name}"
endlocal
"""


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_latest_json(variant: str, version: str, zip_name: str, zip_path: Path) -> dict:
    """自动更新清单：按变体分列；签名位留空，由发布流程填（本轮不生成密钥）。"""
    return {
        "version": version,
        "variants": {
            variant: {
                "url": zip_name,
                "sha256": sha256_of(zip_path) if zip_path.exists() else "",
                "signature": "",
                "released": date.today().isoformat(),
            }
        },
    }


def _ensure_utf8_stdout() -> None:
    """CI 的 Windows 控制台默认 cp1252/cp437，打印中文（文件名/提示）会 UnicodeEncodeError。

    CI 实测：run 41049130275 的 4 个含 gui 变体全挂在这里 —— 产物其实都生成好了，
    只是 print 中文时抛异常把步骤判成失败。与 scripts/gui_snapshot.py 同款守卫；
    刻意各留一份，避免脚本之间互相 import（发布脚本要能单文件跑）。
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


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", required=True, type=Path, help="核 exe 路径")
    ap.add_argument("--gui", required=True, type=Path, help="Gui.exe 路径")
    ap.add_argument("--variant", required=True, help="拼接名，如 Lite-Async-Gui-Vision")
    ap.add_argument("--full-suffix", default="", help="全能力变体的 -Full 后缀")
    ap.add_argument("--version", default="2.0.0")
    ap.add_argument("--out", default="dist", type=Path)
    ap.add_argument("--msi", type=Path, default=None, help="随包附带的 MSI（可选）")
    ap.add_argument("--skip-zip", action="store_true")
    args = ap.parse_args(argv)

    if not args.core.exists():
        print(f"找不到核 exe：{args.core}", file=sys.stderr)
        return 2
    if not args.gui.exists():
        print(f"找不到 Gui.exe：{args.gui}", file=sys.stderr)
        return 2

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    stem = f"布吉岛公会管理脚本-{args.variant}{args.full_suffix}"
    core_name = f"{stem}.exe"
    core_dst = out / core_name
    gui_dst = out / "Gui.exe"
    if args.core.resolve() != core_dst.resolve():
        shutil.copy2(args.core, core_dst)
    if args.gui.resolve() != gui_dst.resolve():
        shutil.copy2(args.gui, gui_dst)
    msi_dst = None
    if args.msi is not None:
        if not args.msi.exists():
            print(f"找不到 MSI：{args.msi}", file=sys.stderr)
            return 2
        msi_dst = out / args.msi.name
        if args.msi.resolve() != msi_dst.resolve():
            shutil.copy2(args.msi, msi_dst)

    # .cmd 用 GBK：cmd.exe 在中文 Windows 上按 OEM 代码页读批处理
    cmd_path = out / "启动.cmd"
    cmd_path.write_bytes(LAUNCH_CMD.format(core_name=core_name).encode("gbk", "replace"))

    zip_path = out / f"{stem}.zip"
    if not args.skip_zip:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in (core_dst, gui_dst, cmd_path):
                zf.write(p, p.name)
            if msi_dst is not None:
                zf.write(msi_dst, msi_dst.name)

    latest = build_latest_json(args.variant, args.version, zip_path.name, zip_path)
    (out / "latest.json").write_text(
        json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"核 exe    : {core_dst}")
    print(f"Gui.exe   : {gui_dst}（4 个 gui 变体共用一份）")
    print(f"启动.cmd  : {cmd_path}")
    print(f"便携 zip  : {zip_path if not args.skip_zip else '(跳过)'}")
    if msi_dst is not None:
        print(f"MSI       : {msi_dst}")
    print(f"latest    : {out / 'latest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
