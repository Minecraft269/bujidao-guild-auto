#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""diag_mei_leftover.py —— onefile 解包残留（_MEI*）诊断（GOALS P0-14）

为什么需要：PyInstaller onefile 每次启动把上百个文件解包到 `%TEMP%\\_MEIxxxxx`，
退出时由 bootloader 删目录。安全软件实时扫描持有其中 DLL/Fiber stub 的句柄时，
删除会失败（PyInstaller 原生 Warning `Failed to remove temporary directory`）；
下次启动再把同名 DLL 解包到新目录，仍被扫描占用 → 原生 Error
`Failed to load Python DLL ... LoadLibrary: 另一个程序正在使用此文件`。
两张框都在我们的代码之外，只能诊断与规避，治本形态是 P7-0 的 onedir。

本脚本做四件事（只读 + 尝试删除，**不结束任何进程、不改任何安全软件设置**）：
  1. 列出每个残留目录：创建/修改时间、文件数、体积；
  2. 判定「有没有进程在用」——枚举所有进程已加载模块的路径（不靠试删：Win10 起
     映射中的文件也允许解除链接，试删会把运行中实例的文件删掉）；
  3. 无进程在用时尝试删除；删不掉就逐个文件申请 DELETE 权限，报告**哪个文件被占用**；
  4. 被锁文件是 `api-ms-win-*` / `python3*.dll` 这类系统 stub 时，给出
     「安全软件实时扫描持有句柄（短时锁）」判定，并列出本机注册的安全产品。

用法：
    python scripts/diag_mei_leftover.py                 # 扫描 %TEMP%，尝试删除
    python scripts/diag_mei_leftover.py --no-delete     # 只看不删
    python scripts/diag_mei_leftover.py --root D:/x/.mei-runtime   # --runtime-tmpdir 目录
    python scripts/diag_mei_leftover.py --min-age 600   # 只看超过 10 分钟的

退出码：0 = 没有残留 / 残留已全部删除；1 = 仍有残留（被锁定）；2 = 参数错误。
"""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

MEI_PREFIX = "_MEI"
# 这些文件名被锁 ⇒ 指向「安全软件实时扫描加载中的系统组件」，而不是业务文件
_STUB_PREFIXES = ("api-ms-win-", "ext-ms-win-", "python3", "vcruntime", "ucrtbase", "msvcp")

_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_READ = 0x0010
_LIST_MODULES_ALL = 0x03
_DELETE = 0x00010000
_FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


def _norm(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def process_holders(directory: str) -> list[tuple[int, str]]:
    """返回「已加载模块落在 directory 内」的进程 [(pid, 模块路径)]。非 Windows 返回空。"""
    if sys.platform != "win32":
        return []
    target = _norm(directory)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.EnumProcesses.argtypes = [
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi.EnumProcessModulesEx.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HMODULE),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
    ]
    psapi.GetModuleFileNameExW.argtypes = [
        wintypes.HANDLE,
        wintypes.HMODULE,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    pids = (wintypes.DWORD * 4096)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed)):
        return []
    hits: list[tuple[int, str]] = []
    for pid in pids[: needed.value // ctypes.sizeof(wintypes.DWORD)]:
        if not pid:
            continue
        handle = k32.OpenProcess(_PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, False, pid)
        if not handle:  # 系统/受保护进程：不会加载用户态 onefile 的模块
            continue
        try:
            mods = (wintypes.HMODULE * 1024)()
            cb = wintypes.DWORD()
            if not psapi.EnumProcessModulesEx(
                handle, mods, ctypes.sizeof(mods), ctypes.byref(cb), _LIST_MODULES_ALL
            ):
                continue
            for i in range(cb.value // ctypes.sizeof(wintypes.HMODULE)):
                buf = ctypes.create_unicode_buffer(260)
                if not psapi.GetModuleFileNameExW(handle, mods[i], buf, 260):
                    continue
                if _norm(os.path.dirname(buf.value)) == target:
                    hits.append((pid, buf.value))
        finally:
            k32.CloseHandle(handle)
    return hits


def can_delete(path: str) -> tuple[bool, int]:
    """非破坏性探测：能否申请到该文件的 DELETE 权限。返回 (可删, winerror)。"""
    if sys.platform != "win32":
        return True, 0
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.CreateFileW(
        path, _DELETE, _FILE_SHARE_ALL, None, _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL, None
    )
    if handle == _INVALID_HANDLE_VALUE:
        return False, ctypes.get_last_error()
    k32.CloseHandle(handle)
    return True, 0


def locked_files(directory: str, limit: int = 10) -> list[tuple[str, int]]:
    """目录内申请不到 DELETE 权限的文件（前 limit 个），(相对路径, winerror)。"""
    out: list[tuple[str, int]] = []
    for root, _dirs, files in os.walk(directory):
        for name in files:
            full = os.path.join(root, name)
            ok, err = can_delete(full)
            if not ok:
                out.append((os.path.relpath(full, directory), err))
                if len(out) >= limit:
                    return out
    return out


def _is_stub(name: str) -> bool:
    low = name.lower()
    return any(low.startswith(p) or f"\\{p}" in low or f"/{p}" in low for p in _STUB_PREFIXES)


def av_products() -> list[str]:
    """只读枚举本机注册的安全产品（Windows Security Center 注册表视图）。

    只读查询，不启停任何服务、不改任何设置。读不到就返回空（不是错误）。
    """
    if sys.platform != "win32":
        return []
    cmd = (
        "Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct "
        "| Select-Object -ExpandProperty displayName"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    raw = proc.stdout
    for enc in ("utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    return [line.strip() for line in text.splitlines() if line.strip()]


def _dir_stats(path: Path) -> tuple[int, int, float]:
    files = 0
    size = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            files += 1
            try:
                size += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return files, size, mtime


def scan(root: Path, min_age: float) -> list[Path]:
    if not root.is_dir():
        return []
    now = time.time()
    found = []
    for child in root.iterdir():
        if not child.name.startswith(MEI_PREFIX) or not child.is_dir():
            continue
        try:
            age = now - child.stat().st_mtime
        except OSError:
            continue
        if age >= min_age:
            found.append(child)
    return sorted(found)


def diagnose(path: Path, *, do_delete: bool) -> dict:
    """返回 {path, files, size, mtime, holders, removed, locked, verdict}。"""
    files, size, mtime = _dir_stats(path)
    holders = process_holders(str(path))
    info = {
        "path": path,
        "files": files,
        "size": size,
        "mtime": mtime,
        "holders": holders,
        "removed": False,
        "locked": [],
    }
    if holders:
        info["verdict"] = "IN_USE"
        return info
    locked = locked_files(str(path))
    info["locked"] = locked
    if locked:
        info["verdict"] = "LOCKED"
        return info
    if not do_delete:
        info["verdict"] = "REMOVABLE"
        return info
    shutil.rmtree(path, ignore_errors=True)
    info["removed"] = not path.exists()
    info["verdict"] = "REMOVED" if info["removed"] else "FAILED"
    return info


def _print_report(items: list[dict], *, do_delete: bool, scanned: Path, min_age: float) -> int:
    stamp = "%Y-%m-%d %H:%M:%S"
    print("=" * 78)
    print("_MEI* 解包残留诊断（P0-14）")
    print(
        f"扫描目录: {scanned}   最小年龄: {int(min_age)}s   "
        f"操作: {'尝试删除' if do_delete else '仅查看'}"
    )
    print("=" * 78)
    if not items:
        print("\n没有残留目录。")
    for info in items:
        when = time.strftime(stamp, time.localtime(info["mtime"])) if info["mtime"] else "-"
        print(f"\n[{info['path'].name}]  {when}  {info['files']} 个文件  {info['size']} 字节")
        if info["verdict"] == "IN_USE":
            pids = ", ".join(str(pid) for pid in dict.fromkeys(pid for pid, _m in info["holders"]))
            print(f"  判定: IN_USE —— 有进程正在使用（pid {pids}），跳过不删")
            continue
        if info["verdict"] == "LOCKED":
            print("  删除: 失败 —— 以下文件申请不到删除权限（被外部句柄持有）:")
            for name, err in info["locked"]:
                print(f"    LOCKED  {name}  (WinError {err})")
            if any(_is_stub(name) for name, _err in info["locked"]):
                print("  判定: 安全软件实时扫描持有系统 stub/DLL 的句柄（典型为短时锁）")
                print("        实测扫描结束后可删；不要结束安全软件进程，等待或把它加入信任目录")
            else:
                print("  判定: 外部句柄持有（非进程模块），稍后重试或重启后再看")
            continue
        if info["verdict"] == "REMOVED":
            print("  删除: 成功")
        elif info["verdict"] == "REMOVABLE":
            print("  删除: 未执行（--no-delete）—— 当前无锁，可直接删除")
        elif info["verdict"] == "FAILED":
            print("  删除: 失败（rmtree 后目录仍在）")
        else:
            print(f"  判定: {info['verdict']}")

    products = av_products()
    if products:
        print("\n本机注册的安全产品（只读查询）: " + " / ".join(products))
    else:
        print("\n本机安全产品: 未读到（Security Center 未注册或无权限，不影响判定）")

    total = len(items)
    removable = sum(1 for i in items if i["verdict"] in ("REMOVED", "REMOVABLE"))
    locked = sum(1 for i in items if i["verdict"] in ("LOCKED", "FAILED"))
    in_use = sum(1 for i in items if i["verdict"] == "IN_USE")
    print("\n" + "-" * 78)
    print(f"汇总: 残留 {total} / 可删 {removable} / 锁定 {locked} / 使用中 {in_use}")
    remaining = locked + in_use
    print(
        "结论: "
        + ("没有残留问题。" if remaining == 0 else f"仍有 {remaining} 个残留目录，见上方判定。")
    )
    return 0 if remaining == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="_MEI* 解包残留诊断（P0-14）")
    ap.add_argument(
        "--root",
        action="append",
        default=[],
        metavar="DIR",
        help="扫描根目录（可重复）。默认 TEMP；本地 --runtime-tmpdir 构建请加 --root <那个目录>",
    )
    ap.add_argument("--min-age", type=float, default=0.0, help="只列出年龄 ≥ N 秒的残留（默认 0）")
    ap.add_argument("--no-delete", action="store_true", help="只看不删")
    args = ap.parse_args(argv)

    roots = [Path(p) for p in args.root] if args.root else [Path(tempfile.gettempdir())]
    items: list[dict] = []
    for root in roots:
        items += [diagnose(p, do_delete=not args.no_delete) for p in scan(root, args.min_age)]
    # 多根时按目录分组打印，单根保持原样
    rc = 0
    for root in roots:
        root_items = [i for i in items if _norm(i["path"].parent) == _norm(root)]
        rc = max(
            rc,
            _print_report(
                root_items, do_delete=not args.no_delete, scanned=root, min_age=args.min_age
            ),
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
