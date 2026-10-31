# -*- coding: utf-8 -*-
"""GUI 快照测试（规范 §10.4）：深浅 × 两套标题栏 = 4 组基准图，像素差异 ≤0.1%。

做法：起一个只读静态服务指向 gui/src，用本机 Edge 无头模式渲染（见 gui/scripts/snapshot.mjs），
逐像素与 gui/snapshots/ 里的基准图比较。基准图缺失即失败，需显式 --update 才写入。

用法：
    python scripts/gui_snapshot.py            # 比对（CI 用这个）
    python scripts/gui_snapshot.py --update   # 重新生成基准图
"""

from __future__ import annotations

import functools
import http.server
import os
import shutil
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

from PIL import Image, ImageChops


def _ensure_utf8_stdout() -> None:
    """CI 的 Windows 控制台默认是 cp1252/cp437，打印中文会 UnicodeEncodeError。

    这里把标准输出/错误重挂成 UTF-8（不能重挂就退回 ascii + replace），
    让脚本在任何代码页下都能跑完 —— 只是显示字符可能降级，不影响比对结果。
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


ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "gui"
SRC = GUI / "src"
BASELINE = GUI / "snapshots"
TMP = GUI / ".snapshots-tmp"
NODE_SCRIPT = GUI / "scripts" / "snapshot.mjs"
THRESHOLD_PCT = 0.1
COMBOS = ["dark-custom", "dark-native", "light-custom", "light-native"]


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):  # 静默，避免污染测试输出
        pass

    def translate_path(self, path):  # 只暴露 gui/src
        rel = path.split("?", 1)[0].split("#", 1)[0].lstrip("/") or "index.html"
        return str(SRC / rel)


def find_browser() -> str:
    """定位浏览器：EDGE_PATH 优先，其次由**系统环境变量**拼标准安装位置。

    这里刻意不写任何盘符或用户名：只从 ProgramFiles(x86)/ProgramFiles/LOCALAPPDATA
    这类系统变量推导相对路径，换机器不会失效，也不会把本机路径写进仓库。
    """
    explicit = os.environ.get("EDGE_PATH")
    if explicit:
        return explicit
    candidates = []
    for base_var, rel in (
        ("ProgramFiles(x86)", ("Microsoft", "Edge", "Application", "msedge.exe")),
        ("ProgramFiles", ("Microsoft", "Edge", "Application", "msedge.exe")),
        ("LOCALAPPDATA", ("Microsoft", "Edge", "Application", "msedge.exe")),
        ("ProgramFiles(x86)", ("Google", "Chrome", "Application", "chrome.exe")),
    ):
        base = os.environ.get(base_var)
        if base:
            candidates.append(Path(base, *rel))
    for path in candidates:
        if path.exists():
            return str(path)
    raise SystemExit("找不到浏览器：请设置 EDGE_PATH（Edge/Chrome 可执行文件路径）后重试")


def capture(out_dir: Path) -> None:
    handler = functools.partial(_Quiet, directory=str(SRC))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    env = dict(os.environ)
    env["EDGE_PATH"] = find_browser()
    try:
        proc = subprocess.run(
            ["node", str(NODE_SCRIPT), f"http://127.0.0.1:{port}/index.html", str(out_dir)],
            cwd=str(GUI),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    finally:
        httpd.shutdown()
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"快照采集失败（退出码 {proc.returncode}）")


def diff_pct(a: Path, b: Path) -> float:
    """可见差异像素占全部像素的百分比；尺寸不一致直接判 100%。"""
    img_a = Image.open(a).convert("RGB")
    img_b = Image.open(b).convert("RGB")
    if img_a.size != img_b.size:
        return 100.0
    delta = ImageChops.difference(img_a, img_b).convert("L")
    changed = sum(1 for v in delta.getdata() if v > 8)  # >8 才算可见差异，滤掉抗锯齿噪声
    return changed * 100.0 / (img_a.size[0] * img_a.size[1])


def main() -> int:
    _ensure_utf8_stdout()
    update = "--update" in sys.argv
    shutil.rmtree(TMP, ignore_errors=True)
    try:
        capture(TMP)
        if update:
            BASELINE.mkdir(parents=True, exist_ok=True)
            for name in COMBOS:
                Image.open(TMP / f"{name}.png").save(BASELINE / f"{name}.png")
            print(f"[snapshot] 已写入 {len(COMBOS)} 组基准图 → {BASELINE}")
            return 0

        failed = []
        for name in COMBOS:
            cur = TMP / f"{name}.png"
            base = BASELINE / f"{name}.png"
            if not base.exists():
                failed.append(f"{name}: 基准图缺失（先跑 --update）")
                continue
            pct = diff_pct(cur, base)
            ok = pct <= THRESHOLD_PCT
            print(
                f"[snapshot] {name}: 差异 {pct:.3f}%（阈值 {THRESHOLD_PCT}%）→ {'OK' if ok else 'FAIL'}"
            )
            if not ok:
                failed.append(f"{name}: 差异 {pct:.3f}%")
        if failed:
            print("\n".join(failed), file=sys.stderr)
            return 1
        print(f"[snapshot] 4 组全部通过（≤{THRESHOLD_PCT}%）")
        return 0
    finally:
        shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
