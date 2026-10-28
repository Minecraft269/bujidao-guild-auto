# -*- coding: utf-8 -*-
"""
test_jsonc_parser_edges.py —— JSONC 注释剥离器的边界分支
=======================================================
config.json 是 JSONC(带 // 与 /* */ 注释),用户会手改它。注释剥离器必须
在**损坏输入**下也收敛,不能抛异常或吞掉真实内容 —— 否则用户改错一个
注释就再也启动不了,且错误信息指向不到注释本身。

覆盖的分支:
  * 行注释在文件末尾(没有换行符) → 容忍
  * 块注释未闭合 → 容忍到文件末尾
  * 字符串里的 // 与 /* 不当注释(路径 "C:/Path" 是常见触发点)
  * 字符串里的转义引号 \" 不提前结束字符串
  * 转义符落在文本最后一字符(越界保护)
  * 注释出现在值之后、嵌套对象内部
"""

import json
import os
import shutil
import tempfile
import unittest

from guild_auto.config import load_config
from guild_auto.config.loader import _strip_jsonc


class TestStripJsoncBranches(unittest.TestCase):
    def test_line_comment_at_eof_without_newline(self):
        """文件末尾的行注释没有换行符 —— 不能因此崩,也不能保留注释文本。"""
        out = _strip_jsonc('{"a": 1} // 末尾注释无换行')
        self.assertEqual(json.loads(out.strip()), {"a": 1})

    def test_unclosed_block_comment_tolerated(self):
        """块注释未闭合:容忍到文件末尾,而不是抛异常或死循环。"""
        out = _strip_jsonc('{"a": 1} /* 用户忘了闭合')
        self.assertEqual(json.loads(out.strip()), {"a": 1})

    def test_double_slash_inside_string_is_kept(self):
        """字符串里的 // 不是注释 —— Windows 路径与 URL 都会踩这个。"""
        out = _strip_jsonc('{"path": "C://Path//To//log"}')
        self.assertEqual(json.loads(out), {"path": "C://Path//To//log"})

    def test_block_comment_marker_inside_string_is_kept(self):
        out = _strip_jsonc('{"note": "/* 不是注释 */"}')
        self.assertEqual(json.loads(out), {"note": "/* 不是注释 */"})

    def test_escaped_quote_does_not_end_string(self):
        """字符串里的 \\" 不能提前结束字符串,否则后面的 // 会被当注释删掉。"""
        out = _strip_jsonc(r'{"note": "他说\"好\" // 仍是字符串"}')
        self.assertEqual(json.loads(out), {"note": '他说"好" // 仍是字符串'})

    def test_trailing_backslash_at_eof_does_not_index_error(self):
        """转义符落在最后一字符时不得越界访问 text[i+1]。"""
        _strip_jsonc('{"a": "x\\')  # 不抛 IndexError 即通过

    def test_comment_inside_nested_object(self):
        src = """{
          "outer": {
            // 内层注释
            "inner": 1 /* 行尾块注释 */
          }
        }"""
        self.assertEqual(json.loads(_strip_jsonc(src)), {"outer": {"inner": 1}})

    def test_comment_between_array_elements(self):
        src = "[1, // 一\n 2, /* 二 */ 3]"
        self.assertEqual(json.loads(_strip_jsonc(src)), [1, 2, 3])


class TestLoadConfigWithBrokenComments(unittest.TestCase):
    """端到端:带注释的配置文件能被 load_config 正确读回。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="jsonc_edge_")
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))
        self.path = os.path.join(self.dir, "config.json")

    def _write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_full_config_with_comments_loads(self):
        from guild_auto.config import DEFAULT_CONFIG

        body = json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2)
        # 在开头与中间各插一段注释
        commented = "// 文件头注释\n" + body.replace('"admin"', '// 管理员\n  "admin"', 1)
        self._write(commented)
        cfg = load_config(self.path)
        self.assertEqual(cfg["guild_qq"], DEFAULT_CONFIG["guild_qq"])

    def test_config_with_windows_path_and_comments(self):
        """真实场景:路径里的反斜杠 + 注释并存,不能互相干扰。"""
        from guild_auto.config import DEFAULT_CONFIG

        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg["game_log"] = r"C:\Users\test\.minecraft\logs\latest.log"
        body = json.dumps(cfg, ensure_ascii=False, indent=2)
        self._write("// 游戏日志路径(反斜杠)\n" + body)
        loaded = load_config(self.path)
        self.assertEqual(loaded["game_log"], cfg["game_log"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

class TestConsoleFallbacks(unittest.TestCase):
    """console() 的两个兜底分支：控制台编码失败 / 日志系统不可用。"""

    def test_unicode_encode_error_falls_back_to_buffer(self):
        from unittest import mock
        import sys
        from guild_auto.config import console

        fake_out = mock.MagicMock()
        fake_out.buffer = mock.MagicMock()
        with mock.patch.object(sys, "stdout", fake_out),              mock.patch("builtins.print", side_effect=UnicodeEncodeError("gbk", "x", 0, 1, "bad")):
            console("中文")
        self.assertTrue(fake_out.buffer.write.called)

    def test_logger_unavailable_is_swallowed(self):
        from unittest import mock
        from guild_auto.config import console

        with mock.patch.dict("sys.modules", {"guild_auto.infrastructure.logger": None}):
            console("中文")     # 不得抛异常

