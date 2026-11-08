# -*- coding: utf-8 -*-
"""P0-3 最小复现/验收脚本 —— 编码探测与轮转健壮性(R-25/R-26/R-27/R-30)

直接跑:
    python scripts/repro_p0_3.py
    GUILD_REAL_LOG_DIR=<真实日志目录> python scripts/repro_p0_3.py   # 追加真实样本回归

每条独立断言,末尾打印 PASS/FAIL/SKIPPED 汇总:
  * 有 FAIL → 退出码 1
  * 无 FAIL 但有 SKIPPED → 退出码 2(没验不能算过,协议 F.4)
  * 全部 PASS → 退出码 0
反转验证:切回修复前的 main(d048e9a),R-25/R-26/R-27/R-30 四条均 FAIL。

真实样本目录走环境变量而不是写死本机路径(CLAUDE.md §6:不得上传本机绝对路径)。
P0-5 修:未设 GUILD_REAL_LOG_DIR 时该项输出独立 **SKIPPED**、**不计入 PASS 数**,
且退出码非零 —— 旧版输出 "SKIP" 却计入 PASS 并 EXIT=0,等于"没验也算过"。
"""

import glob
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from guild_auto.infrastructure import logger  # noqa: E402
from guild_auto.infrastructure.log_watcher import LogWatcher, detect_encoding  # noqa: E402

RESULTS = []


class Skipped:
    """这一项没有可验的样本/环境 —— 既不算通过也不算失败。

    必须与"通过"区分开:把没验的项记成 PASS 会让汇总行看起来全绿,
    而实际上最关键的证据根本没取到。
    """

    def __init__(self, reason):
        self.reason = reason


def _silence_logger():
    """日志写到控制台会把断言输出淹没;这里只保留判定结果。"""

    def _noop(*_a, **_kw):
        return None

    for n in (
        "log_debug",
        "log_info",
        "log_warning",
        "log_error",
        "log_exception",
        "log_critical",
        "log_trace",
        "trace_code_location",
        "trace_return",
        "trace_file_read",
        "trace_call",
    ):
        if hasattr(logger, n):
            setattr(logger, n, _noop)


def check(name, fn):
    t0 = time.time()
    try:
        detail = fn()
    except AssertionError as e:
        RESULTS.append((name, "fail", f"{e} ({time.time() - t0:.1f}s)"))
        return
    except Exception as e:  # 缺属性/API 变化 = 修复前的旧代码
        RESULTS.append((name, "fail", f"{type(e).__name__}: {e}"))
        return
    if isinstance(detail, Skipped):
        RESULTS.append((name, "skip", f"{detail.reason} ({time.time() - t0:.1f}s)"))
    else:
        RESULTS.append((name, "pass", f"{detail or ''} ({time.time() - t0:.1f}s)"))


# ---------------------------------------------------------------------------
# R-25 采样把 UTF-8 多字节字符切成半截 → 合法 UTF-8 日志被误判 gbk
# ---------------------------------------------------------------------------
def _build_misaligned_utf8(path):
    """构造一个"采样位置正好落在汉字中间"的 UTF-8 日志(模拟真实多行日志)。"""
    line = ("公会面板中文内容" * 100).encode("utf-8") + b"\n"
    for pad in range(8):
        data = b"[" + b"x" * pad + line * 200
        size = len(data)
        positions = sorted({0, size // 4, size // 2, size * 3 // 4, max(0, size - 65536)})
        if any(pos and 0x80 <= data[pos] <= 0xBF for pos in positions):
            with open(path, "wb") as f:
                f.write(data)
            return [p for p in positions if p and 0x80 <= data[p] <= 0xBF]
    raise AssertionError("构造不出错位采样点(测试前提失效)")


def r25():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "utf8.log")
    bad_pos = _build_misaligned_utf8(path)
    enc = detect_encoding(path)
    # 同一份内容用"从头整段读"做对照,证明它是合法 UTF-8
    raw = open(path, "rb").read()
    raw.decode("utf-8")
    assert enc == "utf-8", f"合法 UTF-8 被误判为 {enc}(错位采样点 {bad_pos})"
    return f"{len(raw)}B utf-8 严格可解,判定 {enc}"


# ---------------------------------------------------------------------------
# R-26 空日志 open() 时锁定 utf-8 → 之后写入的 GBK 内容全部乱码
# ---------------------------------------------------------------------------
def r26():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "empty_then_gbk.log")
    open(path, "wb").close()
    w = LogWatcher(path, encoding="auto", poll_interval_ms=20)
    w.open()
    try:
        with open(path, "ab") as f:
            f.write("公会面板中文匹配\n".encode("gbk"))
        lines = w.read_new_lines(block=False)
        enc = w.encoding
    finally:
        w.close()
    assert lines == ["公会面板中文匹配"], f"按 {enc} 解码成 {lines!r}"
    return f"空文件起步后锁定 {enc},内容正确"


# ---------------------------------------------------------------------------
# R-27 轮转窗口内 _open_existing 死等 30s,无视调用方 timeout / stop_check
# ---------------------------------------------------------------------------
def r27():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "latest.log")
    with open(path, "wb") as f:
        f.write(b"x\n")
    w = LogWatcher(path, encoding="utf-8", poll_interval_ms=20)
    w.open()
    w.close()  # Windows 上句柄没关就删不掉文件
    os.remove(path)  # 轮转窗口:旧文件已删、新的还没建
    t0 = time.time()
    try:
        w.read_until(timeout=0.6)
    finally:
        w.close()
    took = time.time() - t0
    assert took < 5.0, f"read_until(timeout=0.6) 实际耗时 {took:.1f}s —— 无视调用方 timeout"
    return f"read_until(timeout=0.6) 耗时 {took:.2f}s"


# ---------------------------------------------------------------------------
# R-30 原地重写变大:只认"文件变小"的轮转判定 → 从旧偏移续读,首行被切坏
# ---------------------------------------------------------------------------
def r30():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "rewrite.log")
    old = "旧文件第一行\n旧文件第二行\n"
    with open(path, "wb") as f:
        f.write(old.encode("utf-8"))
    w = LogWatcher(path, encoding="utf-8", poll_interval_ms=20)
    w.open()
    try:
        new = "新文件第一行\n新文件第二行\n新文件第三行\n"
        with open(path, "r+b") as f:  # 原地重写:同 inode,文件变大
            f.seek(0)
            f.write(new.encode("utf-8"))
            f.truncate()
        lines = w.read_new_lines(block=False)
    finally:
        w.close()
    want = ["新文件第一行", "新文件第二行", "新文件第三行"]
    assert lines == want, f"原地重写后读到 {lines!r},首行被切坏(应为 {want!r})"
    return f"原 {len(old)}B → 新 {len(new)}B,读到 {len(lines)} 行完整内容"


# ---------------------------------------------------------------------------
# 真实样本回归:网易版 latest.log.* 严格可解且无替换符
# ---------------------------------------------------------------------------
def real_samples():
    root = os.environ.get("GUILD_REAL_LOG_DIR", "")
    files = sorted(glob.glob(os.path.join(root, "latest.log.*"))) if root else []
    if not files:
        # 没有样本 = 这项没验,既不算通过也不算失败(见 Skipped 的说明)
        return Skipped("未设置 GUILD_REAL_LOG_DIR 或目录下无 latest.log.*,本次未做真实样本回归")
    bad = []
    for p in files:
        enc = detect_encoding(p)
        text = open(p, "rb").read().decode(enc, errors="replace")
        n = text.count("�")
        if n:
            bad.append(f"{os.path.basename(p)}=>{enc} 替换符 {n}")
    assert not bad, "真实样本仍有替换符: " + "; ".join(bad)
    return f"{len(files)} 个样本 0 替换符: " + ", ".join(
        f"{os.path.basename(p)}={detect_encoding(p)}" for p in files
    )


def main():
    # Windows 控制台默认 cp936,打印"�"(U+FFFD)会直接 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    _silence_logger()
    check("R-25 UTF-8 多字节错位采样", r25)
    check("R-26 空文件起步后 GBK 内容", r26)
    check("R-27 轮转窗口内 timeout 生效", r27)
    check("R-30 原地重写变大检测", r30)
    check("真实样本 0 替换符", real_samples)

    print()
    for name, status, detail in RESULTS:
        print(f"[{status.upper()}] {name}: {detail}")
    passed = sum(1 for r in RESULTS if r[1] == "pass")
    failed = [r for r in RESULTS if r[1] == "fail"]
    skipped = sum(1 for r in RESULTS if r[1] == "skip")
    print(f"\n{passed} passed / {len(failed)} failed / {skipped} skipped")
    if failed:
        return 1
    if skipped:
        # 没验不能算过:把"缺样本"暴露成非零退出码,别让汇总行读成"全绿"
        print("存在未验证项(SKIPPED):本次不算全过,退出码 2")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
