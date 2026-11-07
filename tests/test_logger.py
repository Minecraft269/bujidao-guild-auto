# -*- coding: utf-8 -*-
"""
test_logger.py —— 分级日志系统单元测试
========================================
覆盖验收清单 C 项:
  * 6 级逐级包含关系(level N 恰好记录 1..N 级内容,1⊂2⊂…⊂6)
  * 英文级别名显示(Critical/Error/Warning/Info/Debug/Trace)
  * TRACE 追踪函数(trace_call/trace_return/trace_exception/
    trace_file_read/trace_file_write/trace_code_location)
  * latest.log 写入、按天 gzip 归档(同天多份序号递增)、log_exception 致命生效
"""

import gzip
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime


from guild_auto.infrastructure import logger  # noqa: E402

# 每级标记与对应写入函数
MARKS = {
    1: "L1致命标记",
    2: "L2严重标记",
    3: "L3警告标记",
    4: "L4信息标记",
    5: "L5调试标记",
    6: "L6追踪标记",
}
WRITERS = {
    1: logger.log_critical,
    2: logger.log_error,
    3: logger.log_warning,
    4: logger.log_info,
    5: logger.log_debug,
    6: logger.log_trace,
}


class TestLevelInclusion(unittest.TestCase):
    """C: 逐级包含关系 level 1⊂2⊂3⊂4⊂5⊂6。"""

    def _read_latest(self, d):
        with open(os.path.join(d, "latest.log"), encoding="utf-8") as f:
            return f.read()

    def test_level_n_contains_exactly_1_to_n(self):
        prev_count = 0
        for lv in range(1, 7):
            d = tempfile.mkdtemp()
            logger._initialized = False
            logger.init_logger(log_dir=d, level=lv, enabled=True)
            for k in range(1, 7):
                WRITERS[k](MARKS[k])
            content = self._read_latest(d)
            recorded = [k for k in range(1, 7) if MARKS[k] in content]
            self.assertEqual(
                recorded, list(range(1, lv + 1)), f"level {lv}: 应记录 1..{lv},实际 {recorded}"
            )
            self.assertGreater(
                len(recorded), prev_count, f"level {lv} 详细程度必须高于 level {lv - 1}"
            )
            prev_count = len(recorded)

    def test_default_level_is_4(self):
        d = tempfile.mkdtemp()
        logger._initialized = False
        logger.init_logger(log_dir=d)  # 不传 level
        self.assertEqual(logger.current_level(), 4)

    def test_level_clamped(self):
        d = tempfile.mkdtemp()
        logger._initialized = False
        logger.init_logger(log_dir=d, level=99)
        self.assertEqual(logger.current_level(), 6)
        logger._initialized = False
        logger.init_logger(log_dir=d, level=-3)
        self.assertEqual(logger.current_level(), 1)


class TestEnglishLevelNames(unittest.TestCase):
    """C: 日志行级别名为英文。"""

    def test_level_names_in_output(self):
        d = tempfile.mkdtemp()
        logger._initialized = False
        logger.init_logger(log_dir=d, level=6, enabled=True)
        for lv, w in WRITERS.items():
            w("x")
        with open(os.path.join(d, "latest.log"), encoding="utf-8") as f:
            content = f.read()
        for name in ("[Critical]", "[Error]", "[Warning]", "[Info]", "[Debug]", "[Trace]"):
            self.assertIn(name, content, f"日志行应含英文级别名 {name}")


class TestTraceFunctions(unittest.TestCase):
    """C: 第 6 级内部活动追踪。"""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        logger._initialized = False
        logger.init_logger(log_dir=self.d, level=6, enabled=True)

    def _read(self):
        with open(os.path.join(self.d, "latest.log"), encoding="utf-8") as f:
            return f.read()

    def test_trace_call_and_return(self):
        logger.trace_call("mod.func", args=(1,), kwargs={"k": "v"}, result={"r": 2})
        logger.trace_return("mod.ret", [1, 2])
        c = self._read()
        self.assertIn("TRACE_CALL mod.func", c)
        self.assertIn("TRACE_CALL mod.ret", c)
        self.assertIn("→", c)

    def test_trace_exception(self):
        try:
            raise ValueError("boom")
        except ValueError as e:
            logger.trace_exception("mod.badfunc", e)
        c = self._read()
        self.assertIn("TRACE_CALL mod.badfunc", c)
        self.assertIn("EXCEPTION", c)

    def test_trace_file_read_write(self):
        logger.trace_file_read("/tmp/a.txt", 123)
        logger.trace_file_write("/tmp/b.txt")
        c = self._read()
        self.assertIn("TRACE_FILE_READ /tmp/a.txt (123 bytes)", c)
        self.assertIn("TRACE_FILE_WRITE /tmp/b.txt", c)

    def test_trace_code_location(self):
        logger.trace_code_location("main.execute_phase.player", "player=P1")
        self.assertIn("TRACE_LOCATION main.execute_phase.player | player=<snip>", self._read())

    def test_trace_silent_below_level6(self):
        d2 = tempfile.mkdtemp()
        logger._initialized = False
        logger.init_logger(log_dir=d2, level=5, enabled=True)
        logger.trace_call("mod.f", result=1)
        with open(os.path.join(d2, "latest.log"), encoding="utf-8") as f:
            c = f.read()
        self.assertNotIn("TRACE_", c, "level 5 不应输出 trace 埋点")


class TestArchive(unittest.TestCase):
    """C: latest.log 归档为 app-YYYY-MM-DD.gz,同天多份序号递增,GZ 内含 .log 内容。"""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.today = datetime.now().strftime("%Y-%m-%d")

    def _reinit(self):
        logger._initialized = False
        return logger.init_logger(log_dir=self.d, level=4, enabled=True)

    def test_first_archive_no_seq(self):
        self._reinit()
        logger.log_info("归档内容ABC")
        self._reinit()  # 新运行 → 归档旧 latest.log
        gz = os.path.join(self.d, f"app-{self.today}.gz")
        self.assertTrue(os.path.exists(gz))
        with gzip.open(gz, "rt", encoding="utf-8") as f:
            self.assertIn("归档内容ABC", f.read())
        # 中间 .log 文件不残留
        raws = [f for f in os.listdir(self.d) if f.endswith(".log") and f != "latest.log"]
        self.assertEqual(raws, [])

    def test_same_day_seq_increment(self):
        self._reinit()
        self._reinit()  # → app-today.gz
        self._reinit()  # → app-today-2.gz
        self._reinit()  # → app-today-3.gz
        for name in (f"app-{self.today}.gz", f"app-{self.today}-2.gz", f"app-{self.today}-3.gz"):
            self.assertTrue(
                os.path.exists(os.path.join(self.d, name)), f"缺少 {name},目录={os.listdir(self.d)}"
            )

    def test_repeated_init_does_not_archive_current_run(self):
        self._reinit()  # 首次 init 时无旧日志可归档
        gzs = [f for f in os.listdir(self.d) if f.endswith(".gz")]
        self.assertEqual(gzs, [], f"首次运行不应产生归档,但发现 {gzs}")

    def test_log_exception_writes_fatal(self):
        self._reinit()
        try:
            raise RuntimeError("崩溃原因XYZ")
        except RuntimeError as e:
            logger.log_exception("脚本意外关闭", e)
        with open(os.path.join(self.d, "latest.log"), encoding="utf-8") as f:
            c = f.read()
        self.assertIn("[Critical]", c)
        self.assertIn("脚本意外关闭", c)
        self.assertIn("RuntimeError", c)
        self.assertIn("崩溃原因XYZ", c)


class TestTraceRedaction(unittest.TestCase):
    """R-16/R-19:TRACE 落盘前必须脱敏(玩家名、命令原文、绝对路径)。

    验收口径:日志与事件流中玩家名按既有脱敏规则(<snip>)处理;
    但 player_tag 的稳定短标识 p-xxxxxx 要保留 —— 并发查询靠它归因。
    """

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="logredact_")
        self.addCleanup(lambda: shutil.rmtree(self.d, ignore_errors=True))
        logger._initialized = False
        logger.init_logger(log_dir=self.d, level=6, enabled=True)

    def _read(self):
        with open(os.path.join(self.d, "latest.log"), encoding="utf-8") as f:
            return f.read()

    def test_player_field_redacted(self):
        logger.log_trace(
            "TRACE_VERIFY send | task_id=7 player='合成玩家甲' cmd='/guild kick 合成玩家甲 原因'"
        )
        c = self._read()
        self.assertNotIn("合成玩家甲", c, f"真实玩家名落盘了: {c}")
        self.assertIn("player=<snip>", c)
        self.assertIn("cmd=<redacted>", c)

    def test_plain_player_field_redacted(self):
        self.assertEqual(
            logger.sanitize_text("player=合成玩家乙 attempts=2"), "player=<snip> attempts=2"
        )

    def test_player_tag_preserved(self):
        tag = logger.player_tag("合成玩家甲")
        self.assertEqual(logger.sanitize_text(f"player={tag}"), f"player={tag}")

    def test_non_player_fields_untouched(self):
        # \b 边界:multiplayer / min_player 不是玩家字段
        self.assertEqual(
            logger.sanitize_text("multiplayer=on min_player=3"), "multiplayer=on min_player=3"
        )

    def test_windows_abs_path_redacted(self):
        logger.trace_file_write(r"D:\Test\Dev\secret\reports\r.md", size=88)
        c = self._read()
        self.assertNotIn("secret", c)
        self.assertIn("TRACE_FILE_WRITE <path>", c)

    def test_long_trace_line_truncated(self):
        logger.log_trace("TRACE_LOCATION x | detail=" + "y" * 600)
        line = [ln for ln in self._read().splitlines() if "TRACE_LOCATION x" in ln][-1]
        self.assertIn("...(+", line)
        self.assertLess(len(line), 500)

    def test_below_trace_level_writes_nothing(self):
        logger._initialized = False
        logger.init_logger(log_dir=self.d, level=4, enabled=True)
        logger.log_trace("TRACE_VERIFY begin | player='合成玩家丙'")
        self.assertNotIn("合成玩家丙", self._read())


if __name__ == "__main__":
    unittest.main()
