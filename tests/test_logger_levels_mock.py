# -*- coding: utf-8 -*-
"""
test_logger_levels_mock.py —— logger.py 6 级分级日志系统(硬件交互层 mock 覆盖)
=================================================================================

与 tests/test_logger.py 的分工:那份测「6 级包含关系 + 归档正常流程」,这份补齐
未覆盖区域,并对 mock 策略本身下断言。

Mock 铁律在本模块的落地(见 TestNoDeviceTouch / TestMockPolicy):
  * logger.py 不触碰任何硬件 API —— 无 pyautogui / winsdk / pyperclip / time.sleep。
    test_module_touches_no_device_api 把这条规则固化成断言,谁往里加设备调用谁红。
  * 归档重命名 / gzip 压缩 / 目录创建 / 时间源 全部 patch 成假函数,
    只在 tempfile.mkdtemp() 的临时目录里操作,绝不碰项目 logs/。
  * 低开销闸门(level<6 立即 return)用 _Tripwire 记账证明:
    传进去的对象一次 str()/repr() 都没被调用 → 字符串确实没被拼出来。

每个测试都有实质性断言,且能被变异验证打红(见交付 evidence)。
"""
import gzip
import logging
import os
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logger  # noqa: E402

TODAY = datetime.now().strftime("%Y-%m-%d")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_LOGS = os.path.join(PROJECT_ROOT, "logs")

# 被测的 5 个模块级全局,每个测试前后快照/还原,避免污染其它测试文件
_GLOBALS = ("_logger", "_log_dir", "_current_level", "_enabled", "_initialized")

ALL_TRACE_CALLS = (
    lambda: logger.trace_call("f", args=(1,), kwargs={"k": "v"}, result={"r": 2},
                              exc=ValueError("x")),
    lambda: logger.trace_return("f", [1]),
    lambda: logger.trace_exception("f", ValueError("x")),
    lambda: logger.trace_file_read("a.txt", 10, 2),
    lambda: logger.trace_file_write("b.txt", 10),
    lambda: logger.trace_code_location("phase", "detail", k=1),
    lambda: logger.trace_thread("start", role="worker"),
    lambda: logger.trace_queue("put", name="q", depth=1),
    lambda: logger.trace_verify_stage("submit", task_id="T1"),
)


class _Tripwire:
    """任何 str()/repr()/format() 调用都记账。

    用来证明低开销闸门:level<6 时 trace_* 连参数快照都不做。
    """

    def __init__(self, label="trip"):
        self.label = label
        self.calls = 0

    def __str__(self):
        self.calls += 1
        return self.label

    def __repr__(self):
        self.calls += 1
        return self.label


class _FakeThread:
    """确定性的线程替身,避免真的 start/join 造成时序抖动。"""

    def __init__(self, name, ident, alive=True, daemon=False):
        self.name = name
        self.ident = ident
        self._alive = alive
        self.daemon = daemon

    def is_alive(self):
        return self._alive


class LoggerTempCase(unittest.TestCase):
    """基类:每次测试一个全新临时日志目录 + 全局状态还原 + 项目 logs/ 零污染。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="logger_mock_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self._snap = {k: getattr(logger, k) for k in _GLOBALS}
        self.addCleanup(self._restore)
        self._project_logs_before = self._project_logs_state()

    def _restore(self):
        for h in list(getattr(logger._logger, "handlers", []) or []):
            h.close()
        for k, v in self._snap.items():
            setattr(logger, k, v)

    def _project_logs_state(self):
        try:
            return sorted(os.listdir(PROJECT_LOGS))
        except OSError:
            return None

    def assertProjectLogsUntouched(self):
        """日志必须只落在临时目录 —— 项目 logs/ 一个字节都不能变。"""
        self.assertEqual(self._project_logs_state(), self._project_logs_before,
                         "测试污染了项目 logs/ 目录")

    def init_logger(self, level=6, enabled=True, fresh=True, log_dir=None):
        if fresh:
            logger._initialized = False
        return logger.init_logger(log_dir=log_dir or self.d, level=level, enabled=enabled)

    def read_log(self, name="latest.log"):
        with open(os.path.join(self.d, name), encoding="utf-8") as f:
            return f.read()

    def lines_of(self, name="latest.log"):
        return [ln for ln in self.read_log(name).splitlines() if ln.strip()]

    def messages(self, name="latest.log"):
        """取出每行 [Level] 之后、(+X.XXXs) 之前的正文。"""
        out = []
        for ln in self.lines_of(name):
            body = ln.split("] ", 1)[-1]
            out.append(body.rsplit(" (+", 1)[0])
        return out


# ---------------------------------------------------------------------------
# 一、mock 策略自身:禁止真实设备调用
# ---------------------------------------------------------------------------
class TestMockPolicy(LoggerTempCase):
    """logger.py 属硬件交互层,但它本身不该有任何设备调用。"""

    def test_module_touches_no_device_api(self):
        src = open(os.path.join(PROJECT_ROOT, "logger.py"), encoding="utf-8").read()
        for banned in ("pyautogui", "winsdk", "winrt", "pyperclip", "OcrEngine",
                       "clipboard", "time.sleep", "subprocess"):
            self.assertNotIn(banned, src,
                             f"logger.py 出现 {banned}:logger 不允许真实设备/进程调用")

    def test_every_test_writes_only_to_temp_dir(self):
        self.init_logger(level=6)
        for call in ALL_TRACE_CALLS:
            call()
        self.assertTrue(os.path.exists(os.path.join(self.d, "latest.log")))
        self.assertProjectLogsUntouched()
        self.assertEqual(logger._log_dir, self.d)


# ---------------------------------------------------------------------------
# 二、低开销闸门:level<6 / disabled 时立即 return
# ---------------------------------------------------------------------------
class TestLevelGate(LoggerTempCase):
    """硬要求:当前级别 <6 时 trace_* 不产生任何输出、不做任何字符串拼接。"""

    def test_no_output_below_level6(self):
        self.init_logger(level=5)
        for call in ALL_TRACE_CALLS:
            call()
        self.assertEqual(self.read_log(), "", "level 5 下 trace_* 必须零输出")
        self.assertProjectLogsUntouched()

    @mock.patch.object(logger, "log_trace")
    def test_log_trace_never_called_below_level6(self, fake_log_trace):
        for lv in (1, 2, 3, 4, 5):
            with self.subTest(level=lv):
                fake_log_trace.reset_mock()
                self.init_logger(level=lv)
                for call in ALL_TRACE_CALLS:
                    call()
                self.assertEqual(fake_log_trace.call_count, 0,
                                 f"level={lv} 时 trace_* 不得下沉到 log_trace")

    def test_no_snapshot_below_level6(self):
        """闸门是真 return,不是「构造完再丢弃」。"""
        self.init_logger(level=5)
        wire = _Tripwire()
        with mock.patch.object(logger, "log_trace"):
            logger.trace_call("f", args=(wire,), kwargs={"k": wire},
                              result=wire, exc=wire)
            logger.trace_return("f", wire)
            logger.trace_exception("f", wire)
            logger.trace_file_read("p", 1, 1)
            logger.trace_code_location("ph", wire, k=wire)
            logger.trace_thread("e", role=wire)
            logger.trace_queue("put", name="q", depth=wire)
            logger.trace_verify_stage("submit", task_id=wire)
        self.assertEqual(wire.calls, 0,
                         "level<6 时不得对参数做任何 str/repr(字符串被提前拼了)")

    def test_no_output_when_disabled_even_at_level6(self):
        self.init_logger(level=6, enabled=False)
        for call in ALL_TRACE_CALLS:
            call()
        self.assertFalse(os.path.exists(os.path.join(self.d, "latest.log")),
                         "enabled=False 时不得创建 latest.log")
        self.assertProjectLogsUntouched()

    def test_no_snapshot_when_disabled(self):
        """enabled=False 必须在拼字符串之前就 return。

        只断言「没写文件」不够:init_logger 已把阈值抬到 CRITICAL+1,
        即使闸门失效,log_trace 也被 logging 挡下、文件照样是空的 —— 那样闸门就是死代码。
        这里改用记账探针证明 _enabled 判断真的在起作用。
        """
        self.init_logger(level=6, enabled=False)
        wire = _Tripwire()
        with mock.patch.object(logger, "log_trace") as fake_log_trace:
            logger.trace_call("f", args=(wire,), kwargs={"k": wire},
                              result=wire, exc=wire)
            logger.trace_return("f", wire)
            logger.trace_exception("f", wire)
            logger.trace_file_read("p", 1, 1)
            logger.trace_code_location("ph", wire, k=wire)
            logger.trace_thread("e", role=wire)
            logger.trace_queue("put", name="q", depth=wire)
            logger.trace_verify_stage("submit", task_id=wire)
        self.assertEqual(wire.calls, 0,
                         "enabled=False 时不得对参数做任何 str/repr")
        self.assertEqual(fake_log_trace.call_count, 0,
                         "enabled=False 时 trace_* 不得下沉到 log_trace")

    def test_disabled_writes_no_file_for_any_level(self):
        self.init_logger(level=1, enabled=False)
        logger.log_critical("致命")
        logger.log_error("严重")
        logger.log_warning("警告")
        logger.log_info("信息")
        logger.log_debug("调试")
        logger.log_trace("追踪")
        logger.log_exception("异常", ValueError("x"))
        self.assertEqual(os.listdir(self.d), [],
                         "enabled=False 必须零文件写入")
        self.assertFalse(logger.is_enabled())


# ---------------------------------------------------------------------------
# 三、各级别 log_* 的过滤
# ---------------------------------------------------------------------------
class TestLevelFiltering(LoggerTempCase):
    """每级恰好记录 1..N;低开销在普通级别同样成立。"""

    def test_each_level_records_exactly_1_to_n(self):
        marks = {n: f"LV{n}标记" for n in range(1, 7)}
        writers = {
            1: logger.log_critical, 2: logger.log_error, 3: logger.log_warning,
            4: logger.log_info, 5: logger.log_debug, 6: logger.log_trace,
        }
        for lv in range(1, 7):
            d = tempfile.mkdtemp(prefix="logger_lv_")
            self.addCleanup(shutil.rmtree, d, True)
            logger._initialized = False
            logger.init_logger(log_dir=d, level=lv)
            for n, w in writers.items():
                w(marks[n])
            with open(os.path.join(d, "latest.log"), encoding="utf-8") as f:
                content = f.read()
            recorded = [n for n in range(1, 7) if marks[n] in content]
            self.assertEqual(recorded, list(range(1, lv + 1)),
                             f"level {lv} 应记录 1..{lv},实际 {recorded}")

    def test_level_name_is_english_per_level(self):
        self.init_logger(level=6)
        for w in (logger.log_critical, logger.log_error, logger.log_warning,
                  logger.log_info, logger.log_debug, logger.log_trace):
            w("x")
        content = self.read_log()
        for name in ("[Critical]", "[Error]", "[Warning]", "[Info]", "[Debug]", "[Trace]"):
            self.assertIn(name, content)

    def test_log_without_init_is_noop(self):
        logger._logger = None
        with mock.patch.object(logging.Logger, "log") as fake_log:
            logger.log_critical("无人接收")
            logger.log_trace("无人接收")
        self.assertEqual(fake_log.call_count, 0,
                         "_logger is None 时 _log 必须直接 return")

    def test_level_clamped_to_1_6(self):
        self.init_logger(level=99)
        self.assertEqual(logger.current_level(), 6)
        self.init_logger(level=-3)
        self.assertEqual(logger.current_level(), 1)
        self.init_logger(level="5")  # 字符串数字也要能夹取
        self.assertEqual(logger.current_level(), 5)

    def test_is_enabled_tracks_flag(self):
        self.init_logger(level=4, enabled=True)
        self.assertTrue(logger.is_enabled())
        self.init_logger(level=4, enabled=False)
        self.assertFalse(logger.is_enabled())
        self.init_logger(level=4, enabled=1)  # 假值真化
        self.assertTrue(logger.is_enabled())


# ---------------------------------------------------------------------------
# 四、log_exception 两条路径
# ---------------------------------------------------------------------------
class TestLogException(LoggerTempCase):
    """exc=None 取当前异常上下文;exc=对象用自带 __traceback__。两条都记堆栈。"""

    def test_exc_none_uses_current_context(self):
        self.init_logger(level=1)
        try:
            raise KeyError("缺失键AAA")
        except KeyError:
            logger.log_exception("上下文内记录")
        c = self.read_log()
        self.assertIn("[Critical]", c)
        self.assertIn("上下文内记录", c)
        self.assertIn("Traceback (most recent call last)", c)
        self.assertIn("KeyError", c)
        self.assertIn("缺失键AAA", c)

    def test_exc_none_outside_handler(self):
        self.init_logger(level=1)
        logger.log_exception("无异常上下文")
        c = self.read_log()
        self.assertIn("[Critical]", c)
        self.assertIn("无异常上下文", c)
        self.assertIn("NoneType: None", c, "无上下文时应记 NoneType: None")

    def test_exc_object_carries_its_own_traceback(self):
        self.init_logger(level=1)
        try:
            raise ValueError("对象异常原因BBB")
        except ValueError as e:
            saved = e
        logger.log_exception("事后记录", saved)  # 已离开 except 块
        c = self.read_log()
        self.assertIn("[Critical]", c)
        self.assertIn("事后记录", c)
        self.assertIn("ValueError", c)
        self.assertIn("对象异常原因BBB", c)
        self.assertIn("Traceback (most recent call last)", c)

    def test_exc_object_respects_level_gate(self):
        """log_exception 是致命级:级别 6 时也不能被滤掉。"""
        self.init_logger(level=6)
        try:
            raise RuntimeError("ccc")
        except RuntimeError as e:
            logger.log_exception("致命不可滤", e)
        self.assertIn("[Critical]", self.read_log())


# ---------------------------------------------------------------------------
# 五、_snap / _kv:快照与关键字渲染
# ---------------------------------------------------------------------------
class TestSnapAndKv(LoggerTempCase):
    def test_snap_string_kept_verbatim(self):
        self.assertEqual(logger._snap("P1"), "P1")

    def test_snap_non_string_uses_repr(self):
        self.assertEqual(logger._snap(7), "7")
        self.assertEqual(logger._snap(("a", "b")), "('a', 'b')")
        self.assertEqual(logger._snap({"k": 1}), "{'k': 1}")

    def test_snap_flattens_newlines(self):
        self.assertEqual(logger._snap("a\nb\r\nc"), "a b c")

    def test_snap_truncates_with_char_count(self):
        out = logger._snap("x" * 250)
        self.assertTrue(out.endswith("(+50字符)"), out[-20:])
        self.assertEqual(out[:200], "x" * 200)
        s200 = logger._snap("y" * 200)
        self.assertEqual(s200, "y" * 200, "正好 200 字符不该被截断")

    def test_snap_survives_broken_repr(self):
        class Boom:
            def __repr__(self):
                raise RuntimeError("不能 repr")

        out = logger._snap(Boom())
        self.assertIn("<不可快照:", out)
        self.assertIn("不能 repr", out)

    def test_broken_repr_does_not_break_trace(self):
        class Boom:
            def __repr__(self):
                raise RuntimeError("坏 repr")

        self.init_logger(level=6)
        logger.trace_call("f", result=Boom())  # 不得抛出
        self.assertIn("<不可快照:", self.read_log())

    def test_kv_renders_all_fields(self):
        self.assertEqual(logger._kv({"a": 1, "b": "P1"}), " | a=1 | b=P1")
        self.assertEqual(logger._kv({}), "")


# ---------------------------------------------------------------------------
# 六、trace_call / trace_return / trace_exception:函数调用链
# ---------------------------------------------------------------------------
class TestTraceCallChain(LoggerTempCase):
    def setUp(self):
        super().setUp()
        self.init_logger(level=6)

    def test_trace_call_minimal(self):
        logger.trace_call("mod.f")
        self.assertEqual(self.messages(), ["TRACE_CALL mod.f | ENTER"])

    def test_trace_call_all_dimensions(self):
        logger.trace_call("mod.f", args=("P1",), kwargs={"weekly": True},
                          result={"actions": ["加入公会"]})
        self.assertEqual(self.messages(),
                         ["TRACE_CALL mod.f | ENTER | args=('P1',) | "
                          "kwargs={'weekly': True} | → {'actions': ['加入公会']}"])

    def test_trace_call_with_exception(self):
        logger.trace_call("mod.f", exc=ValueError("boom"))
        self.assertEqual(self.messages(),
                         ["TRACE_CALL mod.f | ENTER | EXCEPTION ValueError('boom')"])

    def test_trace_call_falsy_args_omitted(self):
        """空 args / 空 kwargs / None result 不产生空片段。"""
        logger.trace_call("mod.f", args=(), kwargs={}, result=None)
        self.assertEqual(self.messages(), ["TRACE_CALL mod.f | ENTER"])

    def test_trace_return_with_and_without_result(self):
        logger.trace_return("mod.r", {"ok": True})
        logger.trace_return("mod.r")
        self.assertEqual(self.messages(),
                         ["TRACE_CALL mod.r | RETURN | → {'ok': True}",
                          "TRACE_CALL mod.r | RETURN"])

    def test_trace_exception_body(self):
        logger.trace_exception("mod.bad", KeyError("k"))
        self.assertEqual(self.messages(),
                         ["TRACE_CALL mod.bad | EXCEPTION | KeyError('k')"])


# ---------------------------------------------------------------------------
# 七、trace_file_read / trace_file_write:路径 + 字节数 + 行数
# ---------------------------------------------------------------------------
class TestTraceFile(LoggerTempCase):
    def setUp(self):
        super().setUp()
        self.init_logger(level=6)

    def test_file_read_all_dimensions(self):
        logger.trace_file_read("config.json", 12631, 412)
        self.assertEqual(self.messages(),
                         ["TRACE_FILE_READ config.json (12631 bytes, 412 lines)"])

    def test_file_read_bytes_only(self):
        logger.trace_file_read("a.txt", 123)
        self.assertEqual(self.messages(), ["TRACE_FILE_READ a.txt (123 bytes)"])

    def test_file_read_path_only(self):
        logger.trace_file_read("a.txt")
        self.assertEqual(self.messages(), ["TRACE_FILE_READ a.txt"])

    def test_file_read_zero_size_is_kept(self):
        """0 是有效字节数,不能被当成缺省丢掉。"""
        logger.trace_file_read("empty.txt", 0, 0)
        self.assertEqual(self.messages(),
                         ["TRACE_FILE_READ empty.txt (0 bytes, 0 lines)"])

    def test_file_write_all_dimensions(self):
        logger.trace_file_write("reports/r.json", 88, 3)
        self.assertEqual(self.messages(),
                         ["TRACE_FILE_WRITE reports/r.json (88 bytes, 3 lines)"])

    def test_file_write_bytes_only(self):
        logger.trace_file_write("reports/r.json", 88)
        self.assertEqual(self.messages(), ["TRACE_FILE_WRITE reports/r.json (88 bytes)"])

    def test_file_write_path_only(self):
        logger.trace_file_write("reports/r.json")
        self.assertEqual(self.messages(), ["TRACE_FILE_WRITE reports/r.json"])


# ---------------------------------------------------------------------------
# 八、trace_code_location:代码执行位置
# ---------------------------------------------------------------------------
class TestTraceCodeLocation(LoggerTempCase):
    def setUp(self):
        super().setUp()
        self.init_logger(level=6)

    def test_phase_only(self):
        logger.trace_code_location("main.execute")
        self.assertEqual(self.messages(), ["TRACE_LOCATION main.execute"])

    def test_phase_with_detail(self):
        logger.trace_code_location("main.execute", "player=P1")
        self.assertEqual(self.messages(), ["TRACE_LOCATION main.execute | player=P1"])

    def test_empty_detail_omitted(self):
        logger.trace_code_location("main.execute", "")
        self.assertEqual(self.messages(), ["TRACE_LOCATION main.execute"])

    def test_detail_plus_fields(self):
        logger.trace_code_location("main.execute", "player=P1", attempt=1, retry=False)
        self.assertEqual(self.messages(),
                         ["TRACE_LOCATION main.execute | player=P1 | "
                          "attempt=1 | retry=False"])

    def test_fields_only(self):
        logger.trace_code_location("main.execute", attempt=2)
        self.assertEqual(self.messages(),
                         ["TRACE_LOCATION main.execute | attempt=2"])


# ---------------------------------------------------------------------------
# 九、trace_thread:线程活动
# ---------------------------------------------------------------------------
class TestTraceThread(LoggerTempCase):
    def setUp(self):
        super().setUp()
        self.init_logger(level=6)

    def test_defaults_to_current_thread(self):
        logger.trace_thread("main")
        msg = self.messages()[0]
        self.assertTrue(msg.startswith("TRACE_THREAD main | "), msg)
        self.assertIn(f"thread={threading.current_thread().name}", msg)
        self.assertIn("alive=True", msg)
        self.assertIn(f"ident={threading.current_thread().ident}", msg)
        self.assertIn("daemon=", msg)

    def test_explicit_thread_fields(self):
        t = _FakeThread("verify-1", 4242, alive=True, daemon=True)
        logger.trace_thread("start", thread=t)
        self.assertEqual(
            self.messages(),
            ["TRACE_THREAD start | thread=verify-1 | ident=4242 | "
             "alive=True | daemon=True"])

    def test_dead_thread_reported_not_alive(self):
        t = _FakeThread("verify-2", 7, alive=False, daemon=False)
        logger.trace_thread("join", thread=t)
        self.assertIn("alive=False", self.messages()[0])

    def test_extra_fields_appended(self):
        t = _FakeThread("w", 1)
        logger.trace_thread("submit", thread=t, queue_depth=3, task="T1")
        self.assertEqual(
            self.messages(),
            ["TRACE_THREAD submit | thread=w | ident=1 | alive=True | "
             "daemon=False | queue_depth=3 | task=T1"])


# ---------------------------------------------------------------------------
# 十、trace_queue:队列活动(提交/消费/深度)
# ---------------------------------------------------------------------------
class TestTraceQueue(LoggerTempCase):
    def setUp(self):
        super().setUp()
        self.init_logger(level=6)

    def test_name_and_event(self):
        logger.trace_queue("put", name="tasks")
        self.assertEqual(self.messages(), ["TRACE_QUEUE tasks.put"])

    def test_without_name(self):
        logger.trace_queue("get")
        self.assertEqual(self.messages(), ["TRACE_QUEUE get"])

    def test_depth_from_queue_object(self):
        import queue as _q

        q = _q.Queue()
        q.put("T1")
        q.put("T2")
        logger.trace_queue("put", name="tasks", queue=q)
        self.assertEqual(self.messages(), ["TRACE_QUEUE tasks.put | depth=2"])

    def test_explicit_depth_wins_over_queue(self):
        import queue as _q

        q = _q.Queue()
        q.put("T1")
        logger.trace_queue("get", name="tasks", queue=q, depth=99)
        self.assertEqual(self.messages(), ["TRACE_QUEUE tasks.get | depth=99"])

    def test_broken_queue_reports_question_mark(self):
        class BadQueue:
            def qsize(self):
                raise NotImplementedError("没有 qsize")

        logger.trace_queue("put", name="tasks", queue=BadQueue())
        self.assertEqual(self.messages(), ["TRACE_QUEUE tasks.put | depth=?"])

    def test_zero_depth_is_printed(self):
        import queue as _q

        logger.trace_queue("get", name="tasks", queue=_q.Queue())
        self.assertEqual(self.messages(), ["TRACE_QUEUE tasks.get | depth=0"])

    def test_fields_appended(self):
        logger.trace_queue("task_done", name="tasks", depth=0, task="T1")
        self.assertEqual(self.messages(),
                         ["TRACE_QUEUE tasks.task_done | depth=0 | task=T1"])

    def test_queue_operations_are_log_only(self):
        """埋点只记日志,绝不真的动队列。"""
        import queue as _q

        q = _q.Queue()
        q.put("T1")
        logger.trace_queue("get", name="tasks", queue=q, task="T1")
        self.assertEqual(q.qsize(), 1, "trace_queue 不得消费队列")
        self.assertEqual(q.get_nowait(), "T1")


# ---------------------------------------------------------------------------
# 十一、trace_verify_stage:后台验证全过程
# ---------------------------------------------------------------------------
class TestTraceVerifyStage(LoggerTempCase):
    def setUp(self):
        super().setUp()
        self.init_logger(level=6)

    def test_full_six_stage_flow(self):
        """提交→发送→读日志→判定→重试→完成 六阶段连续可读。"""
        for s in logger.VERIFY_STAGES:
            logger.trace_verify_stage(s, task_id="T1", player="P1")
        self.assertEqual(self.messages(),
                         [f"TRACE_VERIFY {s} | task_id=T1 | player=P1"
                          for s in ("submit", "send", "read_log", "judge",
                                    "retry", "done")])

    def test_stage_only(self):
        logger.trace_verify_stage("submit")
        self.assertEqual(self.messages(), ["TRACE_VERIFY submit"])

    def test_task_id_only(self):
        logger.trace_verify_stage("retry", task_id="T9")
        self.assertEqual(self.messages(), ["TRACE_VERIFY retry | task_id=T9"])

    def test_player_only(self):
        logger.trace_verify_stage("judge", player="P2")
        self.assertEqual(self.messages(), ["TRACE_VERIFY judge | player=P2"])

    def test_fields_carry_verdict_details(self):
        logger.trace_verify_stage("judge", task_id="T1", ok=True, reason="匹配成功")
        self.assertEqual(
            self.messages(),
            ["TRACE_VERIFY judge | task_id=T1 | ok=True | reason=匹配成功"])

    def test_optional_ids_omitted_when_none(self):
        logger.trace_verify_stage("done", task_id=None, player=None)
        self.assertEqual(self.messages(), ["TRACE_VERIFY done"])


# ---------------------------------------------------------------------------
# 十二、归档:序号递增 / 重复 init / 兜底 / 异常
# ---------------------------------------------------------------------------
class TestArchiveNaming(LoggerTempCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(shutil.rmtree, self.d, True)

    def _seed_gz(self, name, body):
        with gzip.open(os.path.join(self.d, name), "wt", encoding="utf-8") as f:
            f.write(body)

    def test_next_name_increments_from_numbered_archives(self):
        """已有 app-D.gz 与 app-D-2.gz → 下一份 app-D-3.gz。"""
        self._seed_gz(f"app-{TODAY}.gz", "第一份")
        self._seed_gz(f"app-{TODAY}-2.gz", "第二份")
        self.assertEqual(logger._next_archive_name(self.d, TODAY), f"app-{TODAY}-3")

    def test_next_name_first_of_day(self):
        self.assertEqual(logger._next_archive_name(self.d, TODAY), f"app-{TODAY}")

    def test_next_name_ignores_other_days_and_non_gz(self):
        self._seed_gz("app-1999-01-01.gz", "旧日")
        self._seed_gz(f"app-{TODAY}-7.gz", "本周")
        with open(os.path.join(self.d, f"app-{TODAY}-99.log"), "w", encoding="utf-8") as f:
            f.write("残留中间文件")
        self.assertEqual(logger._next_archive_name(self.d, TODAY), f"app-{TODAY}-8")

    def test_end_to_end_third_archive_of_day(self):
        """业务场景 2(端到端):当天第 3 份归档落到 app-D-3.gz,三份内容各自独立。"""
        for i in (1, 2, 3):
            logger._initialized = False               # 每次都算「新进程」
            logger.init_logger(log_dir=self.d, level=4)
            logger.log_info(f"第{i}次运行")
            logger._initialized = False               # 收尾:让下一次循环归档本轮
        logger._initialized = False
        logger.init_logger(log_dir=self.d, level=4)   # 触发第 3 次归档
        for suffix, body in (("", "第1次运行"), ("-2", "第2次运行"),
                             ("-3", "第3次运行")):
            gz = os.path.join(self.d, f"app-{TODAY}{suffix}.gz")
            self.assertTrue(os.path.exists(gz), f"缺少 {gz}，目录={os.listdir(self.d)}")
            with gzip.open(gz, "rt", encoding="utf-8") as f:
                content = f.read()
            self.assertIn(body, content)
            others = [b for b in ("第1次运行", "第2次运行", "第3次运行") if b != body]
            for other in others:
                self.assertNotIn(other, content, f"{gz} 混入了别的运行的内容")

    def test_stale_intermediate_log_is_replaced(self):
        """同名的中间 .log 残留必须先删掉,归档内容不得混入旧残骸。"""
        stale = os.path.join(self.d, f"app-{TODAY}.log")
        with open(stale, "w", encoding="utf-8") as f:
            f.write("上一轮没清干净的垃圾ZZZ")
        with open(os.path.join(self.d, "latest.log"), "w", encoding="utf-8") as f:
            f.write("本轮真实内容")
        logger._initialized = False
        logger.init_logger(log_dir=self.d, level=4)
        with gzip.open(os.path.join(self.d, f"app-{TODAY}.gz"), "rt",
                       encoding="utf-8") as f:
            content = f.read()
        self.assertIn("本轮真实内容", content)
        self.assertNotIn("垃圾ZZZ", content)
        self.assertFalse(os.path.exists(stale), "中间 .log 残留未清理")

    def test_existing_gz_target_never_overwritten(self):
        """极端兜底:目标 .gz 已存在时递增 -x,绝不覆盖。"""
        self._seed_gz(f"app-{TODAY}-2.gz", "既有归档内容QQQ")
        with open(os.path.join(self.d, "latest.log"), "w", encoding="utf-8") as f:
            f.write("新运行内容")
        with mock.patch.object(logger, "_next_archive_name",
                               return_value=f"app-{TODAY}-2"):
            logger._initialized = False
            logger.init_logger(log_dir=self.d, level=4)
        with gzip.open(os.path.join(self.d, f"app-{TODAY}-2.gz"), "rt",
                       encoding="utf-8") as f:
            self.assertEqual(f.read(), "既有归档内容QQQ", "既有归档被覆盖了")
        fallback = os.path.join(self.d, f"app-{TODAY}-2-x.gz")
        self.assertTrue(os.path.exists(fallback), os.listdir(self.d))
        with gzip.open(fallback, "rt", encoding="utf-8") as f:
            self.assertIn("新运行内容", f.read())

    def test_archive_returns_none_without_latest(self):
        self.assertIsNone(logger._archive_old_latest(self.d))

    def test_gz_contains_log_filename_and_content(self):
        with open(os.path.join(self.d, "latest.log"), "w", encoding="utf-8") as f:
            f.write("压缩内容ABC\n")
        name = logger._archive_old_latest(self.d)
        self.assertEqual(name, f"app-{TODAY}.gz")
        with gzip.open(os.path.join(self.d, name), "rt", encoding="utf-8") as f:
            self.assertIn("压缩内容ABC", f.read())
        self.assertFalse(os.path.exists(os.path.join(self.d, f"app-{TODAY}.log")),
                         "中间 .log 未删除")

    def test_already_cleaned_intermediate_is_not_deleted_twice(self):
        """中间 .log 已被外部清掉时,finally 里的 os.remove 不能炸。

        真实场景:多线程/看门狗先一步删了中间 .log,logger 必须容忍文件不存在。
        """
        raw = os.path.join(self.d, f"app-{TODAY}.log")
        with open(os.path.join(self.d, "latest.log"), "w", encoding="utf-8") as f:
            f.write("内容DDD\n")
        state = {"vanished": False}
        real_exists = os.path.exists

        def _exists(path):
            if state["vanished"] and os.fspath(path) == raw:
                return False
            return real_exists(path)

        def _copy_then_vanish(f_in, f_out):
            f_out.write(f_in.read())
            state["vanished"] = True  # 模拟外部把中间 .log 删了

        with mock.patch.object(logger.shutil, "copyfileobj", _copy_then_vanish), \
                mock.patch.object(logger.os.path, "exists", _exists):
            name = logger._archive_old_latest(self.d)
        self.assertEqual(name, f"app-{TODAY}.gz")
        self.assertTrue(state["vanished"], "本用例的前提没生效,分支没走到")
        with gzip.open(os.path.join(self.d, name), "rt", encoding="utf-8") as f:
            self.assertIn("内容DDD", f.read())


class TestArchiveFailure(LoggerTempCase):
    """归档异常路径:失败要抛出来,且不留半成品中间文件。"""

    def test_move_failure_propagates_and_leaves_no_raw(self):
        with open(os.path.join(self.d, "latest.log"), "w", encoding="utf-8") as f:
            f.write("待归档")
        with mock.patch.object(logger.shutil, "move",
                               side_effect=PermissionError("文件被占用")):
            with self.assertRaises(PermissionError):
                logger._archive_old_latest(self.d)
        self.assertFalse(os.path.exists(os.path.join(self.d, f"app-{TODAY}.log")))
        self.assertEqual([n for n in os.listdir(self.d) if n.endswith(".gz")], [])

    def test_gzip_failure_removes_intermediate_log(self):
        with open(os.path.join(self.d, "latest.log"), "w", encoding="utf-8") as f:
            f.write("待压缩")
        with mock.patch.object(logger.gzip, "GzipFile",
                               side_effect=OSError("磁盘满")):
            with self.assertRaises(OSError):
                logger._archive_old_latest(self.d)
        self.assertFalse(os.path.exists(os.path.join(self.d, f"app-{TODAY}.log")),
                         "压缩失败后必须删掉中间 .log,不留残骸")

    def test_makedirs_failure_propagates(self):
        bad = os.path.join(self.d, "logs")
        logger._initialized = False  # 明确起点,不依赖别的测试文件留下的全局状态
        with mock.patch.object(logger.os, "makedirs",
                               side_effect=PermissionError("无权限")):
            with self.assertRaises(PermissionError):
                logger.init_logger(log_dir=bad, level=4)
        self.assertFalse(os.path.exists(bad))
        self.assertFalse(logger._initialized, "makedirs 失败不应标记为已初始化")

    def test_init_survives_already_existing_dir(self):
        logger._initialized = False
        logger.init_logger(log_dir=self.d, level=4)  # 目录已存在(exist_ok)
        logger.log_info("可用")
        self.assertIn("可用", self.read_log())


# ---------------------------------------------------------------------------
# 十三、init_logger 生命周期
# ---------------------------------------------------------------------------
class TestInitLifecycle(LoggerTempCase):
    def test_repeat_init_does_not_archive_current_run(self):
        """业务场景 3:连续两次 init_logger 只归档一次(第二次不误伤本轮)。"""
        logger.init_logger(log_dir=self.d, level=4)   # 已初始化标志由基类 setUp 复位
        logger.log_info("第一次写的内容")
        logger.init_logger(log_dir=self.d, level=4)   # 不复位 _initialized
        logger.log_info("第二次写的内容")
        self.assertEqual([n for n in os.listdir(self.d) if n.endswith(".gz")], [],
                         "同一轮内重复 init 不得产生归档")
        content = self.read_log()
        self.assertIn("第一次写的内容", content, "重复 init 后日志被截断了")
        self.assertIn("第二次写的内容", content)

    def test_fresh_init_archives_previous_run(self):
        logger.init_logger(log_dir=self.d, level=4)
        logger.log_info("上一轮")
        logger._initialized = False               # 模拟新进程
        logger.init_logger(log_dir=self.d, level=4)
        gz = os.path.join(self.d, f"app-{TODAY}.gz")
        self.assertTrue(os.path.exists(gz))
        with gzip.open(gz, "rt", encoding="utf-8") as f:
            self.assertIn("上一轮", f.read())
        self.assertIn(f"[归档] 上一运行日志已压缩: app-{TODAY}.gz", self.read_log())

    def test_disabled_init_sets_level_above_critical(self):
        logger.init_logger(log_dir=self.d, level=4, enabled=False)
        self.assertEqual(logger._logger.level, logging.CRITICAL + 1)
        self.assertEqual(logger._logger.handlers, [])
        self.assertTrue(logger._initialized)

    def test_init_returns_shared_logger_without_propagation(self):
        lg = logger.init_logger(log_dir=self.d, level=4)
        self.assertIs(lg, logging.getLogger("guild_auto"))
        self.assertFalse(lg.propagate, "禁止向上冒泡,避免重复输出到 root")
        self.assertEqual(len(lg.handlers), 1)
        self.assertEqual(lg.level, logging.INFO)

    def test_handlers_not_duplicated_on_reinit(self):
        for lv in (4, 5, 6):
            logger._initialized = False
            logger.init_logger(log_dir=self.d, level=lv)
        self.assertEqual(len(logger._logger.handlers), 1,
                         "重复 init 叠加了 handler,同一行会被写多遍")
        logger.log_info("只写一次")
        self.assertEqual(self.read_log().count("只写一次"), 1)

    def test_threshold_follows_level(self):
        for lv, expect in ((1, logging.CRITICAL), (2, logging.ERROR),
                           (3, logging.WARNING), (4, logging.INFO),
                           (5, logging.DEBUG), (6, logger.TRACE_LEVEL)):
            logger._initialized = False
            lg = logger.init_logger(log_dir=self.d, level=lv)
            self.assertEqual(lg.level, expect, f"level {lv} 阈值映射错误")
            self.assertEqual(lg.handlers[0].level, expect,
                             f"level {lv} 的 handler 阈值未同步")

    def test_concurrent_init_archives_once(self):
        """多线程同时 init 只允许一个进入归档分支。"""
        with open(os.path.join(self.d, "latest.log"), "w", encoding="utf-8") as f:
            f.write("上一轮")
        logger._initialized = False
        errors = []

        def _worker():
            try:
                logger.init_logger(log_dir=self.d, level=4)
            except Exception as e:  # noqa: BLE001 - 测试要收集所有失败
                errors.append(e)

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual([n for n in os.listdir(self.d) if n.endswith(".gz")],
                         [f"app-{TODAY}.gz"], "并发 init 产生了重复归档")


# ---------------------------------------------------------------------------
# 十四、时间偏移 (+X.XXXs) 与格式化器
# ---------------------------------------------------------------------------
class TestTimeOffset(LoggerTempCase):
    def test_relative_offset_format(self):
        with mock.patch.object(logger._time, "monotonic",
                               return_value=logger._START_TIME + 1.5):
            self.init_logger(level=4)
            logger.log_info("带偏移的行")
        line = self.lines_of()[0]
        self.assertIn("(+1.500s)", line)
        self.assertNotIn("(+0.000s)", line)

    def test_offset_zero_at_startup(self):
        with mock.patch.object(logger._time, "monotonic",
                               return_value=logger._START_TIME):
            self.init_logger(level=4)
            logger.log_info("启动瞬间")
        self.assertIn("(+0.000s)", self.lines_of()[0])

    def test_offset_tracks_monotonic_not_wallclock(self):
        """偏移取自 monotonic,不随系统时间跳变。"""
        self.init_logger(level=4)
        with mock.patch.object(logger._time, "monotonic",
                               return_value=logger._START_TIME + 61.25):
            logger.log_info("一分钟后")
        self.assertIn("(+61.250s)", self.lines_of()[0])

    def test_line_carries_timestamp_and_level_and_body(self):
        self.init_logger(level=4)
        logger.log_info("正文内容")
        line = self.lines_of()[0]
        self.assertRegex(
            line, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} "
                  r"\[Info\] 正文内容 \(\+\d+\.\d{3}s\)$")

    def test_unknown_level_keeps_original_name(self):
        """LEVEL_EN 没有的级别保留 logging 原名(Level 25),不吞掉。"""
        fmt = logger._Formatter("%(levelname)s %(message)s")
        rec = logging.LogRecord("guild_auto", 25, __file__, 1, "自定义级别", (), None)
        self.assertEqual(rec.levelname, "Level 25")
        with mock.patch.object(logger._time, "monotonic",
                               return_value=logger._START_TIME + 0.5):
            out = fmt.format(rec)
        self.assertEqual(out, "Level 25 自定义级别 (+0.500s)")
        self.assertEqual(rec.levelname, "Level 25")

    def test_known_level_rewrites_name_in_place(self):
        """命中 LEVEL_EN 的级别名被就地改写成英文显示名。"""
        fmt = logger._Formatter("%(levelname)s %(message)s")
        rec = logging.LogRecord("guild_auto", logger.TRACE_LEVEL, __file__, 1,
                                "追踪正文", (), None)
        self.assertEqual(rec.levelname, "Level 5")
        with mock.patch.object(logger._time, "monotonic",
                               return_value=logger._START_TIME + 2.25):
            out = fmt.format(rec)
        self.assertEqual(out, "Trace 追踪正文 (+2.250s)")
        self.assertEqual(rec.levelname, "Trace")


class TestLevelConstants(unittest.TestCase):
    """6 级语义是业务规格,常量必须锁死。"""

    def test_level_map(self):
        self.assertEqual(logger.LEVEL_MAP, {
            1: logging.CRITICAL, 2: logging.ERROR, 3: logging.WARNING,
            4: logging.INFO, 5: logging.DEBUG, 6: 5,
        })
        self.assertEqual(logger.TRACE_LEVEL, 5)
        self.assertLess(logger.TRACE_LEVEL, logging.DEBUG,
                        "TRACE 必须比 DEBUG 更详细")

    def test_level_names(self):
        self.assertEqual(logger.LEVEL_EN, {
            logging.CRITICAL: "Critical", logging.ERROR: "Error",
            logging.WARNING: "Warning", logging.INFO: "Info",
            logging.DEBUG: "Debug", 5: "Trace",
        })

    def test_verify_stage_order(self):
        self.assertEqual(logger.VERIFY_STAGES,
                         ("submit", "send", "read_log", "judge", "retry", "done"))

    def test_snap_max(self):
        self.assertEqual(logger._SNAP_MAX, 200)


if __name__ == "__main__":
    unittest.main()
