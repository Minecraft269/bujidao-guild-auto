# -*- coding: utf-8 -*-
"""
logger.py —— 分级日志系统
==========================
日志写入脚本同目录的 logs/ 文件夹:
  * latest.log         —— 本次运行进行中的最新日志(每次运行先归档旧的再新建)
  * app-YYYY-MM-DD.gz  —— 历史归档:重命名 latest.log → app-YYYY-MM-DD.log 后 gzip 压成
                           .gz 压缩包(GZ 包内存放 .log 文件);当天多份时文件名末加 -2/-3...
                           序号防重复

日志级别(用户级别,数字越大越详细,每级包含上一级更严重的全部):
  1 致命    : 仅脚本启动失败 / 意外关闭(非用户手动关闭)
  2 严重错误 : 严重错误(含级别1)
  3 错误    : 一般错误/警告(含级别2)
  4 信息    : 常规运行信息(含级别3)     <-- 默认
  5 调试    : 脚本正在做什么 + 返回值（当前"调试"）
  6 追踪    : 最详细,完整追踪：函数调用链、读取了什么文件、代码执行到哪里
"""
import gzip
import logging
import os
import re
import shutil
from datetime import datetime

# 用户级别 → Python logging 阈值(logging: CRITICAL=50 > ERROR=40 > WARNING=30 > INFO=20 > DEBUG=10)
# TRACE 自定义为 5,比 DEBUG 更详细
TRACE_LEVEL = 5
LEVEL_MAP = {
    1: logging.CRITICAL,
    2: logging.ERROR,
    3: logging.WARNING,
    4: logging.INFO,
    5: logging.DEBUG,
    6: TRACE_LEVEL,
}
# 级别数值 → 英文显示名(用于日志行)
LEVEL_EN = {
    logging.CRITICAL: "Critical",
    logging.ERROR: "Error",
    logging.WARNING: "Warning",
    logging.INFO: "Info",
    logging.DEBUG: "Debug",
    TRACE_LEVEL: "Trace",
}

_logger = None       # 实际 logging.Logger
_log_dir = "logs"    # 日志目录(相对脚本运行目录)
_current_level = 4   # 当前用户级别(默认 4=信息)
_enabled = True
_initialized = False  # 防止重复 init 时误归档当前运行的日志


# 进程启动时间戳(用于相对偏移)
import time as _time
_START_TIME = _time.monotonic()


class _Formatter(logging.Formatter):
    """将日志行中的 levelname 显示为英文级别名 + 相对启动时间偏移。"""

    def format(self, record):
        record.levelname = LEVEL_EN.get(record.levelno, record.levelname)
        # 相对启动时间的秒偏移(进程内单调,不受系统时间调整影响)
        rel = _time.monotonic() - _START_TIME
        return f"{super().format(record)} (+{rel:.3f}s)"


def _next_archive_name(log_dir, today):
    """计算当天下一份归档的文件基名(不含扩展名),序号递增防重复。

    第1份 app-YYYY-MM-DD(无序号);已有归档时取现有最大序号+1:
      已有 app-D.gz        → app-D-2
      已有 app-D-2.gz      → app-D-3
      已有 app-D.gz 与 -2  → app-D-3
    """
    existing = [f for f in os.listdir(log_dir)
                if f.startswith(f"app-{today}") and f.endswith(".gz")]
    if not existing:
        return f"app-{today}"
    max_seq = 0
    for f in existing:
        m = re.match(rf"app-{re.escape(today)}-(\d+)\.gz$", f)
        if m:
            max_seq = max(max_seq, int(m.group(1)))
    if max_seq == 0:  # 只有无序号的 app-today.gz
        max_seq = 1
    return f"app-{today}-{max_seq + 1}"


def _archive_old_latest(log_dir):
    """归档旧的 latest.log:重命名(保留 .log)→ app-YYYY-MM-DD.log,再压缩为 .gz。

    压缩包(GZ)内存放的是对应 .log 文件(GzipFile 内嵌 .log 文件名)。
    返回归档后的 .gz 文件名,无旧日志返回 None。"""
    latest_path = os.path.join(log_dir, "latest.log")
    if not os.path.exists(latest_path):
        return None

    today = datetime.now().strftime("%Y-%m-%d")
    base = _next_archive_name(log_dir, today)
    log_name = base + ".log"      # 重命名后的 .log 文件
    gz_path = os.path.join(log_dir, base + ".gz")

    raw = os.path.join(log_dir, log_name)
    if os.path.exists(raw):
        os.remove(raw)
    if os.path.exists(gz_path):   # 极端兜底:目标已存在则再递增,绝不覆盖
        base = f"{base}-x"
        log_name = base + ".log"
        gz_path = os.path.join(log_dir, base + ".gz")
        raw = os.path.join(log_dir, log_name)

    shutil.move(latest_path, raw)
    # gzip 压缩:压缩包内文件名记为 .log(用 GzipFile filename 内嵌)
    try:
        with open(raw, "rb") as f_in, open(gz_path, "wb") as f_gz, \
                gzip.GzipFile(filename=log_name, mode="wb", fileobj=f_gz) as f_out:
            shutil.copyfileobj(f_in, f_out)
    finally:
        if os.path.exists(raw):
            os.remove(raw)  # 删除未压缩的中间 .log(压缩失败也不留残骸)
    return os.path.basename(gz_path)


def init_logger(log_dir="logs", level=4, enabled=True):
    """初始化日志系统。返回 logging.Logger。

    log_dir: 日志目录(相对脚本运行目录,自动创建)
    level:   用户日志级别 1~6(默认 4=信息)
    enabled: False 时返回空 logger,不写文件
    重复调用不会再次归档(仅首次归档上一运行的 latest.log)。
    """
    global _logger, _log_dir, _current_level, _enabled, _initialized
    _log_dir = log_dir
    _current_level = max(1, min(6, int(level)))
    _enabled = bool(enabled)

    logger = logging.getLogger("guild_auto")
    logger.handlers.clear()
    logger.propagate = False

    if not enabled:
        logger.setLevel(logging.CRITICAL + 1)  # 高于致命,实际不记录
        _logger = logger
        _initialized = True
        return logger

    os.makedirs(log_dir, exist_ok=True)
    archived = None
    if not _initialized:  # 仅首次初始化时归档上一运行的日志
        archived = _archive_old_latest(log_dir)

    threshold = LEVEL_MAP[_current_level]
    logger.setLevel(threshold)

    handler = logging.FileHandler(os.path.join(log_dir, "latest.log"), encoding="utf-8")
    handler.setLevel(threshold)
    handler.setFormatter(_Formatter("%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
                                      datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    _logger = logger
    _initialized = True
    if archived:
        logger.log(logging.INFO, f"[归档] 上一运行日志已压缩: {archived}")
    return logger


def _log(level, msg):
    if _logger is None:
        return
    _logger.log(level, msg)


def log_critical(msg): _log(logging.CRITICAL, msg)
def log_error(msg):    _log(logging.ERROR, msg)
def log_warning(msg):  _log(logging.WARNING, msg)
def log_info(msg):     _log(logging.INFO, msg)
def log_debug(msg):    _log(logging.DEBUG, msg)
def log_trace(msg):    _log(TRACE_LEVEL, msg)


def log_exception(msg, exc=None):
    """记录异常/意外关闭(级别 致命)。exc 为异常对象,None 时取当前异常上下文。"""
    import traceback
    if exc is None:
        tb = traceback.format_exc()
    else:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log_critical(msg + "\n" + tb)


# ---------------------------------------------------------------------------
# TRACE 追踪辅助:函数调用追踪器
# ---------------------------------------------------------------------------
import inspect


def trace_call(func_name, args=None, kwargs=None, result=None, exc=None):
    """记录函数调用追踪(级别 6)。
    func_name: 函数名(建议含模块前缀,如 "main.query_member")
    args/kwargs: 参数快照(仅关键字段,避免敏感/大数据)
    result: 返回值快照(简短)
    exc: 异常对象(若有)
    """
    if _current_level < 6:
        return
    parts = [f"TRACE_CALL {func_name}"]
    if args:
        parts.append(f"args={args}")
    if kwargs:
        parts.append(f"kwargs={kwargs}")
    if result is not None:
        parts.append(f"→ {result}")
    if exc is not None:
        parts.append(f"! EXCEPTION: {exc}")
    log_trace(" | ".join(parts))


def trace_return(func_name, result):
    """记录函数返回(级别 6)。"""
    trace_call(func_name, result=result)


def trace_exception(func_name, exc):
    """记录函数异常(级别 6)。"""
    trace_call(func_name, exc=exc)


def trace_file_read(path, size=None):
    """记录文件读取(级别 6)。"""
    if _current_level < 6:
        return
    info = f"TRACE_FILE_READ {path}"
    if size is not None:
        info += f" ({size} bytes)"
    log_trace(info)


def trace_file_write(path, size=None):
    """记录文件写入(级别 6)。"""
    if _current_level < 6:
        return
    info = f"TRACE_FILE_WRITE {path}"
    if size is not None:
        info += f" ({size} bytes)"
    log_trace(info)


def trace_code_location(phase, detail=""):
    """记录代码执行位置(级别 6)。
    phase: 简短阶段名,如 "query_member.start" / "execute_action.retry"
    detail: 补充信息
    """
    if _current_level < 6:
        return
    msg = f"TRACE_LOCATION {phase}"
    if detail:
        msg += f" | {detail}"
    log_trace(msg)


# 便捷查询
def current_level():
    return _current_level


def is_enabled():
    return _enabled
