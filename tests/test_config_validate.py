# -*- coding: utf-8 -*-
"""
test_config_validate.py —— 配置加载/校验/生成 边界补齐
=======================================================
对应硬性验收标准:核心层 ≥95% 包含「配置加载与校验」。

补齐 config.py 的覆盖率缺口,全部是**真实的用户错误防护**:
  * 配置文件不存在时的默认兜底
  * JSONC 注释剥离与深合并(向前兼容旧配置)
  * validate_config 的每条校验规则(阈值类型/延迟区间/重试次数/查询范围/
    跳过等级/文案模板/命令/聊天键/Debug 跳过阶段/模拟参数/日志级别/异步配置/
    game_log 防呆)
  * save_config 往返(存→读内容一致)

每条校验都对应一类真实事故:
  阈值写成字符串 → 判定崩溃;min>max → 随机延时异常;game_log 指向脚本自身
  logs/latest.log → 脚本读自己的日志,自读自写死循环;日志级别越界 → 日志失效。
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logger  # noqa: E402
import config as config_mod  # noqa: E402
from config import (  # noqa: E402
    DEFAULT_CONFIG,
    VALID_SCOPES,
    apply_cli_overrides,
    console,
    ensure_default_config,
    load_config,
    save_config,
    validate_config,
)

for _n in ("log_debug", "log_info", "log_warning", "log_error",
           "log_critical", "log_trace", "log_exception"):
    if not hasattr(logger, _n):
        setattr(logger, _n, lambda *a, **k: None)


def _cfg():
    """深拷贝默认配置,避免测试间互相污染。"""
    return copy.deepcopy(DEFAULT_CONFIG)


def _errors(issues):
    return [m for lvl, m in issues if lvl == "error"]


def _warnings(issues):
    return [m for lvl, m in issues if lvl == "warning"]


class MagicArgs:
    """最小 argparse 命名空间替身(只带 apply_cli_overrides 会读的键)。"""

    def __init__(self, admin=None, log=None, options=None):
        self.admin = admin
        self.log = log
        self.options = options


class TestLoadConfigFallback(unittest.TestCase):
    """配置文件缺失时的兜底(补 line 453-454 缺口)。"""

    def test_missing_file_returns_defaults(self):
        missing = os.path.join(tempfile.gettempdir(), "不存在的配置_xyz.json")
        if os.path.exists(missing):
            os.remove(missing)
        cfg = load_config(missing)
        self.assertEqual(cfg["thresholds"]["high_demote"], 30000)
        self.assertEqual(cfg["guild_qq"], "YOUR_QQ_GROUP")

    def test_missing_file_returns_independent_copy(self):
        """必须是深拷贝:改返回值不得污染 DEFAULT_CONFIG。"""
        missing = os.path.join(tempfile.gettempdir(), "不存在的配置_abc.json")
        if os.path.exists(missing):
            os.remove(missing)
        cfg = load_config(missing)
        cfg["thresholds"]["high_demote"] = 999999
        self.assertEqual(DEFAULT_CONFIG["thresholds"]["high_demote"], 30000,
                         "返回值污染了 DEFAULT_CONFIG —— 会跨次运行泄漏配置")


class TestJsoncAndDeepMerge(unittest.TestCase):
    """JSONC 注释剥离与深合并(向前兼容旧配置)。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_jsonc_comments_stripped(self):
        """带 // 注释的配置文件必须能解析(用户手改注释是常态)。"""
        p = os.path.join(self.tmp, "c.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write('{\n  // 这是注释\n  "admin": "某管理员",\n'
                    '  "log_level": 5\n}\n')
        cfg = load_config(p)
        self.assertEqual(cfg["admin"], "某管理员")

    def test_partial_config_merged_with_defaults(self):
        """只写一个键,其余必须由默认值补齐(向前兼容)。"""
        p = os.path.join(self.tmp, "partial.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"admin": "只写了这一个键"}, f, ensure_ascii=False)
        cfg = load_config(p)
        self.assertEqual(cfg["admin"], "只写了这一个键")
        self.assertEqual(cfg["thresholds"]["high_demote"], 30000,
                         "缺失的 thresholds 未由默认值补齐")

    def test_nested_override_keeps_siblings(self):
        """嵌套覆盖只改指定键,同级兄弟键保留默认。"""
        p = os.path.join(self.tmp, "nested.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"thresholds": {"high_demote": 12345}}, f, ensure_ascii=False)
        cfg = load_config(p)
        self.assertEqual(cfg["thresholds"]["high_demote"], 12345)
        self.assertEqual(cfg["thresholds"]["entry_kick"], 3500,
                         "覆盖一个阈值时把兄弟阈值冲掉了")


class TestSaveConfigRoundTrip(unittest.TestCase):
    """保存与读取往返(补 line 462-464 缺口)。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_then_load_preserves_values(self):
        """存→读后内容一致,且中文不被转义。"""
        p = os.path.join(self.tmp, "saved.json")
        cfg = _cfg()
        cfg["admin"] = "演示管理员"
        cfg["thresholds"]["mid_demote"] = 4321
        save_config(p, cfg)
        self.assertTrue(os.path.exists(p))
        with open(p, "rb") as f:
            raw = f.read().decode("utf-8")
        self.assertIn("演示管理员", raw, "中文被转义成 \\uXXXX,用户打开配置看不懂")
        self.assertNotIn("\\u", raw)
        back = load_config(p)
        self.assertEqual(back["admin"], "演示管理员")
        self.assertEqual(back["thresholds"]["mid_demote"], 4321)


class TestEnsureDefaultConfig(unittest.TestCase):
    """默认配置生成(交付物要求:每项带详细中文描述)。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_file_with_chinese_comments(self):
        """首次运行必须生成带中文注释的配置文件。"""
        p = os.path.join(self.tmp, "new.json")
        self.assertTrue(ensure_default_config(p), "首次运行未生成配置文件")
        with open(p, "r", encoding="utf-8") as f:
            text = f.read()
        self.assertIn("//", text, "生成的配置没有注释行")

    def test_does_not_overwrite_existing(self):
        """已有配置不得被覆盖(否则用户改的设置会丢)。"""
        p = os.path.join(self.tmp, "exists.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"admin": "用户改过的"}')
        self.assertFalse(ensure_default_config(p), "已存在的配置被覆盖了")
        with open(p, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["admin"], "用户改过的")

    def test_generated_file_is_loadable(self):
        """生成的配置必须能被自己的加载器读回(自洽)。"""
        p = os.path.join(self.tmp, "roundtrip.json")
        ensure_default_config(p)
        cfg = load_config(p)
        self.assertEqual(cfg["thresholds"]["high_demote"], 30000)

    def test_generated_has_no_real_local_paths(self):
        """安全:生成的默认配置不得含真实群号。"""
        p = os.path.join(self.tmp, "safe.json")
        ensure_default_config(p)
        with open(p, "r", encoding="utf-8") as f:
            text = f.read()
        self.assertIn("YOUR_QQ_GROUP", text, "群号占位符缺失,可能被写成了真实群号")


class TestValidateDefaultsPass(unittest.TestCase):
    """默认配置必须零 error(否则用户开箱即报错)。"""

    def test_default_config_has_no_errors(self):
        issues = validate_config(_cfg())
        self.assertEqual(_errors(issues), [],
                         f"默认配置存在校验错误: {_errors(issues)}")


class TestValidateNumericFields(unittest.TestCase):
    """数值型校验(阈值/延时/超时)。"""

    def test_threshold_string_rejected(self):
        """阈值写成字符串必须报错(否则判定时崩溃)。"""
        c = _cfg(); c["thresholds"]["high_demote"] = "30000"
        self.assertTrue(any("thresholds.high_demote" in m for m in _errors(validate_config(c))))

    def test_threshold_negative_rejected(self):
        c = _cfg(); c["thresholds"]["entry_kick"] = -1
        self.assertTrue(any("thresholds.entry_kick" in m for m in _errors(validate_config(c))))

    def test_threshold_bool_rejected(self):
        """bool 是 int 子类,True 不应通过数值校验(补 line 474 缺口)。"""
        c = _cfg(); c["thresholds"]["high_demote"] = True
        self.assertTrue(any("thresholds.high_demote" in m for m in _errors(validate_config(c))))

    def test_all_thresholds_checked(self):
        """每个阈值键都应被校验,不能漏。"""
        for key in DEFAULT_CONFIG["thresholds"]:
            c = _cfg()
            c["thresholds"][key] = "不是数字"
            self.assertTrue(
                any(f"thresholds.{key}" in m for m in _errors(validate_config(c))),
                f"阈值 {key} 未被校验")

    def test_delay_min_greater_than_max_rejected(self):
        """min>max 必须报错(延时区间颠倒会让随机延时异常)。"""
        c = _cfg()
        c["delays"]["min_send_interval"] = 5.0
        c["delays"]["max_send_interval"] = 1.0
        self.assertTrue(any("min_send_interval" in m for m in _errors(validate_config(c))))

    def test_delay_non_numeric_rejected(self):
        for key in ("min_send_interval", "max_send_interval"):
            c = _cfg(); c["delays"][key] = "快"
            self.assertTrue(any(f"delays.{key}" in m for m in _errors(validate_config(c))))

    def test_max_retries_negative_rejected(self):
        c = _cfg(); c["timeouts"]["max_retries"] = -1
        self.assertTrue(any("max_retries" in m for m in _errors(validate_config(c))))

    def test_response_wait_non_numeric_rejected(self):
        c = _cfg(); c["timeouts"]["response_wait"] = None
        self.assertTrue(any("response_wait" in m for m in _errors(validate_config(c))))


class TestValidateScopeAndRanks(unittest.TestCase):
    """查询范围与跳过等级校验。"""

    def test_invalid_scope_rejected(self):
        c = _cfg(); c["query_scope"] = "全部"
        self.assertTrue(any("query_scope" in m for m in _errors(validate_config(c))))

    def test_all_valid_scopes_accepted(self):
        """全部合法范围都必须通过(补 VALID_SCOPES 覆盖)。"""
        for scope in VALID_SCOPES:
            c = _cfg(); c["query_scope"] = scope
            self.assertFalse(any("query_scope" in m for m in _errors(validate_config(c))),
                             f"合法范围 {scope} 被误拒")

    def test_unknown_skip_rank_is_warning_not_error(self):
        """未知等级应是 warning(不阻塞运行),不是 error。"""
        c = _cfg(); c["skip_ranks"] = ["不存在的等级"]
        issues = validate_config(c)
        self.assertTrue(any("skip_ranks" in m for m in _warnings(issues)))
        self.assertFalse(any("skip_ranks" in m for m in _errors(issues)),
                         "未知跳过等级不应阻塞运行")


class TestValidateTemplatesAndCommands(unittest.TestCase):
    """文案模板与命令格式校验(补 502-507/510 缺口)。"""

    def test_empty_template_rejected(self):
        """空文案模板必须报错(否则发空消息)。"""
        c = _cfg(); c["notify_templates"]["promote"] = ""
        self.assertTrue(any("notify_templates" in m for m in _errors(validate_config(c))))

    def test_whitespace_template_rejected(self):
        """纯空白模板也必须拒绝。"""
        c = _cfg(); c["notify_templates"]["kick"] = "   "
        self.assertTrue(any("notify_templates" in m for m in _errors(validate_config(c))))

    def test_non_string_template_rejected(self):
        c = _cfg(); c["notify_templates"]["none"] = 123
        self.assertTrue(any("notify_templates" in m for m in _errors(validate_config(c))))

    def test_empty_command_rejected(self):
        """空命令必须报错(否则会发出残缺指令)。"""
        c = _cfg(); c["commands"]["guild_promote"] = ""
        self.assertTrue(any("commands" in m for m in _errors(validate_config(c))))

    def test_non_string_command_rejected(self):
        c = _cfg(); c["commands"]["guild_member"] = ["不是字符串"]
        self.assertTrue(any("commands" in m for m in _errors(validate_config(c))))

    def test_chat_key_non_string_rejected(self):
        """非 auto 且非字符串的聊天键必须报错。"""
        c = _cfg(); c["chat_key"] = 123
        self.assertTrue(any("chat_key" in m for m in _errors(validate_config(c))))

    def test_chat_key_auto_accepted(self):
        c = _cfg(); c["chat_key"] = "auto"
        self.assertFalse(any("chat_key" in m for m in _errors(validate_config(c))))

    def test_chat_key_string_accepted(self):
        c = _cfg(); c["chat_key"] = "回车"
        self.assertFalse(any("chat_key" in m for m in _errors(validate_config(c))))


class TestValidateDebugSection(unittest.TestCase):
    """Debug 模式配置校验(补 517/523-530/536 缺口)。"""

    def test_invalid_skip_stage_rejected(self):
        c = _cfg(); c.setdefault("debug", {})["skip_stages"] = ["不存在的阶段"]
        self.assertTrue(any("skip_stages" in m for m in _errors(validate_config(c))))

    def test_all_valid_stages_accepted(self):
        for stage in ("list", "member", "decide", "notify", "execute"):
            c = _cfg(); c.setdefault("debug", {})["skip_stages"] = [stage]
            self.assertFalse(any("skip_stages" in m for m in _errors(validate_config(c))),
                             f"合法阶段 {stage} 被误拒")

    def test_players_per_rank_unknown_rank_warning(self):
        """模拟人数里未知等级应是 warning。"""
        c = _cfg()
        c.setdefault("debug", {}).setdefault("simulation", {})["players_per_rank"] = {
            "不存在的等级": 3}
        issues = validate_config(c)
        self.assertTrue(any("players_per_rank" in m for m in _warnings(issues)))

    def test_players_per_rank_negative_rejected(self):
        c = _cfg()
        c.setdefault("debug", {}).setdefault("simulation", {})["players_per_rank"] = {"entry": -1}
        self.assertTrue(any("players_per_rank" in m for m in _errors(validate_config(c))))

    def test_players_per_rank_non_integer_rejected(self):
        c = _cfg()
        c.setdefault("debug", {}).setdefault("simulation", {})["players_per_rank"] = {"entry": "三人"}
        self.assertTrue(any("players_per_rank" in m for m in _errors(validate_config(c))))

    def test_players_per_rank_int_accepted(self):
        """整数形式(所有等级统一人数)必须被接受。"""
        c = _cfg()
        c.setdefault("debug", {}).setdefault("simulation", {})["players_per_rank"] = 5
        self.assertFalse(any("players_per_rank" in m for m in _errors(validate_config(c))))

    def test_players_per_rank_bad_scalar_rejected(self):
        """既非 dict 也非合法 int → 报错(补 line 529-530 缺口)。"""
        c = _cfg()
        c.setdefault("debug", {}).setdefault("simulation", {})["players_per_rank"] = -1
        self.assertTrue(any("players_per_rank" in m for m in _errors(validate_config(c))))

    def test_contribution_range_wrong_length_rejected(self):
        """贡献范围必须是长度为 2 的列表。"""
        c = _cfg()
        c.setdefault("debug", {}).setdefault("simulation", {})["contribution_range"] = [1, 2, 3]
        self.assertTrue(any("contribution_range" in m for m in _errors(validate_config(c))))

    def test_contribution_range_min_gt_max_rejected(self):
        c = _cfg()
        c.setdefault("debug", {}).setdefault("simulation", {})["contribution_range"] = [100, 1]
        self.assertTrue(any("contribution_range" in m for m in _errors(validate_config(c))))

    def test_contribution_range_valid_accepted(self):
        c = _cfg()
        c.setdefault("debug", {}).setdefault("simulation", {})["contribution_range"] = [0, 50000]
        self.assertFalse(any("contribution_range" in m for m in _errors(validate_config(c))))


class TestValidateLogLevel(unittest.TestCase):
    """日志级别必须为 1~6 的整数。"""

    def test_level_zero_rejected(self):
        c = _cfg(); c.setdefault("log", {})["level"] = 0
        self.assertTrue(any("log.level" in m for m in _errors(validate_config(c))))

    def test_level_seven_rejected(self):
        c = _cfg(); c.setdefault("log", {})["level"] = 7
        self.assertTrue(any("log.level" in m for m in _errors(validate_config(c))))

    def test_level_bool_rejected(self):
        """bool 不应通过整数校验。"""
        c = _cfg(); c.setdefault("log", {})["level"] = True
        self.assertTrue(any("log.level" in m for m in _errors(validate_config(c))))

    def test_level_float_rejected(self):
        c = _cfg(); c.setdefault("log", {})["level"] = 4.5
        self.assertTrue(any("log.level" in m for m in _errors(validate_config(c))))

    def test_all_valid_levels_accepted(self):
        for lv in range(1, 7):
            c = _cfg(); c.setdefault("log", {})["level"] = lv
            self.assertFalse(any("log.level" in m for m in _errors(validate_config(c))),
                             f"合法日志级别 {lv} 被误拒")


class TestValidateAsyncConfig(unittest.TestCase):
    """异步配置校验(补 546-551 缺口)。"""

    def test_enabled_non_bool_rejected(self):
        c = _cfg(); c.setdefault("async", {})["enabled"] = "yes"
        self.assertTrue(any("async.enabled" in m for m in _errors(validate_config(c))))

    def test_num_workers_zero_rejected(self):
        """工作线程数必须 >=1(0 个线程 = 永远不消费队列)。"""
        c = _cfg(); c.setdefault("async", {})["num_workers"] = 0
        self.assertTrue(any("num_workers" in m for m in _errors(validate_config(c))))

    def test_num_workers_negative_rejected(self):
        c = _cfg(); c.setdefault("async", {})["num_workers"] = -2
        self.assertTrue(any("num_workers" in m for m in _errors(validate_config(c))))

    def test_max_queue_size_zero_rejected(self):
        c = _cfg(); c.setdefault("async", {})["max_queue_size"] = 0
        self.assertTrue(any("max_queue_size" in m for m in _errors(validate_config(c))))

    def test_valid_async_config_accepted(self):
        c = _cfg(); c["async"] = {"enabled": True, "num_workers": 2, "max_queue_size": 100}
        self.assertFalse(any("async." in m for m in _errors(validate_config(c))))


class TestValidateGameLogSafety(unittest.TestCase):
    """game_log 防呆(补 555-581 缺口)。

    这是真实事故防护:game_log 若指向脚本自身的 logs/latest.log,
    脚本会读自己写的日志 → 归档互相触发 → 自读自写死循环。
    """

    def test_game_log_pointing_at_own_log_dir_rejected(self):
        """指向脚本自身日志目录必须报错。"""
        c = _cfg()
        c["log"] = {"dir": "logs", "level": 4, "enabled": True}
        c["game_log"] = os.path.abspath(os.path.join("logs", "latest.log"))
        self.assertTrue(any("game_log" in m for m in _errors(validate_config(c))),
                        "指向自身日志目录未被拦截 —— 会造成自读自写死循环")

    def test_game_log_exactly_log_dir_rejected(self):
        """正好等于日志目录本身也应报错。"""
        c = _cfg()
        c["log"] = {"dir": "mylogs", "level": 4, "enabled": True}
        c["game_log"] = os.path.abspath("mylogs")
        self.assertTrue(any("game_log" in m for m in _errors(validate_config(c))))

    def test_normal_game_log_accepted(self):
        """正常游戏日志路径必须被接受。"""
        c = _cfg()
        c["log"] = {"dir": "logs", "level": 4, "enabled": True}
        c["game_log"] = r"C:\Path\To\Minecraft\logs\latest.log"
        self.assertFalse(any("game_log" in m for m in _errors(validate_config(c))))

    def test_empty_game_log_skips_check(self):
        """game_log 为空时跳过检查(允许用户稍后填写)。"""
        c = _cfg(); c["game_log"] = ""
        self.assertFalse(any("game_log" in m for m in _errors(validate_config(c))))

    def test_none_game_log_skips_check(self):
        c = _cfg(); c["game_log"] = None
        self.assertFalse(any("game_log" in m for m in _errors(validate_config(c))))


class TestApplyCliOverrides(unittest.TestCase):
    """命令行覆盖(补 588-594 缺口)。"""

    def test_admin_override(self):
        c = _cfg()
        apply_cli_overrides(c, MagicArgs(admin="命令行管理员"))
        self.assertEqual(c["admin"], "命令行管理员")

    def test_log_override(self):
        c = _cfg()
        apply_cli_overrides(c, MagicArgs(log=r"D:\some\latest.log"))
        self.assertEqual(c["game_log"], r"D:\some\latest.log")

    def test_options_override(self):
        c = _cfg()
        apply_cli_overrides(c, MagicArgs(options=r"D:\some\options.txt"))
        self.assertEqual(c["game_options"], r"D:\some\options.txt")

    def test_none_overrides_do_not_clobber(self):
        """未传的参数不得覆盖已有配置。"""
        c = _cfg()
        c["admin"] = "原管理员"
        c["game_log"] = "原路径"
        apply_cli_overrides(c, MagicArgs(admin=None, log=None, options=None))
        self.assertEqual(c["admin"], "原管理员")
        self.assertEqual(c["game_log"], "原路径")


class TestConsole(unittest.TestCase):
    """终端分级输出(补 604-610 缺口)。"""

    def test_console_levels_do_not_raise(self):
        """各级别都应能输出且不抛异常。"""
        for level in ("info", "warning", "error", "debug", "critical", "trace"):
            console(f"测试消息 {level}", level)

    def test_console_default_level(self):
        """不传级别时按默认输出。"""
        console("测试消息")

    def test_console_unknown_level_falls_back(self):
        """未知级别不得抛异常(应降级为默认)。"""
        console("测试消息", "不存在的级别")


if __name__ == "__main__":
    unittest.main(verbosity=2)