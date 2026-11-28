#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""diag_mei_leftover.py —— onefile 解包残留（_MEI*）诊断（GOALS P0-14）

为什么需要：PyInstaller onefile 每次启动把上百个文件解包到 `%TEMP%\\_MEIxxxxx`，
退出时由 bootloader 删目录。安全软件实时扫描持有其中 DLL/Fiber stub 的句柄时，
删除会失败（PyInstaller 原生 Warning `Failed to remove temporary directory`）；
下次启动再把同名 DLL 解包到新目录，仍被扫描占用 → 原生 Error
`Failed to load Python DLL ... LoadLibrary: 另一个程序正在使用此文件`。
两张框都在我们的代码之外，只能诊断与规避，治本形态是 P7-0 的 onedir。

**默认只看不删**（诊断工具不该顺手改宿主环境）；要清残留加 `--delete`。
判定只读、不结束任何进程、不改任何安全软件设置。

判定规则：
  * IN_USE   —— 有进程已加载该目录里的模块（不能删，可能是正在运行的实例）；
  * LOCKED   —— 无进程在用，但个别文件连 DELETE 权限都申请不到
                （仅 WinError 32/33 共享冲突；典型为安全软件实时扫描的短时锁）；
  * UNKNOWN  —— 无法证明「没人在用」：进程枚举不可信（EnumProcesses 失败/截断、
                同会话有看不见的同名实例），或文件因 ACL/超长路径等原因访问不了；
  * REMOVABLE / REMOVED / FAILED —— 未发现持有者；可用 --delete 删 / 已删 / 删不掉。

用法：
    python scripts/diag_mei_leftover.py                 # 扫描 %TEMP%（只看，min-age 600s）
    python scripts/diag_mei_leftover.py --delete        # 扫描并删除无持有的残留
    python scripts/diag_mei_leftover.py --min-age 0     # 连新解包的目录也列出来
    python scripts/diag_mei_leftover.py --root D:/x/.mei-runtime   # --runtime-tmpdir 目录

退出码：0 = 没有残留（或已全部删除）；1 = 仍有残留目录；2 = 参数错误。
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
DEFAULT_MIN_AGE = 600.0
# 这些文件名被锁 ⇒ 指向「安全软件实时扫描加载中的系统组件」，而不是业务文件
_STUB_PREFIXES = ("api-ms-win-", "ext-ms-win-", "python3", "vcruntime", "ucrtbase", "msvcp")
_SHARING_ERRORS = (32, 33)  # ERROR_SHARING_VIOLATION / ERROR_LOCK_VIOLATION

_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_READ = 0x0010
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_LIST_MODULES_ALL = 0x03
_DELETE = 0x00010000
_FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_MAX_PATH = 32768
_INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


def _path_key(path) -> str:
    """比较用路径键：normcase + 去 `\\\\?\\` 前缀。只用于比较。"""
    norm = os.path.normcase(os.path.abspath(str(path)))
    if norm.startswith("\\\\?\\"):
        norm = norm[4:]
        if norm.startswith("unc\\"):
            norm = "\\\\" + norm[4:]
    return norm


def _same_or_below(key: str, root_key: str) -> bool:
    return key == root_key or key.startswith(root_key + os.sep)


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _process_session(pid: int) -> int | None:
    k32 = _kernel32()
    k32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    session = wintypes.DWORD()
    if not k32.ProcessIdToSessionId(pid, ctypes.byref(session)):
        return None
    return session.value


def _process_image_name(pid: int) -> str | None:
    k32 = _kernel32()
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(_MAX_PATH)
        size = wintypes.DWORD(_MAX_PATH)
        if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        return os.path.basename(buf.value)
    finally:
        k32.CloseHandle(handle)


def process_holders(directory: str) -> list[tuple[int, str]] | None:
    """已加载模块落在 directory 内的进程 [(pid, 模块路径)]；None = 枚举不可信。

    不可信：EnumProcesses 失败 / PID 数顶到缓冲区上限；或同会话进程打不开、
    且其映像名与本进程 exe 同名（可能是提权的另一个实例）。此时不能证明没人在用。
    非 Windows 返回 []（本工具只针对 Windows onefile 目录）。
    """
    if sys.platform != "win32":
        return []
    target = _path_key(directory)
    k32 = _kernel32()
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
        return None
    if needed.value >= ctypes.sizeof(pids):
        return None
    count = needed.value // ctypes.sizeof(wintypes.DWORD)

    own_names = {os.path.normcase(os.path.basename(sys.executable or ""))}
    own_names.add(os.path.normcase(os.path.basename(sys.argv[0] or "")))
    own_names.discard("")
    my_session = _process_session(os.getpid())

    hits: list[tuple[int, str]] = []
    for pid in pids[:count]:
        if not pid:
            continue
        handle = k32.OpenProcess(_PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, False, pid)
        if not handle:
            if my_session is not None and _process_session(pid) == my_session:
                name = _process_image_name(pid)
                if name and os.path.normcase(name) in own_names:
                    return None
            continue
        try:
            mods = (wintypes.HMODULE * 1024)()
            cb = wintypes.DWORD()
            if not psapi.EnumProcessModulesEx(
                handle, mods, ctypes.sizeof(mods), ctypes.byref(cb), _LIST_MODULES_ALL
            ):
                continue
            for i in range(cb.value // ctypes.sizeof(wintypes.HMODULE)):
                buf = ctypes.create_unicode_buffer(_MAX_PATH)
                if not psapi.GetModuleFileNameExW(handle, mods[i], buf, _MAX_PATH):
                    continue
                if _same_or_below(_path_key(os.path.dirname(buf.value)), target):
                    hits.append((pid, buf.value))
        finally:
            k32.CloseHandle(handle)
    return hits


def can_delete(path: str) -> tuple[bool, int]:
    """非破坏性探测：能否申请到该文件的 DELETE 权限。返回 (可删, winerror)。"""
    if sys.platform != "win32":
        return True, 0
    k32 = _kernel32()
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


def probe_files(
    directory: str, limit: int = 10
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """探测目录内文件的删除权限：返回 (共享冲突文件, 其他错误文件)，各限 limit 个。

    只有 WinError 32/33（别人正开着、且不允许删除共享）才算「被锁」；
    ACL 拒绝、超长路径等其它错误单独归类，不能都报成"安全软件在扫"。
    """
    locked: list[tuple[str, int]] = []
    other: list[tuple[str, int]] = []
    for root, _dirs, files in os.walk(directory):
        for name in files:
            full = os.path.join(root, name)
            ok, err = can_delete(full)
            if ok:
                continue
            rel = os.path.relpath(full, directory)
            if err in _SHARING_ERRORS:
                if len(locked) < limit:
                    locked.append((rel, err))
            elif len(other) < limit:
                other.append((rel, err))
    return locked, other


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
    """返回 {path, files, size, mtime, holders, removed, locked, other, reason, verdict}。"""
    files, size, mtime = _dir_stats(path)
    holders = process_holders(str(path))
    info = {
        "path": path,
        "files": files,
        "size": size,
        "mtime": mtime,
        "holders": holders or [],
        "removed": False,
        "locked": [],
        "other": [],
        "reason": "",
    }
    if holders is None:
        info["verdict"] = "UNKNOWN"
        info["reason"] = "进程枚举不可信（EnumProcesses 失败/截断，或同会话有看不见的同名实例）"
        return info
    if holders:
        info["verdict"] = "IN_USE"
        return info
    locked, other = probe_files(str(path))
    info["locked"], info["other"] = locked, other
    if locked:
        info["verdict"] = "LOCKED"
        return info
    if other:
        info["verdict"] = "UNKNOWN"
        info["reason"] = "文件因 ACL/路径等原因无法申请删除权限（非共享冲突）"
        return info
    if not do_delete:
        info["verdict"] = "REMOVABLE"
        return info
    shutil.rmtree(path, ignore_errors=True)
    info["removed"] = not path.exists()
    info["verdict"] = "REMOVED" if info["removed"] else "FAILED"
    return info


def _print_report(items: list[dict], *, do_delete: bool, scanned: Path, min_age: float) -> None:
    stamp = "%Y-%m-%d %H:%M:%S"
    print("=" * 78)
    print("_MEI* 解包残留诊断（P0-14）")
    print(
        f"扫描目录: {scanned}   最小年龄: {int(min_age)}s   "
        f"操作: {'尝试删除' if do_delete else '仅查看（如需删除加 --delete）'}"
    )
    print("=" * 78)
    if not items:
        print("\n没有残留目录。")
    for info in items:
        when = time.strftime(stamp, time.localtime(info["mtime"])) if info["mtime"] else "-"
        print(f"\n[{info['path'].name}]  {when}  {info['files']} 个文件  {info['size']} 字节")
        verdict = info["verdict"]
        if verdict == "IN_USE":
            pids = ", ".join(str(pid) for pid in dict.fromkeys(pid for pid, _m in info["holders"]))
            print(f"  判定: IN_USE —— 有进程正在使用（pid {pids}），跳过不删")
            continue
        if verdict == "UNKNOWN":
            print(f"  判定: UNKNOWN —— {info['reason']}")
            for name, err in info["other"]:
                print(f"    无法访问  {name}  (WinError {err})")
            continue
        if verdict == "LOCKED":
            print("  判定: LOCKED —— 以下文件正被其它程序打开（共享冲突）:")
            for name, err in info["locked"]:
                print(f"    LOCKED  {name}  (WinError {err})")
            if any(_is_stub(name) for name, _err in info["locked"]):
                print("  原因: 安全软件实时扫描持有系统 stub/DLL 的句柄（典型为短时锁）")
                print("        实测扫描结束后可删；不要结束安全软件进程，等待或把它加入信任目录")
            continue
        if verdict == "REMOVED":
            print("  删除: 成功")
        elif verdict == "REMOVABLE":
            print("  删除: 未执行 —— 未发现持有者，且所有文件可申请删除权限（--delete 可删）")
        elif verdict == "FAILED":
            print("  删除: 失败（rmtree 后目录仍在）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="_MEI* 解包残留诊断（P0-14）")
    ap.add_argument(
        "--root",
        action="append",
        default=[],
        metavar="DIR",
        help="扫描根目录（可重复）。默认 TEMP；--runtime-tmpdir 构建请加 --root <那个目录>",
    )
    ap.add_argument(
        "--min-age",
        type=float,
        default=DEFAULT_MIN_AGE,
        help=f"只列出年龄 ≥ N 秒的残留（默认 {int(DEFAULT_MIN_AGE)}，与启动自愈同口径）",
    )
    ap.add_argument("--delete", action="store_true", help="删除无持有的残留（默认只看不删）")
    args = ap.parse_args(argv)

    roots = [Path(p) for p in args.root] if args.root else [Path(tempfile.gettempdir())]
    items: list[dict] = []
    for root in roots:
        items += [diagnose(p, do_delete=args.delete) for p in scan(root, args.min_age)]

    for root in roots:
        root_items = [i for i in items if _path_key(i["path"].parent) == _path_key(root)]
        _print_report(root_items, do_delete=args.delete, scanned=root, min_age=args.min_age)

    if items:
        products = av_products()
        if products:
            print("\n本机注册的安全产品（只读查询）: " + " / ".join(products))
        else:
            print("\n本机安全产品: 未读到（Security Center 未注册或无权限，不影响判定）")

    total = len(items)
    removable = sum(1 for i in items if i["verdict"] in ("REMOVED", "REMOVABLE"))
    locked = sum(1 for i in items if i["verdict"] == "LOCKED")
    in_use = sum(1 for i in items if i["verdict"] == "IN_USE")
    unknown = sum(1 for i in items if i["verdict"] in ("UNKNOWN", "FAILED"))
    print("\n" + "-" * 78)
    print(
        f"汇总: 残留 {total} / 可删 {removable} / 锁定 {locked} / 使用中 {in_use} / 未知 {unknown}"
    )
    remaining = sum(1 for i in items if i["path"].exists())
    if remaining == 0:
        print("结论: 没有残留目录。")
        return 0
    print(f"结论: 仍有 {remaining} 个残留目录（未删除或被锁定/占用），见上方判定。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
