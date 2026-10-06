# -*- coding: utf-8 -*-
"""
test_log_watcher_mock.py —— log_watcher(游戏日志增量监控)mock 覆盖测试
=============================================================================
mock 铁律(本文件零违规):
  * 绝不读真实 latest.log —— 全部指向 tempfile.mkdtemp() 下的临时文件;
  * 绝不真实等待 —— patch log_watcher.time 为 FakeClock,时间由测试推进,sleep 全记录;
  * 绝不 import 真实 main —— `from main import _force_stop` 用 sys.modules 假模块喂,
    两条分支(真值 / 导入失败回退)都能走到;
  * log_watcher 本身不碰 pyautogui / winsdk / pyperclip(纯文件层),故无设备调用。

覆盖的业务场景(与真实客户端对齐):
  1. 编码检测 utf-8 / gbk / gb2312 / 全失败回退 gbk;多处采样防"头 ASCII 中文字尾"误判
  2. open() 跳到末尾(只关心本次运行后的新日志)+ 文件不存在时轮询等待并超时抛错
  3. 轮转(截断/消失)→ 重开;discard_buffer 丢弃前次残留
  4. 块提取:网易版单物理行(内部字面 \\n)与标准 Java 版物理多行; [C] 复制标记两种写法
  5. min_wait_after_start 起步守卫 / stop_check 去抖 / _force_stop(Ctrl+C) 立即中断
  6. max_pending_chars 超限截断(防 pending 无限增长)
"""
import contextlib
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logger as logger_mod  # noqa: E402
import log_watcher as lw  # noqa: E402
from log_watcher import (  # noqa: E402
    LogWatcher,
    detect_encoding,
    is_block_start_line,
    is_block_end_line,
    extract_chat_block,
    _starts_with_block,
)

# ---------------------------------------------------------------------------
# 公共素材(格式取自真实 latest.log:首行带时间戳前缀,面板首尾是分隔线)
# ---------------------------------------------------------------------------
HEAD = "[10月2026 21:23:46.135] [Render thread/INFO] [ChatComponent/]: "
CHAT_TAG = "".join(("[", "CHAT", "]"))     # 避免源码里直接出现裸标签
START_LINE = "-" * 20 + "公会" + "-" * 21
END_LINE = "-" * 44


def chat_line(text):
    """一条物理日志行:时间戳头 + [CHAT] + 内容。"""
    return HEAD + CHAT_TAG + " " + text


def write(path, data):
    """写文件:str 按 utf-8 编码;GBK 场景直接传 bytes(不用 text 模式)。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    with open(path, "wb") as f:
        f.write(data)


def append(path, data):
    """追加写入(模拟游戏持续写 latest.log)。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    with open(path, "ab") as f:
        f.write(data)


class FakeClock:
    """替换 log_watcher.time:时间由测试推进,零真实等待。"""

    def __init__(self, start=1_700_000_000.0):
        self.now = float(start)
        self.sleeps = []
        self.on_sleep = None      # 可选回调:第 n 次 sleep 时模拟"游戏写入"

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(seconds)


class BoomHandle:
    """close() 抛 OSError 的假句柄 —— 验证 close/discard_buffer 吞异常。"""

    def close(self):
        raise OSError("模拟句柄关闭失败")


class FlipFlag:
    """布尔标志:前 n 次判定为 False,之后恒为 True(模拟等待中途按下 Ctrl+C)。"""

    def __init__(self, flip_after):
        self.remaining = flip_after
        self.checks = 0

    def __bool__(self):
        self.checks += 1
        if self.remaining > 0:
            self.remaining -= 1
            return False
        return True


@contextlib.contextmanager
def fake_main(force_stop=False):
    """往 sys.modules 塞一个只有 _force_stop 的假 main,避免 import 真实 main.py。"""

    def _make(value):
        mod = types.ModuleType("main")
        mod._force_stop = value
        return mod

    with mock.patch.dict(sys.modules, {"main": _make(force_stop)}):
        yield


@contextlib.contextmanager
def no_main():
    """让 `from main import _force_stop` 抛 ImportError(覆盖回退分支)。"""
    with mock.patch.dict(sys.modules, {"main": None}):
        yield


class _Base(unittest.TestCase):
    """公共 setUp:临时目录 + 假时钟 + 假 logger(绝不写真实 logs/latest.log)。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lw_mock_")
        self.path = os.path.join(self.dir, "latest.log")
        self.clock = FakeClock()
        self.t0 = self.clock.now
        self.logs = []
        self._patches = [
            mock.patch.object(lw, "time", self.clock),
            mock.patch.object(logger_mod, "log_debug",
                              lambda m: self.logs.append(("debug", str(m)))),
            mock.patch.object(logger_mod, "log_warning",
                              lambda m: self.logs.append(("warning", str(m)))),
            mock.patch.object(logger_mod, "log_trace", lambda *a, **k: None),
            mock.patch.object(logger_mod, "trace_code_location", lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(self._teardown)

    def _teardown(self):
        for p in reversed(self._patches):
            p.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def watcher(self, **kw):
        kw.setdefault("encoding", "utf-8")
        kw.setdefault("poll_interval_ms", 100)
        w = LogWatcher(self.path, **kw)
        self.addCleanup(w.close)
        return w

    def debug_logs(self):
        return [m for lvl, m in self.logs if lvl == "debug"]

    def assert_no_long_sleeps(self):
        """所有 sleep 必须是 50ms 子步(让 SIGINT 在 sleep 边界有机会被处理)。"""
        self.assertTrue(self.clock.sleeps, "轮询循环必须真的 sleep")
        for s in self.clock.sleeps:
            self.assertEqual(s, 0.05, f"sleep 应拆成 0.05s 子步,实际 {s}")


# ===========================================================================
# 1. 编码检测
# ===========================================================================
class TestEncodingDetection(_Base):
    def test_explicit_preferred_skips_detection(self):
        """preferred 非 auto → 直接返回,不碰文件。"""
        self.assertEqual(detect_encoding(self.path, preferred="gbk"), "gbk")
        self.assertEqual(detect_encoding("Z:/绝对不存在的路径", preferred="utf-8"), "utf-8")

    def test_utf8_file(self):
        write(self.path, ("中文 utf-8 日志 " * 500).encode("utf-8"))
        self.assertEqual(detect_encoding(self.path), "utf-8")

    def test_gbk_file(self):
        """网易版客户端是 GBK:utf-8 严格解码失败 → 命中 gbk。"""
        write(self.path, ("公会面板中文内容 " * 500).encode("gbk"))
        self.assertEqual(detect_encoding(self.path), "gbk")

    def test_gb2312_file_lands_in_gbk_family(self):
        """gb2312 字节(gbk 的子集)必须被识别为 gbk 族,绝不能误判 utf-8。"""
        write(self.path, ("公会成员贡献" * 500).encode("gb2312"))
        self.assertIn(detect_encoding(self.path), ("gbk", "gb2312"))

    def test_all_candidates_fail_fallback_gbk(self):
        """0xFF/0xFE 非任何候选编码的合法序列:utf-8/gbk/gb2312 全部严格解码失败
        → 走最后一行 `return "gbk"` 回退(errors=replace 尽力解码中文)。"""
        write(self.path, b"\xff\xfe" * 100 + b"plain ascii" * 100)
        for enc in ("utf-8", "gbk", "gb2312"):
            with self.assertRaises(UnicodeDecodeError,
                                   msg=f"{enc} 竟能解码 0xFF/0xFE,测试前提失效"):
                (b"\xff\xfe" * 100 + b"plain ascii" * 100).decode(enc)
        self.assertEqual(detect_encoding(self.path), "gbk")

    def test_multi_sample_catches_chinese_after_long_ascii_head(self):
        """业务坑:开头 200KB 纯 ASCII、中文只出现在尾部 64KB。
        只采样头部会误判 utf-8 → 中文乱码 → 解析全失败;必须采样多处。"""
        write(self.path, b"A" * 200_000 + ("中文尾巴" * 5000).encode("gbk"))
        self.assertNotEqual(detect_encoding(self.path), "utf-8",
                            "长 ASCII 头 + GBK 尾被误判为 utf-8(中文将乱码)")
        self.assertEqual(detect_encoding(self.path), "gbk")

    def test_empty_file_detects_utf8(self):
        write(self.path, b"")
        self.assertEqual(detect_encoding(self.path), "utf-8")

    def test_missing_file_fallback_gbk(self):
        self.assertEqual(detect_encoding(os.path.join(self.dir, "无此文件.log")), "gbk")


# ===========================================================================
# 2. open / close
# ===========================================================================
class TestOpenClose(_Base):
    def test_open_missing_file_polls_then_raises(self):
        """文件不存在:每秒轮询一次,超过 wait_seconds 抛 FileNotFoundError。"""
        missing = os.path.join(self.dir, "还没生成的.log")
        w = LogWatcher(missing, encoding="utf-8")
        with self.assertRaises(FileNotFoundError) as ctx:
            w.open(wait_seconds=3)
        msg = str(ctx.exception)
        self.assertIn("游戏日志不存在", msg)
        self.assertIn("game_log", msg)
        self.assertEqual(w._fh, None, "open 失败后不得留下半开的句柄")
        self.assertTrue(self.clock.sleeps, "必须轮询 sleep,不能直接抛错")
        self.assertTrue(all(s == 1.0 for s in self.clock.sleeps),
                        f"轮询间隔应为 1.0s,实际 {self.clock.sleeps}")
        self.assertGreaterEqual(self.clock.now - self.t0, 3.0, "必须等到超时才抛")

    def test_open_seeks_to_end_and_reads_only_new_lines(self):
        """业务坑:open 必须跳到末尾,否则会读到 launch 前的旧响应块。"""
        write(self.path, ("launch 前的旧日志 " * 500).encode("utf-8"))
        size = os.path.getsize(self.path)
        w = self.watcher()
        w.open()
        self.assertEqual(w._size, size, "open 后 _size 应等于文件大小(seek 到末尾)")
        self.assertEqual(w.read_new_lines(block=False), [], "末尾之后无新内容时应返回空")
        append(self.path, "本次运行的新日志\n")
        self.assertEqual(w.read_new_lines(block=False), ["本次运行的新日志"])

    def test_open_detects_encoding_when_auto(self):
        write(self.path, ("中文日志 " * 300).encode("gbk"))
        w = LogWatcher(self.path, poll_interval_ms=100)   # encoding 默认 auto
        self.addCleanup(w.close)
        w.open()
        self.assertEqual(w.encoding, "gbk", "auto 模式必须在 open 时把编码定下来")

    def test_close_is_idempotent(self):
        write(self.path, "x\n")
        w = self.watcher()
        w.open()
        self.assertIsNotNone(w._fh)
        w.close()
        self.assertIsNone(w._fh)
        w.close()          # 重复关闭:不抛异常
        self.assertIsNone(w._fh)

    def test_close_swallows_oserror(self):
        """句柄 close() 抛 OSError → 静默吞掉,仍把 _fh 置 None(不泄漏句柄)。"""
        write(self.path, "x\n")
        w = self.watcher()
        w.open()
        w._fh = BoomHandle()
        w.close()
        self.assertIsNone(w._fh)


# ===========================================================================
# 3. discard_buffer / 轮转重开
# ===========================================================================
class TestDiscardAndRotation(_Base):
    def test_discard_buffer_drops_previous_command_residual(self):
        """业务坑:first_command 前丢弃残留,否则把上一次的回显误认成本次响应。"""
        write(self.path, "上一次命令的残留回显\n")
        w = self.watcher()
        w.open()
        append(self.path, "残留回显第二段\n")
        w.discard_buffer()
        self.assertEqual(w._size, os.path.getsize(self.path), "重开后应跳到文件末尾")
        self.assertEqual(w.read_new_lines(block=False), [], "残留必须被丢弃")
        append(self.path, "本次 paste 后的回显\n")
        self.assertEqual(w.read_new_lines(block=False), ["本次 paste 后的回显"])

    def test_discard_buffer_never_opened(self):
        """从未 open 就 discard_buffer:也能重开并跳到末尾。"""
        write(self.path, "残留 A\n残留 B\n")
        w = self.watcher()
        w.discard_buffer()
        self.assertEqual(w._size, os.path.getsize(self.path))
        self.assertEqual(w.read_new_lines(block=False), [])

    def test_discard_buffer_empty_file(self):
        """空文件:不 seek,_size 保持 0(避免 seek 前后状态错乱)。"""
        write(self.path, b"")
        w = self.watcher()
        w.open()
        w.discard_buffer()
        self.assertEqual(w._size, 0)

    def test_discard_buffer_missing_file_resets_size(self):
        """重开抛异常(文件被删)→ 记 debug,_size 归零,不把异常抛给调用方。"""
        write(self.path, "残留\n")
        w = self.watcher()
        w.open()
        w.close()                 # Windows 上必须先放句柄,否则文件删不掉
        os.remove(self.path)
        w.discard_buffer()
        self.assertEqual(w._size, 0)
        self.assertIsNone(w._fh)
        self.assertTrue(any("重开异常" in m for m in self.debug_logs()),
                        f"应记录重开异常日志,实际 {self.debug_logs()}")

    def test_discard_buffer_survives_close_error(self):
        """旧句柄 close() 抛异常也必须继续重开(异常分支不能提前 return)。"""
        write(self.path, "残留内容\n")
        w = self.watcher()
        w.open()
        w._fh = BoomHandle()
        w.discard_buffer()
        self.assertEqual(w._size, os.path.getsize(self.path))
        self.assertEqual(w.read_new_lines(block=False), [])

    def test_reopen_if_rotated_detects_truncate_and_missing(self):
        """轮转判定:文件变小(截断/重建)→ True 并归零 _size;文件不存在 → True。"""
        write(self.path, "A" * 100)
        w = self.watcher()
        w.open()
        self.assertFalse(w._reopen_if_rotated(), "文件未变化时不应重开")
        write(self.path, "B" * 10)            # 被截断重写
        self.assertTrue(w._reopen_if_rotated(), "文件变小应判定为轮转")
        self.assertEqual(w._size, 0)
        self.assertIsNone(w._fh, "轮转时必须先关掉旧句柄")
        os.remove(self.path)
        self.assertTrue(w._reopen_if_rotated(), "文件消失应判定为轮转")
        self.assertIsNone(w._fh)

    def test_read_new_lines_after_truncation_reads_new_file_from_zero(self):
        """轮转后从头读:旧内容不得复活(否则把陈旧面板当成本次响应)。"""
        write(self.path, "OLDLINE\n" * 1000)
        w = self.watcher()
        w.open()
        write(self.path, "NEWCONTENT only\n")
        lines = w.read_new_lines(block=False)
        self.assertEqual(lines, ["NEWCONTENT only"])
        self.assertNotIn("OLDLINE", "\n".join(lines))

    def test_read_new_lines_reopens_when_handle_is_none(self):
        """句柄为 None(如 close 之后)→ 自动重开并从 0 读。"""
        write(self.path, "第一行\n第二行\n")
        w = self.watcher()
        self.assertIsNone(w._fh)
        self.assertEqual(w.read_new_lines(block=False), ["第一行", "第二行"])
        self.assertIsNotNone(w._fh)

    def test_read_new_lines_block_false_returns_empty(self):
        write(self.path, "已有内容\n")
        w = self.watcher()
        w.open()
        self.assertEqual(w.read_new_lines(block=False), [])

    def test_read_new_lines_block_true_polls_until_data(self):
        write(self.path, b"")
        w = self.watcher()
        w.open()

        def _on_sleep(_sec):
            self.clock.on_sleep = None      # 只触发一次
            append(self.path, "轮询到的新行\n")

        self.clock.on_sleep = _on_sleep
        lines = w.read_new_lines(block=True)
        self.assertEqual(lines, ["轮询到的新行"], "block=True 必须轮询到有数据才返回")
        self.assertIn(w.poll, self.clock.sleeps, "空读必须 sleep 一个 poll 周期再试")

    def test_read_new_lines_replace_undecodable_bytes(self):
        """坏字节用 errors='replace' 处理,绝不抛 UnicodeDecodeError。"""
        write(self.path, b"")
        w = self.watcher()
        w.open()
        append(self.path, b"good \xff\xfe bad\n")
        lines = w.read_new_lines(block=False)
        self.assertEqual(len(lines), 1)
        self.assertIn("bad", lines[0])
        self.assertIn("\ufffd", lines[0], "坏字节应被替换成 U+FFFD 而不是抛异常")


# ===========================================================================
# 4. wait_for_chat_block
# ===========================================================================
class TestWaitForChatBlock(_Base):
    def _panel(self):
        return [START_LINE, "-- 高活跃成员 --", "●玩家A", END_LINE + " [C]"]

    def test_returns_block_netEase_single_physical_line(self):
        """网易版:整块面板是【一条物理行】,内部换行是字面 \\n。"""
        write(self.path, b"")
        w = self.watcher()
        w.open()
        append(self.path, chat_line("\\n".join(self._panel())) + "\n")
        with fake_main(False):
            block = w.wait_for_chat_block(timeout=5)
        self.assertEqual(block, ["-- 高活跃成员 --", "●玩家A"])
        self.assertTrue(any("BLOCK 首行" in m for m in self.debug_logs()))

    def test_returns_block_java_multi_physical_lines(self):
        """标准 Java 版:面板是物理多行。"""
        write(self.path, b"")
        w = self.watcher()
        w.open()
        append(self.path, "\n".join([chat_line(START_LINE), "-- 低活跃成员 --",
                                     "●玩家B", END_LINE]) + "\n")
        with fake_main(False):
            block = w.wait_for_chat_block(timeout=5)
        self.assertEqual(block, ["-- 低活跃成员 --", "●玩家B"])

    def test_scans_earlier_messages_before_last_one(self):
        """多条消息:前面某条已是完整面板时必须立刻返回,不等最后一条闭合。"""
        write(self.path, b"")
        w = self.watcher()
        w.open()
        append(self.path, "\n".join([
            chat_line("普通闲聊"),
            chat_line("\\n".join(self._panel())),
            chat_line(START_LINE + "\\n还在写入中的内容"),
        ]) + "\n")
        with fake_main(False):
            block = w.wait_for_chat_block(timeout=5)
        self.assertEqual(block, ["-- 高活跃成员 --", "●玩家A"])

    def test_min_wait_after_start_blocks_early_return(self):
        """业务坑:起步 N 秒内不返回块(防读到 discard 之前的延迟写入)。"""
        write(self.path, b"")
        w = self.watcher()
        w.open()
        append(self.path, chat_line("\\n".join(self._panel())) + "\n")
        with fake_main(False):
            block = w.wait_for_chat_block(timeout=30, min_wait_after_start=1.0)
        self.assertIsNotNone(block, "过了 min_wait 窗口后必须能返回块")
        elapsed = self.clock.now - self.t0
        self.assertGreaterEqual(elapsed, 1.0, f"返回过早:elapsed={elapsed:.2f}s < min_wait")
        self.assertLess(elapsed, 30.0)
        self.assert_no_long_sleeps()

    def test_min_wait_longer_than_timeout_returns_none(self):
        """timeout < min_wait → 起步守卫根本不放行,只能超时返回 None。"""
        write(self.path, b"")
        w = self.watcher()
        w.open()
        append(self.path, chat_line("\\n".join(self._panel())) + "\n")
        with fake_main(False):
            block = w.wait_for_chat_block(timeout=0.4, min_wait_after_start=5.0)
        self.assertIsNone(block, "min_wait 未到且窗口关闭,不应返回块")
        self.assertTrue(any("超时" in m for m in self.debug_logs()),
                        f"应记录超时日志,实际 {self.debug_logs()}")

    def test_stop_check_transient_true_does_not_abort(self):
        """去抖:瞬时 True(False→True→False)不得放弃等待(用户原话"按下暂停键后不暂停")。
        断言 stop_check 在 1 个 poll 窗口内被复检 ceil(poll/0.05) 次 —— 若去抖循环退化成
        单次判定,复检次数会掉到 2(外层门 + 内层 1 次),测试立即变红。"""
        write(self.path, b"")
        w = self.watcher(poll_interval_ms=100)
        w.open()
        append(self.path, chat_line("\\n".join(self._panel())) + "\n")
        calls = {"n": 0}

        def stop_check():
            calls["n"] += 1
            return calls["n"] == 1          # 只有外层门那一次 True,随后一直 False

        with fake_main(False):
            block = w.wait_for_chat_block(timeout=5, stop_check=stop_check)
        self.assertIsNotNone(block, "瞬时 stop_check=True 不该放弃等待")
        # 外层门 1 次 + 去抖窗口内 ceil(100/50)=2 次复检
        self.assertGreaterEqual(calls["n"], 3,
                                f"去抖窗口内必须反复复检 stop_check,实际只调了 {calls['n']} 次")
        self.assertEqual(calls["n"], 3, f"复检次数应恰为 3(poll=100ms/50ms 子步),实际 {calls['n']}")
        self.assert_no_long_sleeps()

    def test_stop_check_persistent_true_returns_none(self):
        """持续 True 超过 1 个 poll 周期 → 放弃等待返回 None。"""
        write(self.path, b"")
        w = self.watcher()
        w.open()
        append(self.path, chat_line("\\n".join(self._panel())) + "\n")
        with fake_main(False):
            block = w.wait_for_chat_block(timeout=60, stop_check=lambda: True)
        self.assertIsNone(block, "持续暂停必须放弃等待")
        self.assertTrue(any("持续暂停中断" in m for m in self.debug_logs()),
                        f"应记录中断日志,实际 {self.debug_logs()}")
        self.assertLess(self.clock.now - self.t0, 10, "命中去抖后必须立即返回,不能死等")

    def test_max_pending_chars_truncates_pending(self):
        """超长面板(未闭合)不能让 pending 无限增长:截断到末尾 200 行。"""
        write(self.path, b"")
        w = self.watcher()
        w.open()
        body = [chat_line(START_LINE)] + [f"第{i}行" for i in range(300)]
        append(self.path, "\n".join(body) + "\n")

        seen = []
        real_split = lw.split_messages

        def spy(lines):
            seen.append(len(lines))
            return real_split(lines)

        with mock.patch.object(lw, "split_messages", spy):
            with fake_main(False):
                block = w.wait_for_chat_block(timeout=2, max_pending_chars=200)
        self.assertIsNone(block, "面板未闭合不应返回块")
        self.assertGreater(len(seen), 1, "应至少轮询两轮才能观察到截断")
        self.assertEqual(seen[0], 301, "首轮 pending 应是全部 301 行")
        self.assertEqual(seen[1], 200, "超限后 pending 必须截断到末尾 200 行")

    def test_force_stop_returns_none_immediately(self):
        """业务坑:Ctrl+C(_force_stop)时等待响应块必须立即中断,不能死等。"""
        write(self.path, b"")
        w = self.watcher()
        w.open()
        append(self.path, chat_line(START_LINE + "\\n未闭合内容") + "\n")
        with fake_main(True):
            block = w.wait_for_chat_block(timeout=60)
        self.assertIsNone(block, "_force_stop 时必须返回 None")
        self.assertLess(self.clock.now - self.t0, 10, "_force_stop 必须立即返回而非等满 timeout")
        self.assertTrue(any("_force_stop" in m for m in self.debug_logs()),
                        f"应记录 _force_stop 日志,实际 {self.debug_logs()}")

    def test_force_stop_import_failure_falls_back_to_false(self):
        """`from main import _force_stop` 失败 → 回退 False,照常轮询到超时。"""
        write(self.path, b"")
        w = self.watcher()
        w.open()
        append(self.path, chat_line("普通聊天,不含面板") + "\n")
        with no_main():
            block = w.wait_for_chat_block(timeout=0.4)
        self.assertIsNone(block, "没有面板块时应超时返回 None")
        self.assertGreaterEqual(self.clock.now - self.t0, 0.4,
                                "回退 False 后必须正常轮询到超时,不能提前放弃")

    def test_pending_keeps_only_head_of_plain_chat_message(self):
        """普通聊天只保留消息头(防止 pending 无限增长);面板写入中则完整保留。"""
        write(self.path, b"")
        w = self.watcher()
        w.open()
        append(self.path, "\n".join([chat_line("普通闲聊 A"), "  续行1", "  续行2"]) + "\n")
        append(self.path, "\n".join([chat_line(START_LINE),
                                     "面板行1", "面板行2", "面板行3"]) + "\n")
        seen = []
        real_split = lw.split_messages

        def spy(lines):
            seen.append(list(lines))
            return real_split(lines)

        with mock.patch.object(lw, "split_messages", spy):
            with fake_main(False):
                block = w.wait_for_chat_block(timeout=0.4)
        self.assertIsNone(block, "未闭合面板不应返回块")
        self.assertTrue(any(len(s) > 1 for s in seen),
                        "存在面板消息时 pending 必须保留多行(否则块中间行会永久丢失)")


# ===========================================================================
# 5. read_until
# ===========================================================================
class TestReadUntil(_Base):
    def test_collects_lines_within_window(self):
        write(self.path, b"")
        w = self.watcher()
        w.open()
        append(self.path, "回显一\n回显二\n")
        with fake_main(False):
            out = w.read_until(timeout=0.2)
        self.assertEqual(out, ["回显一", "回显二"])
        self.assertTrue(any("回显收集完成" in m for m in self.debug_logs()))
        self.assert_no_long_sleeps()

    def test_force_stop_breaks_immediately(self):
        write(self.path, b"")
        w = self.watcher()
        w.open()
        append(self.path, "回显一\n")
        with fake_main(True):
            out = w.read_until(timeout=60)
        self.assertEqual(out, [], "第一轮就应因 _force_stop 中断,一条也不收")
        self.assertLess(self.clock.now - self.t0, 10, "_force_stop 必须立即 break")

    def test_force_stop_flip_midwait_rechecks_inside_sleep(self):
        """_force_stop 在等待中途置位:内层 sleep 循环必须复检才能提前 break。"""
        write(self.path, b"")
        w = self.watcher(poll_interval_ms=300)
        w.open()
        append(self.path, "回显一\n")
        flag = FlipFlag(flip_after=3)
        with fake_main(flag):
            out = w.read_until(timeout=60)
        self.assertEqual(out, ["回显一"], "已收到的行不能被丢弃")
        self.assertGreaterEqual(flag.checks, 4, "内层 sleep 循环必须反复复检 _force_stop")
        self.assertLess(self.clock.now - self.t0, 0.25,
                        f"应在 0.1s 内提前 break,实际耗时 {self.clock.now - self.t0:.3f}s")

    def test_import_failure_falls_back_and_still_collects(self):
        write(self.path, b"")
        w = self.watcher()
        w.open()
        append(self.path, "回显甲\n")
        with no_main():
            out = w.read_until(timeout=0.2)
        self.assertEqual(out, ["回显甲"], "main 导入失败时回退 _fs=False,仍应正常收集")


# ===========================================================================
# 6. 纯函数边界(块起始/结尾/切分/提取)
# ===========================================================================
class TestBlockHelpers(unittest.TestCase):
    def test_start_line_accepts_both_copy_marker_forms(self):
        """[C] 复制标记两种写法(带空格 / 不带空格)都要能识别为块起始。"""
        self.assertTrue(is_block_start_line(START_LINE))
        self.assertTrue(is_block_start_line(START_LINE + " [C]"))
        self.assertTrue(is_block_start_line(START_LINE + "[C]"))
        self.assertTrue(is_block_start_line("  " + START_LINE + " [C]  "))

    def test_start_line_rejects_non_dash_content(self):
        self.assertFalse(is_block_start_line("----公会---- 标题"))
        self.assertFalse(is_block_start_line("普通聊天行"))
        self.assertFalse(is_block_start_line(""))
        self.assertFalse(is_block_start_line("公 会 "))

    def test_end_line_length_threshold_and_marker(self):
        self.assertTrue(is_block_end_line("-" * 20))
        self.assertTrue(is_block_end_line("-" * 44 + " [C]"))
        self.assertTrue(is_block_end_line("-" * 44 + "[C]"))
        self.assertFalse(is_block_end_line("-" * 19), "短于 20 个 - 不算块结尾")
        self.assertFalse(is_block_end_line("--- 标题 ---"), "含非 '-' 字符不算块结尾")
        self.assertFalse(is_block_end_line(""), "空行不算块结尾")

    def test_extract_block_skips_leading_blank_lines(self):
        """[CHAT] 与面板起始线之间的空行必须被跳过(否则首行判不出块)。"""
        msg = [chat_line(""), "", "", START_LINE, "●玩家C", END_LINE]
        block = extract_chat_block(msg)
        self.assertEqual(block, ["●玩家C"])

    def test_extract_block_strips_copy_marker_on_start_line(self):
        """块起始线自带 [C] 复制标记(网易版"点击复制"开着时才有):
        标记必须被 is_block_start_line 剥离,否则整块识别失败返回 None。"""
        for marked in (START_LINE + " [C]", START_LINE + "[C]"):
            block = extract_chat_block([chat_line(marked), "●玩家D", END_LINE + " [C]"])
            self.assertEqual(block, ["●玩家D"], f"带复制标记的起始线未被剥离:{marked!r}")

        # 单物理行(网易版)写法同样必须剥离
        panel = "\\n".join([START_LINE + " [C]", "●玩家E", END_LINE + " [C]"])
        self.assertEqual(extract_chat_block([chat_line(panel)]), ["●玩家E"])

    def test_extract_block_strips_copy_marker_on_end_line(self):
        """块结尾线带 [C] 时必须被识别为结尾(否则块不闭合 → None)。"""
        for marked in (END_LINE + " [C]", END_LINE + "[C]"):
            block = extract_chat_block([chat_line(START_LINE), "●玩家F", marked])
            self.assertEqual(block, ["●玩家F"], f"带复制标记的结尾线未被剥离:{marked!r}")

    def test_extract_block_rejects_non_block_head(self):
        """首行不是分隔线 → None(普通聊天消息不误判成面板)。"""
        self.assertIsNone(extract_chat_block([chat_line("普通聊天")]))
        self.assertIsNone(extract_chat_block(
            ["[148月2026 21:00:00.000] [INFO]: 没有聊天标签的行"]))

    def test_extract_block_none_when_no_end_line(self):
        self.assertIsNone(extract_chat_block([chat_line(START_LINE + "\\n只有内容没有结尾")]))

    def test_starts_with_block_boundaries(self):
        self.assertFalse(_starts_with_block(["没有 CHAT 标记的行"]),
                         "无 [CHAT] 标签的消息不得被当成面板")
        self.assertTrue(_starts_with_block([chat_line(START_LINE + "\\n未闭合")]))
        # 面板首线前有空行时,应跳过空行找到首条非空行再判定
        self.assertTrue(_starts_with_block(["头 " + CHAT_TAG, "", "   ", START_LINE]))
        self.assertFalse(_starts_with_block(["头 " + CHAT_TAG, "", "   "]),
                         "[CHAT] 后全为空行 → 无块首线")
        self.assertFalse(_starts_with_block([chat_line("普通聊天")]))


if __name__ == "__main__":
    unittest.main()