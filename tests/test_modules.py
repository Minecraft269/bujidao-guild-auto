# -*- coding: utf-8 -*-
"""
test_modules.py —— 补漏测模块的单元测试
==========================================
覆盖 verification-loop 审查发现的 10 个零引用公开行为:
  * log_watcher: detect_encoding / split_messages / extract_chat_block
  * window_input: resolve_chat_key / read_chat_key_from_options / MC_KEY_TO_PYAUTOGUI 映射
  * reporter: write_contribution_report / write_action_report(双报告格式契约)
  * config: validate_config / ensure_default_config / load_config(JSONC round-trip)
全部用临时目录/合成数据,零真实设备操作。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from log_watcher import (  # noqa: E402
    detect_encoding,
    split_messages,
    extract_chat_block,
    is_block_start_line,
    is_block_end_line,
)
from window_input import (  # noqa: E402
    resolve_chat_key,
    read_chat_key_from_options,
    MC_KEY_TO_PYAUTOGUI,
)
from reporter import write_contribution_report, write_action_report  # noqa: E402
from config import validate_config, ensure_default_config, load_config, DEFAULT_CONFIG  # noqa: E402


class TestDetectEncoding(unittest.TestCase):
    """log_watcher.detect_encoding: utf-8/gbk 检测与回退。"""

    def test_explicit_preferred(self):
        self.assertEqual(detect_encoding("x", preferred="gbk"), "gbk")

    def test_utf8_detected(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "a.log")
        with open(p, "wb") as f:
            f.write("中文日志内容 with ascii tail ".encode("utf-8") * 200)
        self.assertEqual(detect_encoding(p), "utf-8")

    def test_bad_bytes_fall_back_to_gbk_family(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "b.log")
        # 无效 UTF-8 序列(0x80 连续) → 应落入 gbk 族或最终回退 gbk
        with open(p, "wb") as f:
            f.write(b"\x80\x80" + b"plain ascii" * 100)
        self.assertIn(detect_encoding(p), ("gbk", "gb2312"))

    def test_missing_file_returns_gbk(self):
        self.assertEqual(detect_encoding("Z:/no/such/file.log"), "gbk")


class TestMessageSplitAndBlock(unittest.TestCase):
    """log_watcher: 消息切分与公会面板块提取(网易版单物理行 \\n 场景)。"""

    def test_split_messages_by_timestamp_head(self):
        lines = [
            "[148月2026 21:23:46.135] [Render thread/INFO]: first",
            "continuation line no head",
            "[148月2026 21:23:47.000] [Render thread/INFO]: second",
        ]
        msgs = split_messages(lines)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(len(msgs[0]), 2)   # 首条含续行
        self.assertEqual(len(msgs[1]), 1)

    def test_split_messages_no_leading_head_dropped(self):
        msgs = split_messages(["orphan line without head"])
        self.assertEqual(msgs, [])

    def test_is_block_lines(self):
        self.assertTrue(is_block_start_line("--------公会--------"))
        self.assertTrue(is_block_end_line("-" * 30))
        self.assertFalse(is_block_end_line("--short--"))

    def test_extract_chat_block_single_physical_line(self):
        # 网易版:整个面板是单物理行,内部换行是字面 \n(实测 184 人面板单行 2233 字符)
        panel = "\\n".join([
            "--------------------公会---------------------",
            "-- 高活跃成员 --",
            "●玩家A",
            "--------------------------------------------",
        ])
        msg = ["[148月2026 21:00:00.000] [Render thread/INFO]: [CHAT] " + panel]
        block = extract_chat_block(msg)
        self.assertIsNotNone(block)
        self.assertEqual(block[0], "-- 高活跃成员 --")
        self.assertEqual(block[-1], "●玩家A")

    def test_extract_chat_block_multiline_physical(self):
        msg = [
            "[148月2026 21:00:00.000] [Render thread/INFO]: [CHAT] ----公会----",
            "-- 低活跃成员 --",
            "●玩家B",
            "-" * 44,
        ]
        block = extract_chat_block(msg)
        self.assertEqual(block, ["-- 低活跃成员 --", "●玩家B"])

    def test_extract_chat_block_unclosed_returns_none(self):
        msg = ["[t] [INFO]: [CHAT] ----公会----", "content only"]
        self.assertIsNone(extract_chat_block(msg))

    def test_extract_chat_block_no_chat_tag(self):
        self.assertIsNone(extract_chat_block(["[t] plain line"]))


class TestChatKeyResolution(unittest.TestCase):
    """window_input: 键名解析(auto/options.txt/别名/直接名)。"""

    def test_alias_resolution(self):
        cfg = {"chat_key": "回车", "game_options": ""}
        self.assertEqual(resolve_chat_key(cfg), "enter")
        cfg["chat_key"] = "空格"
        self.assertEqual(resolve_chat_key(cfg), "space")

    def test_direct_key_lowercased(self):
        self.assertEqual(resolve_chat_key({"chat_key": "T"}), "t")

    def test_auto_reads_options_txt(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "options.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("music:0.5\nkey_key.chat:key.keyboard.enter\nversion:1.20\n")
        key = resolve_chat_key({"chat_key": "auto", "game_options": p})
        self.assertEqual(key, "enter")

    def test_auto_fallback_t_when_missing(self):
        key = resolve_chat_key({"chat_key": "auto", "game_options": "Z:/none.txt"})
        self.assertEqual(key, "t")

    def test_read_options_gbk_encoded(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "options.txt")
        with open(p, "wb") as f:
            f.write("key_key.chat:key.keyboard.t\n".encode("gbk"))
        self.assertEqual(read_chat_key_from_options(p), "t")

    def test_keymap_covers_letters_digits_fkeys(self):
        self.assertEqual(MC_KEY_TO_PYAUTOGUI["key.keyboard.a"], "a")
        self.assertEqual(MC_KEY_TO_PYAUTOGUI["key.keyboard.9"], "9")
        self.assertEqual(MC_KEY_TO_PYAUTOGUI["key.keyboard.f12"], "f12")


class TestReports(unittest.TestCase):
    """reporter 双报告:命名/分组/联系人/未执行区。"""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_contribution_report_grouping(self):
        recs = [{"player": "P1", "rank": "high", "weekly_total": 25000, "actions": ["demote"]},
                {"player": "P2", "rank": "entry", "weekly_total": 40000, "actions": ["promote"] * 3},
                {"player": "P3", "rank": "mid", "weekly_total": 100, "actions": []}]
        p = write_contribution_report(self.d, "演示公会", recs, "AdminX")
        self.assertTrue(os.path.exists(p))
        c = open(p, encoding="utf-8").read()
        for seg in ("---高活跃---", "---中等活跃---", "---入门成员---",
                    "玩家:P1 周贡献为:25000 执行操作:降职",
                    "玩家:P3 周贡献为:100 执行操作:无"):
            self.assertIn(seg, c)

    def test_action_report_sections_and_contact(self):
        recs = [{"player": "K1", "rank": "entry", "new_rank": "kicked", "weekly_total": 3000,
                 "actions": ["kick"], "trigger": "entry_kick", "executed": True, "error": None},
                {"player": "D1", "rank": "high", "new_rank": "mid", "weekly_total": 20000,
                 "actions": ["demote"], "trigger": "high_demote", "executed": True, "error": None}]
        cfg = {"contact_text": "若有异议联系群管理员{admin}", "admin": "Boss"}
        p = write_action_report(self.d, "演示公会", recs, cfg)
        c = open(p, encoding="utf-8").read()
        self.assertIn("被踢出（1人）", c)
        self.assertIn("被降职（1人）", c)
        self.assertIn("K1", c)
        self.assertIn("若有异议联系群管理员Boss", c)

    def test_action_report_failed_section(self):
        recs = [{"player": "F1", "rank": "mid", "new_rank": "mid", "weekly_total": 500,
                 "actions": ["demote"], "executed": True, "error": "权限不足"}]
        p = write_action_report(self.d, "G", recs, {"contact_text": "c{admin}", "admin": "A"})
        self.assertIn("【未执行】(1人)", open(p, encoding="utf-8").read())


class TestConfigContract(unittest.TestCase):
    """config: JSONC round-trip、深合并向前兼容、校验。"""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.path = os.path.join(self.d, "config.json")

    def test_ensure_default_creates_once(self):
        self.assertTrue(ensure_default_config(self.path))
        self.assertFalse(ensure_default_config(self.path))   # 第二次不重建
        raw = open(self.path, encoding="utf-8").read()
        self.assertIn("// ", raw)                            # JSONC 注释存在

    def test_load_strips_comments_and_merges_defaults(self):
        ensure_default_config(self.path)
        cfg = load_config(self.path)
        self.assertEqual(cfg["thresholds"]["high_demote"], 30000)   # 默认值继承
        self.assertEqual(cfg["log"]["level"], 4)

    def test_load_partial_user_config_keeps_defaults(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"admin": "自定义管理员"}')   # 只有部分键
        cfg = load_config(self.path)
        self.assertEqual(cfg["admin"], "自定义管理员")
        self.assertEqual(cfg["guild_qq"], DEFAULT_CONFIG["guild_qq"])  # 其余取默认

    def test_validate_rejects_bad_values(self):
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg["query_scope"] = "invalid"
        cfg["log"] = {"level": 99}
        issues = validate_config(cfg)
        msgs = [m for _, m in issues]
        self.assertTrue(any("query_scope" in m for m in msgs))
        self.assertTrue(any("log.level" in m for m in msgs))

    def test_validate_accepts_default(self):
        errors = [lvl for lvl, _ in validate_config(json.loads(json.dumps(DEFAULT_CONFIG)))
                  if lvl == "error"]
        self.assertEqual(errors, [])


class TestSecurityHardening(unittest.TestCase):
    """第 3 轮安全修复的回归测试:
      * parsers.validate_player_name 黑名单(伪造面板注入防线)
      * reporter._safe_guild_name 文件名消毒(路径注入防线)
      * LogWatcher 轮转后 seek 末尾(陈旧块复活防线)
    """

    def test_player_name_blacklist(self):
        from parsers import validate_player_name, ParseError
        # 危险名:路径符/●/花括号/控制字符/超长/空 → 全拒
        for bad in (r"..\..\evil", "A●B", "{0.__class__}", "a/b", 'x"y', "", "x" * 49,
                    "tab\tname", "nl\nname"):
            with self.assertRaises(ParseError, msg=f"{bad!r} 应被拒绝"):
                validate_player_name(bad)
        # 真实样本合法名(含空格/中文/下划线)→ 全过
        for good in ("带空格 演示名", "GuildLeader_01", "ViceAdmin_02", "演示玩家甲",
                     "一个超长的演示玩家名字被游戏截断成两行"):
            self.assertEqual(validate_player_name(good), good)

    def test_guild_name_sanitized_for_filename(self):
        from reporter import _safe_guild_name
        s = _safe_guild_name("..\\..\\evil")
        self.assertNotIn("\\", s)
        self.assertNotIn("/", s)
        self.assertFalse(s.startswith("."))
        self.assertEqual(_safe_guild_name("演示公会"), "演示公会")   # 合法名不变
        self.assertEqual(_safe_guild_name(""), "unknown")        # 空兜底
        self.assertEqual(_safe_guild_name(None), "unknown")

    def test_log_rotation_seeks_to_end(self):
        from log_watcher import LogWatcher
        d = tempfile.mkdtemp()
        p = os.path.join(d, "latest.log")
        with open(p, "w", encoding="utf-8") as f:
            f.write("OLDLINE\n" * 10000)
        w = LogWatcher(p)
        w.open()
        # 模拟轮转:文件被截断重写(只剩新内容),旧句柄偏移失效
        with open(p, "w", encoding="utf-8") as f:
            f.write("NEWCONTENT only\n")
        lines = w.read_new_lines(block=False)
        joined = "\n".join(lines)
        self.assertNotIn("OLDLINE", joined, f"陈旧内容复活:{lines[:3]}")
        self.assertIn("NEWCONTENT only", joined)


if __name__ == "__main__":
    unittest.main()
