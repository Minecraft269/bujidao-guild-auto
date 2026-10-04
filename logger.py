# -*- coding: utf-8 -*-
"""
logger.py —— 6 级分级日志系统
============================
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
  5 调试    : 脚本正在做什么 + 返回值
  6 追踪    : 最详细,五类结构化埋点(见下方 TRACE 埋点)

TRACE(6 级)埋点 —— 每类一个辅助函数,调用方一行打出结构化追踪:
  trace_call / trace_return / trace_exception   函数调用链:进入(参数快照)/返回(返回值)
  trace_file_read / trace_file_write           文件读写:路径 + 字节数 + 行数
  trace_code_location                          代码执行位置:阶段标记
  trace_thread                                 线程活动:线程名/标识/存活状态
  trace_queue                                  队列活动:队列名/深度/提交与消费
  trace_verify_stage                           后台验证全过程:提交→发送→读日志→判定→重试→完成

低开销:当前级别 <6(或未启用)时所有 trace_* 立即 return,不构造任何字符串。
线程安全:全部埋点走同一个 logging.Logger,由 logging 的 handler 锁保证单条记录不撕裂。
"""
import gzip
import logging
import os
import re
import shutil
import threading
import time as _time
import traceback as _tb
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

# 后台验证的标准阶段名(供调用方对齐,顺序即正常流程)
VERIFY_STAGES = ("submit", "send", "read_log", "judge", "retry", "done")

# trace 快照截断长度:防止单个返回值/参数把日志撑爆
_SNAP_MAX = 200

_logger = None       # 实际 logging.Logger
_log_dir = "logs"    # 日志目录(相对脚本运行目录)
_current_level = 4   # 当前用户级别(默认 4=信息)
_enabled = True
_initialized = False  # 防止重复 init 时误归档当前运行的日志

_START_TIME = _time.monotonic()   # 进程启动时间戳(用于相对偏移)
_INIT_LOCK = threading.Lock()      # 防止多线程同时 init 导致重复归档


class _Formatter(logging.Formatter):
    """将日志行中的 levelname 显示为英文级别名 + 相对启动时间偏移。"""

    def format(self, record):
        record.levelname = LEVEL_EN.get(record.levelno, record.levelname)
        # 相对启动时间的秒偏移(进程内单调,不受系统时间调整影响)
        rel = _time.monotonic() - _START_TIME
        return f"{super().format(record)} (+{rel:.3f}s)"


def _snap(value):
    """值 → 单行短字符串(超长截断)。只在 trace 级别真正输出时才被调用。"""
    try:
        s = value if isinstance(value, str) else repr(value)
    except Exception as e:  # 快照失败不该让业务代码崩
        s = f"<不可快照: {e!r}>"
    s = s.replace("\r\n", " ").replace("\n", " ")
    return s if len(s) <= _SNAP_MAX else s[:_SNAP_MAX] + f"...(+{len(s) - _SNAP_MAX}字符)"


def _kv(fields):
    """关键字参数 → " | k=v | k=v" 片段(空则空串)。只在 trace 输出时才被调用。"""
    return "".join(f" | {k}={_snap(v)}" for k, v in fields.items())


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

    # ponytail: 归档时其他线程仍可能持有 latest.log 句柄(Windows 上 move 会失败)。
    # 现实现 init 只在启动期单线程调用;若将来支持运行中归档需先加全局写锁。
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
    level:   用户日志级别 1~6(默认 4=信息,超界自动夹取)
    enabled: False 时返回空 logger,不写文件(测试友好)
    重复调用不会再次归档(仅首次归档上一运行的 latest.log)。
    """
    global _logger, _log_dir, _current_level, _enabled, _initialized
    with _INIT_LOCK:  # 多线程同时 init 只允许一个进入归档分支
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


# ---------------------------------------------------------------------------
# 普通级别输出
# ---------------------------------------------------------------------------
def log_critical(msg): _log(logging.CRITICAL, msg)
def log_error(msg):    _log(logging.ERROR, msg)
def log_warning(msg):  _log(logging.WARNING, msg)
def log_info(msg):     _log(logging.INFO, msg)
def log_debug(msg):    _log(logging.DEBUG, msg)
def log_trace(msg):    _log(TRACE_LEVEL, msg)


def log_exception(msg, exc=None):
    """记录异常/意外关闭(级别 致命)。exc 为异常对象,None 时取当前异常上下文。"""
    if exc is None:
        tb = _tb.format_exc()
    else:
        tb = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
    log_critical(msg + "\n" + tb)


# ---------------------------------------------------------------------------
# TRACE 埋点(级别 6)。每个函数第一行就是级别闸门:
# 未到 6 级直接 return,不构造任何字符串、不取任何快照。
# 闸门通过后由 logging 的 handler 锁串行写入,多线程记录不会交错撕裂。
# ---------------------------------------------------------------------------
def trace_call(func_name, args=None, kwargs=None, result=None, exc=None):
    """函数调用链 —— 进入(可选一并带返回值/异常)。记录参数快照。"""
    if _current_level < 6 or not _enabled:
        return
    msg = f"TRACE_CALL {func_name} | ENTER"
    if args:
        msg += f" | args={_snap(args)}"
    if kwargs:
        msg += f" | kwargs={_snap(kwargs)}"
    if result is not None:
        msg += f" | → {_snap(result)}"
    if exc is not None:
        msg += f" | EXCEPTION {_snap(exc)}"
    log_trace(msg)


def trace_return(func_name, result=None):
    """函数调用链 —— 返回。记录返回值快照。"""
    if _current_level < 6 or not _enabled:
        return
    msg = f"TRACE_CALL {func_name} | RETURN"
    if result is not None:
        msg += f" | → {_snap(result)}"
    log_trace(msg)


def trace_exception(func_name, exc):
    """函数调用链 —— 抛异常。"""
    if _current_level < 6 or not _enabled:
        return
    log_trace(f"TRACE_CALL {func_name} | EXCEPTION | {_snap(exc)}")


def trace_file_read(path, size=None, lines=None):
    """文件读取:路径 + 字节数 + 行数(缺省的维度不打印)。"""
    if _current_level < 6 or not _enabled:
        return
    info = f"TRACE_FILE_READ {path}"
    if size is not None:
        info += f" ({size} bytes"
        info += f", {lines} lines)" if lines is not None else ")"
    log_trace(info)


def trace_file_write(path, size=None, lines=None):
    """文件写入:路径 + 字节数 + 行数(缺省的维度不打印)。"""
    if _current_level < 6 or not _enabled:
        return
    info = f"TRACE_FILE_WRITE {path}"
    if size is not None:
        info += f" ({size} bytes"
        info += f", {lines} lines)" if lines is not None else ")"
    log_trace(info)


def trace_code_location(phase, detail="", **fields):
    """代码执行位置:阶段标记 + 可选细节。"""
    if _current_level < 6 or not _enabled:
        return
    msg = f"TRACE_LOCATION {phase}"
    if detail:
        msg += f" | {detail}"
    if fields:
        msg += _kv(fields)
    log_trace(msg)


def trace_thread(event, thread=None, **fields):
    """线程活动:线程名/标识/存活状态/守护标志。

    event: submit/start/stop/join/daemon-change 等动作名
    thread: 目标线程对象,None 表示当前线程
    """
    if _current_level < 6 or not _enabled:
        return
    t = thread or threading.current_thread()
    msg = (f"TRACE_THREAD {event} | thread={t.name} | ident={t.ident} "
           f"| alive={t.is_alive()} | daemon={t.daemon}")
    if fields:
        msg += _kv(fields)
    log_trace(msg)


def trace_queue(event, name=None, queue=None, depth=None, **fields):
    """队列活动:队列名/深度/提交与消费。

    event: put/get/task_done/join 等动作名
    name:  队列名;queue 传队列对象则自动取 qsize();depth 直接给深度时优先
    """
    if _current_level < 6 or not _enabled:
        return
    if depth is None and queue is not None:
        try:
            depth = queue.qsize()
        except Exception:  # 非标准队列对象不该拖垮埋点
            depth = "?"
    msg = f"TRACE_QUEUE {name}.{event}" if name else f"TRACE_QUEUE {event}"
    if depth is not None:
        msg += f" | depth={_snap(depth)}"
    if fields:
        msg += _kv(fields)
    log_trace(msg)


def trace_verify_stage(stage, task_id=None, player=None, **fields):
    """后台验证全过程:提交→发送→读日志→判定→重试→完成。

    stage 取 VERIFY_STAGES 之一;其余信息走关键字参数。
    """
    if _current_level < 6 or not _enabled:
        return
    msg = f"TRACE_VERIFY {stage}"
    if task_id is not None:
        msg += f" | task_id={_snap(task_id)}"
    if player is not None:
        msg += f" | player={_snap(player)}"
    if fields:
        msg += _kv(fields)
    log_trace(msg)


# 便捷查询
def current_level():
    return _current_level


def is_enabled():
    return _enabled


if __name__ == "__main__":  # 自检:一轮跑完 6 级 + 全部埋点
    import shutil
    import tempfile
    d = tempfile.mkdtemp()
    init_logger(log_dir=d, level=6)
    log_info("[自检] 开始")
    q = __import__("queue").Queue()
    trace_thread("main")
    q.put("T1")  # 真实入队:trace_queue 只记日志,不会真的操作队列
    trace_queue("put", name="tasks", queue=q, task_id="T1", payload="加入公会")
    trace_call("main.decide", args=("P1",), kwargs={"weekly": True})
    trace_return("main.decide", {"actions": ["加入公会"]})
    trace_exception("main.decide", ValueError("boom"))
    trace_code_location("main.execute", "player=P1", attempt=1)
    trace_file_read("config.json", size=12631, lines=412)
    trace_file_write("reports/r.json", size=88)
    for s in VERIFY_STAGES:
        trace_verify_stage(s, task_id="T1", player="P1")
    q.get()
    trace_queue("get", name="tasks", queue=q, task_id="T1")
    log_debug("[自检] 中间状态")
    log_warning("[自检] 警告")
    log_error("[自检] 错误")
    try:
        raise RuntimeError("自检异常")
    except RuntimeError as e:
        log_exception("[自检] 致命", e)
    log_critical("[自检] 致命")
    with open(os.path.join(d, "latest.log"), encoding="utf-8") as f:
        print(f.read())
    shutil.rmtree(d, ignore_errors=True)