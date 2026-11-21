#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
build_all.py —— 本地一条命令构建能力注册表里的**全部**变体(GOALS P6-2)
====================================================================
变体集合与产物名**不写死**:全部由 guild_auto.capabilities.iter_variants()
现算(与 scripts/build-variants.py --list 同源)。注册表新增能力后,本脚本与
生成的 INDEX.md 自动多出 2^n-1 个变体,这里零改动。

每个变体串行构建:
    1. python scripts/build-variants.py --write-const src/guild_auto/_build_capabilities.py
       --capabilities <该变体的能力清单>          (与 CI 完全相同的打包期常量)
    2. PyInstaller --onefile ...                  (参数逐条对齐 build.yml 的变体任务)
    3. 产物移入 <out>/<拼接名>/;构建完在空目录里跑一次 exe,读它自报的变体名

输出布局(与 P1-4 对齐):
    <out>/<拼接名>/布吉岛公会管理脚本-<拼接名>[-Full].exe
    <out>/INDEX.md      变体名 / 文件名 / 字节数 / sha256 / 构建耗时 / 自检 / 状态

单个变体失败不中断其余;结束时退出码 = 失败变体数(协议 E:跳过必须可见)。

用法:
    python scripts/build_all.py                 # 构建全部(默认输出 dist-all/)
    python scripts/build_all.py --list          # 只列出将产出的 exe,不构建
    python scripts/build_all.py --only Lite --only Lite-Async   # 只构建指定变体(调试/故障演练)
    python scripts/build_all.py --out D:\\x --clean
    python scripts/build_all.py --no-verify     # 跳过产物自检(仅调试用)

注:P6-1 尚未落地,PyInstaller 参数暂与本文件同住一处(与 build.yml 逐条对齐);
P6-1 抽 scripts/build_args.py 时把 pyinstaller_args() 搬过去即可。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
sys.path.insert(0, str(SRC))

from guild_auto import capabilities  # noqa: E402

VARIANT_SCRIPT = Path(__file__).resolve().parent / "build-variants.py"
CONST_FILE = SRC / "guild_auto" / "_build_capabilities.py"
ICON = REPO_ROOT / "gui" / "src-tauri" / "icons" / "icon.ico"
# 产物命名规则(与 build.yml / package.py 一致)—— 这是产品名,不是变体名。
APP_NAME = "布吉岛公会管理脚本"

# 与 build.yml 同口径的体积裁剪清单。P6-1 抽单一真源后由 build_args.py 提供。
_EXCLUDES = (
    "cv2",
    "numpy",
    "PyQt5",
    "matplotlib",
    "scipy",
    "IPython",
    "pandas",
    "PIL.ImageTk",
    "win32api",
    "win32com",
    "win32con",
    "pythoncom",
    "pywintypes",
    "pytest",
)

_S0_RE = re.compile(r"build_variant=(\S+)\s+capabilities=(\S+)")
_BANNER_RE = re.compile(r"构建变体:\s*(\S+)\s+已启用能力:\s*(\S+)")


@dataclass(frozen=True)
class Variant:
    """一个构建变体。name/suffix 都由注册表现算,不存字面量。"""

    name: str
    capabilities: tuple[str, ...]
    suffix: str

    @property
    def exe_name(self) -> str:
        return f"{APP_NAME}-{self.name}{self.suffix}.exe"


@dataclass
class Row:
    """一个变体的构建结果(INDEX.md 的一行)。"""

    variant: str
    path: Path | None = None
    seconds: float = 0.0
    verify: str = ""
    error: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.path is not None and not self.error


def plan() -> list[Variant]:
    """注册表里的全部变体 —— 唯一权威是 capabilities.iter_variants()。

    全能力变体带 -Full 后缀(与 build-variants.py 的 artifact_suffix 同规则:
    取 aliases()["full"],不是写死的变体名)。
    """
    full = capabilities.aliases()["full"]
    return [
        Variant(name=name, capabilities=tuple(caps), suffix="-Full" if name == full else "")
        for name, caps in capabilities.iter_variants()
    ]


def select(variants: list[Variant], names: list[str]) -> list[Variant]:
    """按名字挑变体(调试/故障演练用)。名字必须来自 plan(),未知名字直接报错。"""
    if not names:
        return variants
    known = {v.name for v in variants}
    unknown = sorted(set(names) - known)
    if unknown:
        raise ValueError(f"未知变体: {','.join(unknown)};可用: {','.join(sorted(known))}")
    wanted = set(names)
    return [v for v in variants if v.name in wanted]


def pyinstaller_args(v: Variant, dist: Path, work: Path, spec: Path) -> list[str]:
    """单个变体的 PyInstaller 参数(逐条对齐 build.yml 的 matrix 任务)。"""
    args = [
        "--onefile",
        "--clean",
        "--noconfirm",
        "--name",
        v.exe_name[: -len(".exe")],
        "--paths",
        str(SRC),
        "--icon",
        str(ICON),
        "--distpath",
        str(dist),
        "--workpath",
        str(work),
        "--specpath",
        str(spec),
    ]
    if "vision" in v.capabilities:
        args += ["--collect-all", "winsdk"]
    else:
        args += ["--exclude-module", "winsdk"]
    for mod in _EXCLUDES:
        args += ["--exclude-module", mod]
    if "gui" in v.capabilities:
        # gui 变体双击即出界面,不该再弹控制台窗口(PE 子系统 = GUI)
        args += ["--noconsole"]
    args.append(str(SRC / "guild_auto" / "main.py"))
    return args


def _warn_upx() -> None:
    """UPX 缺失必须可见:PyInstaller 只打一条 warning 就静默跳过压缩。"""
    if shutil.which("upx"):
        return
    print(
        "[WARNING] 未找到 upx:本次产物未压缩(体积明显大于 CI 产物)。"
        "安装 upx 后重跑即可;若本机不需要压缩可忽略。",
        file=sys.stderr,
    )


def _copy_with_retry(src: Path, dst: Path, timeout: float = 60.0) -> None:
    """复制产物到输出目录;Windows 杀软会短暂占用刚生成的 exe,遇到占用就重试。

    不用 shutil.move:它跨盘是「复制 + 删除源」,删源被杀软挡住会让已经复制成功的
    产物被误判为构建失败(实测 Lite-Async / Lite-Gui-Vision 各命中一次)。
    临时目录由 TemporaryDirectory(ignore_cleanup_errors=True) 收尾,删不掉也不影响交付。
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            shutil.copy2(src, dst)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def build_one(v: Variant, out_root: Path) -> Row:
    """构建一个变体;任何失败都抛异常,由 run_plan() 记 FAILED 并继续。"""
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="guild-auto-build-", ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        subprocess.run(
            [
                sys.executable,
                str(VARIANT_SCRIPT),
                "--write-const",
                str(CONST_FILE),
                "--capabilities",
                ",".join(v.capabilities),
            ],
            cwd=REPO_ROOT,
            check=True,
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                *pyinstaller_args(v, tmp_path / "dist", tmp_path / "build", tmp_path),
            ],
            cwd=REPO_ROOT,
            check=True,
        )
        produced = tmp_path / "dist" / v.exe_name
        if not produced.is_file():
            raise RuntimeError(f"PyInstaller 未产出预期文件: {v.exe_name}")
        target_dir = out_root / v.name
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / v.exe_name
        _copy_with_retry(produced, target)
    return Row(variant=v.name, path=target, seconds=time.monotonic() - started)


def _read_text(path: Path) -> str:
    """读文本:优先 UTF-8;出现替换符且能按 GBK 解码时改按 GBK(冻结产物的控制台是 OEM 代码页)。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    utf8 = raw.decode("utf-8", errors="replace")
    if "�" in utf8:
        try:
            return raw.decode("gbk")
        except UnicodeDecodeError:
            pass
    return utf8


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
        capture_output=True,
        check=False,
    )
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def verify_exe(v: Variant, exe: Path, timeout: float = 30.0) -> tuple[bool, str]:
    """在空目录里跑一次产物,读**产物自己**写出的启动横幅。

    清掉 GUILD_AUTO_CAPABILITIES:产物读不到环境变量,只能靠打包期常量,
    这样比对的是「exe 里烧进去的那份能力清单」,不是源码/环境。

    横幅(capabilities.build_banner)由 console() 同时写入 latest.log ——
    gui 变体是 --noconsole,控制台不可见,日志是唯一可靠通道(8 个变体实测都有)。
    S0 启动快照里的 build_variant 只作兜底:横幅缺失判失败,不拿 S0 冒充横幅。
    """
    with tempfile.TemporaryDirectory(prefix="guild-auto-check-") as tmp:
        tmp_path = Path(tmp)
        stdout_file = tmp_path / "stdout.txt"
        log_file = tmp_path / "logs" / "latest.log"
        env = {k: val for k, val in os.environ.items() if k != capabilities.ENV_VAR}
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"  # 横幅要及时落到 stdout(仅控制台变体可见)
        with stdout_file.open("wb") as fh:
            proc = subprocess.Popen(
                [str(exe)], cwd=tmp, env=env, stdout=fh, stderr=subprocess.STDOUT
            )
            deadline = time.monotonic() + timeout
            try:
                while time.monotonic() < deadline:
                    if _BANNER_RE.search(_read_text(log_file)):
                        break
                    if proc.poll() is not None:
                        break
                    time.sleep(0.2)
            finally:
                _kill_tree(proc)
        banner = _BANNER_RE.search(_read_text(log_file)) or _BANNER_RE.search(
            _read_text(stdout_file)
        )
        s0 = _S0_RE.search(_read_text(log_file))

    if banner is not None:
        observed = banner.group(1)
        source = f"横幅={observed} 能力={banner.group(2)}"
    elif s0 is not None:
        return False, f"横幅缺失,只有 S0日志={s0.group(1)} 能力={s0.group(2)}"
    else:
        return False, "未取到横幅或 S0 日志(产物启动失败?)"
    if observed != v.name:
        return False, f"{source};期望 {v.name}"
    return True, source


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_rev() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return out.stdout.strip() or "-"
    except (OSError, subprocess.CalledProcessError):
        return "-"


def run_plan(
    variants: list[Variant],
    out_root: Path,
    *,
    verify: bool = True,
    build_fn=build_one,
    verify_fn=verify_exe,
) -> list[Row]:
    """串行构建;单个变体失败记录后继续下一个(协议 E:跳过必须可见)。"""
    rows: list[Row] = []
    total = len(variants)
    for idx, v in enumerate(variants, 1):
        print(f"=== [{idx}/{total}] 构建 {v.name} ({','.join(v.capabilities)}) ===")
        try:
            row = build_fn(v, out_root)
        except (subprocess.CalledProcessError, OSError, RuntimeError) as exc:
            print(f"!!! {v.name} 构建失败: {exc}", file=sys.stderr)
            rows.append(Row(variant=v.name, error=str(exc)))
            continue
        if verify:
            try:
                ok, detail = verify_fn(v, row.path)
            except Exception as exc:  # 自检本身出错也要记录,不能让整轮崩掉
                ok, detail = False, f"自检异常: {exc!r}"
            row.verify = detail
            if not ok:
                row.error = f"自检失败: {detail}"
                print(f"!!! {v.name} {row.error}", file=sys.stderr)
        size = row.path.stat().st_size if row.path and row.path.is_file() else 0
        print(f"--- {v.name}: {row.seconds:.1f}s, {size} 字节, {'OK' if row.ok else 'FAILED'}")
        rows.append(row)
    return rows


def render_index(rows: list[Row], out_root: Path, started_at: str, total_seconds: float) -> str:
    """INDEX.md:变体名 / 文件名 / 字节数 / sha256 / 构建耗时 / 自检 / 状态。"""
    ok_count = sum(1 for r in rows if r.ok)
    lines = [
        "# 本地构建产物索引",
        "",
        "> 由 `scripts/build_all.py` 生成。变体集合由 `capabilities.py` 注册表现算,数量不写死。",
        "",
        f"- 生成时间: {started_at}",
        f"- 提交: {_git_rev()}",
        f"- 变体总数: {len(rows)}(注册表现算)",
        f"- 成功 / 失败: {ok_count} / {len(rows) - ok_count}",
        f"- 总耗时: {total_seconds:.1f}s",
        "",
        "| 变体 | 文件 | 字节数 | sha256 | 构建耗时 | 自检 | 状态 |",
        "| --- | --- | ---: | --- | ---: | --- | --- |",
    ]
    for r in rows:
        if r.ok:
            path = r.path
            rel = path.relative_to(out_root).as_posix()
            lines.append(
                f"| {r.variant} | {rel} | {path.stat().st_size} | {sha256_of(path)} "
                f"| {r.seconds:.1f}s | {r.verify or '-'} | OK |"
            )
        else:
            reason = (r.error or "未知错误").replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {r.variant} | - | - | - | {r.seconds:.1f}s | - | FAILED: {reason} |")
    lines.append("")
    return "\n".join(lines)


def _snapshot_const() -> bytes | None:
    return CONST_FILE.read_bytes() if CONST_FILE.exists() else None


def _restore_const(snapshot: bytes | None) -> None:
    """还原打包期常量:留在仓库里会让源码运行悄悄锁定成某一个变体的能力。"""
    if snapshot is None:
        CONST_FILE.unlink(missing_ok=True)
    else:
        CONST_FILE.write_bytes(snapshot)


def _ensure_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdout()
    ap = argparse.ArgumentParser(description="本地构建能力注册表里的全部变体")
    ap.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "dist-all",
        help="输出根目录(默认 dist-all/;<out>/<拼接名>/ 每变体一目录)",
    )
    ap.add_argument("--clean", action="store_true", help="构建前清空输出根目录")
    ap.add_argument("--list", action="store_true", help="只列出将产出的 exe,不构建")
    ap.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="VARIANT",
        help="只构建指定变体(可重复;名字须出现在 --list 里)",
    )
    ap.add_argument("--no-verify", action="store_true", help="跳过产物自检(调试用)")
    ap.add_argument(
        "--verify-timeout", type=float, default=30.0, help="单次产物自检等待秒数(默认 30)"
    )
    args = ap.parse_args(argv)

    variants = plan()
    try:
        variants = select(variants, args.only)
    except ValueError as exc:
        ap.error(str(exc))
    if args.list:
        for v in variants:
            print(f"{v.name}\t{v.exe_name}\t{','.join(v.capabilities)}")
        return 0

    out_root = args.out if args.out.is_absolute() else (Path.cwd() / args.out)
    if args.clean and out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    if not ICON.is_file():
        print(f"[WARNING] 图标不存在,所有变体都会构建失败: {ICON}", file=sys.stderr)
    _warn_upx()

    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    t0 = time.monotonic()
    snapshot = _snapshot_const()
    try:
        rows = run_plan(
            variants,
            out_root,
            verify=not args.no_verify,
            verify_fn=lambda v, exe: verify_exe(v, exe, args.verify_timeout),
        )
    finally:
        _restore_const(snapshot)
    total_seconds = time.monotonic() - t0

    index_path = out_root / "INDEX.md"
    index_path.write_text(render_index(rows, out_root, started_at, total_seconds), encoding="utf-8")
    failed = [r for r in rows if not r.ok]
    print()
    print(
        f"汇总: 成功 {len(rows) - len(failed)} / 失败 {len(failed)}  (总耗时 {total_seconds:.1f}s)"
    )
    if failed:
        print("失败的变体: " + ", ".join(f"{r.variant}({r.error})" for r in failed))
    print(f"索引: {index_path}")
    return len(failed)


if __name__ == "__main__":
    raise SystemExit(main())
