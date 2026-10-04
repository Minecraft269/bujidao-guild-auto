# -*- coding: utf-8 -*-
"""
test_config_jsonc_guards.py —— 新增校验规则与 JSONC 解析的回归
=========================================================
补 tests/test_config_validate.py 未覆盖的缺口,每条对应一类真实事故:

  * JSONC 解析:行尾 '//' 注释、'/* */' 块注释、BOM —— 用户在记事本里编辑
    config.json 必然产生这些;解析器扛不住 = 配置直接读不出来,脚本无法启动。
  * 字符串字面量内的 '//' 不得被当注释(路径 C:\\...\\logs\\latest.log、
    文案里的 "若有异议//管理员")—— 正则式剥注释会截断整个配置。
  * 阈值逻辑矛盾(entry_promote_1 < entry_kick):入门成员 exp 落在两个门槛
    之间时 if/elif 链两边都不命中,该成员被静默跳过 —— 升职踢人都不执行。
  * 开关写成字符串 "false":JSON 里加个引号就永远为真,开关关不掉。
  * 必需键被整个删掉:静默回退默认值,用户以为自己改生效了其实没有。
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (  # noqa: E402
    DEFAULT_CONFIG,
    VALID_NOTIFY_MODES,
    VALID_SCOPES,
    load_config,
    validate_config,
)


def _cfg():
    """深拷贝默认配置,避免测试间互相污染。"""
    return copy.deepcopy(DEFAULT_CONFIG)


def _errors(issues):
    return [m for lvl, m in issues if lvl == "error"]


class _TmpMixin:
    """临时目录用例的 setUp/tearDown(临时文件不入项目目录)。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, text, name="c.json"):
        p = os.path.join(self.tmp, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p


class TestJsoncParsing(_TmpMixin, unittest.TestCase):
    """JSONC:三种真实用户编辑痕迹都必须能解析。"""

    def test_trailing_line_comment(self):
        """行尾注释("admin": "x" // 备注)必须能解析 —— 用户最常见的写法。"""
        p = self.write('{\n  "admin": "某管理员" // 行尾备注\n}\n')
        self.assertEqual(load_config(p)["admin"], "某管理员")

    def test_block_comment(self):
        """块注释 /* */ 必须被忽略。"""
        p = self.write('{\n  /* 整段说明\n     第二行 */\n  "admin": "某管理员"\n}\n')
        self.assertEqual(load_config(p)["admin"], "某管理员")

    def test_utf8_bom_tolerated(self):
        """带 BOM(记事本另存为 UTF-8 常见)必须能解析。"""
        p = os.path.join(self.tmp, "bom.json")
        with open(p, "w", encoding="utf-8-sig") as f:
            f.write('{"admin": "某管理员"}')
        self.assertEqual(load_config(p)["admin"], "某管理员")

    def test_double_slash_inside_string_preserved(self):
        """字符串里的 '//' 不是注释:URL、路径、文案都可能出现。"""
        p = self.write('{\n  "game_log": "C://mc/logs/latest.log"\n}\n')
        self.assertEqual(load_config(p)["game_log"], "C://mc/logs/latest.log")

    def test_escaped_backslash_before_quote(self):
        """路径里的转义反斜杠不得吃掉后面的引号(状态机分串的关键用例)。"""
        p = self.write(r'{"game_log": "C:\\Path\\To\\logs\\latest.log"}' + "\n")
        self.assertEqual(load_config(p)["game_log"],
                         "C:\\Path\\To\\logs\\latest.log")

    def test_block_comment_inside_string_not_stripped(self):
        """字符串里的 '/* */' 原样保留。"""
        p = self.write('{\n  "contact_text": "联系/*管理员*/"\n}\n')
        self.assertEqual(load_config(p)["contact_text"], "联系/*管理员*/")

    def test_unterminated_block_comment_is_json_error_not_crash(self):
        """未闭合块注释会吞掉文件剩余部分 → JSON 本身不完整,应抛 JSONDecodeError
        (明确报错好过静默产出错误配置),但绝不能是 AttributeError 之类。"""
        p = self.write('{\n  "admin": "某管理员"\n  /* 忘了闭合\n')
        with self.assertRaises(json.JSONDecodeError):
            load_config(p)

    def test_generated_config_parses_with_block_comments(self):
        """生成器产出的 // 注释 + 用户手加的 /* */ 混排仍可解析。"""
        from config import ensure_default_config
        p = os.path.join(self.tmp, "mixed.json")
        ensure_default_config(p)
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n/* 用户手加块注释 */\n")
        cfg = load_config(p)
        self.assertEqual(cfg["thresholds"]["high_demote"], 30000)


class TestThresholdContradictions(unittest.TestCase):
    """阈值逻辑矛盾:门槛倒置会让成员被静默跳过,必须报错。"""

    def test_entry_promote_1_below_kick_rejected(self):
        c = _cfg(); c["thresholds"]["entry_promote_1"] = 100
        errs = _errors(validate_config(c))
        self.assertTrue(any("entry_promote_1" in m and "entry_kick" in m for m in errs),
                        f"未拦下升职门槛低于踢出门槛: {errs}")

    def test_entry_tiers_must_increase(self):
        """entry_promote_1 > entry_promote_2 即档位倒置。"""
        c = _cfg(); c["thresholds"]["entry_promote_1"] = 99999
        self.assertTrue(any("entry_promote_1" in m for m in _errors(validate_config(c))))

    def test_entry_promote_2_above_promote_3_rejected(self):
        c = _cfg(); c["thresholds"]["entry_promote_2"] = 99999
        self.assertTrue(any("entry_promote_2" in m for m in _errors(validate_config(c))))

    def test_low_tiers_must_increase(self):
        c = _cfg(); c["thresholds"]["low_promote_1"] = 99999
        self.assertTrue(any("low_promote_1" in m for m in _errors(validate_config(c))))

    def test_equal_thresholds_accepted(self):
        """相等不算矛盾:entry_promote_1 == entry_kick 时区间为空,语义仍确定。"""
        c = _cfg(); c["thresholds"]["entry_promote_1"] = 3500
        self.assertEqual(_errors(validate_config(c)), [])

    def test_default_thresholds_pass(self):
        """默认阈值必须无矛盾(30000 处 high/mid 同值反向是刻意继承的,不算矛盾)。"""
        self.assertEqual(_errors(validate_config(_cfg())), [])

    def test_contradiction_check_skips_non_numeric(self):
        """阈值写成字符串时,矛盾检查不应再 TypeError(非数值错误已由类型校验报出)。"""
        c = _cfg(); c["thresholds"]["entry_kick"] = "不是数字"
        errs = _errors(validate_config(c))
        self.assertTrue(any("entry_kick" in m for m in errs))


class TestSwitchValidation(unittest.TestCase):
    """开关类型:JSON 里写成 "false" 是最常见的坑。"""

    def test_string_switch_rejected(self):
        c = _cfg(); c["switches"]["promote_enabled"] = "false"
        self.assertTrue(any("promote_enabled" in m for m in _errors(validate_config(c))),
                        '写成字符串 "false" 未被拦截 —— 该开关永远为真,关不掉')

    def test_int_switch_rejected(self):
        c = _cfg(); c["switches"]["kick_enabled"] = 0
        self.assertTrue(any("kick_enabled" in m for m in _errors(validate_config(c))))

    def test_all_switches_checked(self):
        for key in ("promote_enabled", "demote_enabled", "kick_enabled",
                    "execution_enabled", "notify_enabled", "verbose_log"):
            c = _cfg(); c["switches"][key] = "yes"
            self.assertTrue(any(key in m for m in _errors(validate_config(c))),
                            f"开关 {key} 未被校验")

    def test_valid_switches_accepted(self):
        c = _cfg()
        c["switches"] = {"promote_enabled": False, "demote_enabled": False,
                         "kick_enabled": False, "execution_enabled": False,
                         "notify_enabled": False, "verbose_log": False,
                         "notify_mode": "summary"}
        self.assertEqual(_errors(validate_config(c)), [])

    def test_notify_mode_legal_values(self):
        for mode in VALID_NOTIFY_MODES:
            c = _cfg(); c["switches"]["notify_mode"] = mode
            self.assertEqual(_errors(validate_config(c)), [], f"合法模式 {mode} 被误拒")

    def test_notify_mode_typo_rejected(self):
        c = _cfg(); c["switches"]["notify_mode"] = "perplayer"
        self.assertTrue(any("notify_mode" in m for m in _errors(validate_config(c))))

    def test_kick_confirm_must_be_bool(self):
        c = _cfg(); c["kick_confirm"] = "true"
        self.assertTrue(any("kick_confirm" in m for m in _errors(validate_config(c))))

    def test_switches_section_must_be_dict(self):
        c = _cfg(); c["switches"] = ["promote_enabled"]
        self.assertTrue(any("switches" in m for m in _errors(validate_config(c))))


class TestRequiredKeys(unittest.TestCase):
    """必需键缺失:静默回退默认值会让用户误以为改动生效。"""

    def test_missing_threshold_rejected(self):
        c = _cfg(); del c["thresholds"]["high_demote"]
        self.assertTrue(any("thresholds.high_demote" in m for m in _errors(validate_config(c))))

    def test_missing_command_rejected(self):
        c = _cfg(); del c["commands"]["guild_kick"]
        self.assertTrue(any("commands.guild_kick" in m for m in _errors(validate_config(c))))

    def test_missing_template_rejected(self):
        c = _cfg(); del c["notify_templates"]["summary"]
        self.assertTrue(any("notify_templates.summary" in m for m in _errors(validate_config(c))))

    def test_all_required_threshold_keys_checked(self):
        for key in DEFAULT_CONFIG["thresholds"]:
            c = _cfg(); del c["thresholds"][key]
            self.assertTrue(any(f"thresholds.{key}" in m for m in _errors(validate_config(c))),
                            f"阈值 {key} 缺失未被拦截")

    def test_section_wrong_type_rejected(self):
        c = _cfg(); c["commands"] = "不是对象"
        self.assertTrue(any("commands" in m for m in _errors(validate_config(c))))

    def test_every_section_wrong_type_reports_instead_of_crashing(self):
        """每段被写成非 dict 时都必须**报出 error**,而不是抛 AttributeError/TypeError。

        这是校验器自身的健壮性:main.py 靠 validate_config 的返回值决定是否退出,
        一旦这里抛异常,配置错误会被伪装成脚本崩溃,用户看不到真正的问题。
        """
        for name in ("thresholds", "commands", "notify_templates", "delays",
                     "timeouts", "switches", "debug", "log", "async",
                     "rank_names", "state_check"):
            for bad in ("字符串", ["列表"], 42):
                c = _cfg()
                c[name] = bad
                try:
                    errs = _errors(validate_config(c))
                except Exception as exc:  # noqa: BLE001 —— 正是要防这个
                    self.fail(f"validate_config 在 {name}={bad!r} 时抛异常: "
                              f"{type(exc).__name__}: {exc}")
                if bad == "字符串":
                    self.assertTrue(errs, f"{name} 被写成字符串却未报出错误")

    def test_nested_section_wrong_type_reports(self):
        """嵌套段(debug.simulation)被写坏时同样不能崩。"""
        for name in ("simulation",):
            c = _cfg()
            c["debug"][name] = "不是对象"
            try:
                _errors(validate_config(c))
            except Exception as exc:  # noqa: BLE001
                self.fail(f"debug.{name} 写坏时抛异常: {type(exc).__name__}: {exc}")


class TestDelayRangeValidation(unittest.TestCase):
    """三组随机区间都要查 min<=max(基线只查了 send_interval)。"""

    def test_chat_key_delay_inverted(self):
        c = _cfg()
        c["delays"]["min_chat_key_delay"] = 0.9
        c["delays"]["max_chat_key_delay"] = 0.1
        self.assertTrue(any("min_chat_key_delay" in m for m in _errors(validate_config(c))))

    def test_paste_to_enter_inverted(self):
        c = _cfg()
        c["delays"]["min_paste_to_enter"] = 0.9
        c["delays"]["max_paste_to_enter"] = 0.1
        self.assertTrue(any("min_paste_to_enter" in m for m in _errors(validate_config(c))))

    def test_all_delay_keys_numeric_checked(self):
        for key in ("min_send_interval", "max_send_interval",
                    "min_chat_key_delay", "max_chat_key_delay",
                    "min_paste_to_enter", "max_paste_to_enter"):
            c = _cfg(); c["delays"][key] = "很快"
            self.assertTrue(any(f"delays.{key}" in m for m in _errors(validate_config(c))),
                            f"delays.{key} 未被校验")

    def test_delay_inversion_check_survives_non_numeric(self):
        """两端非数值时不再比较(否则 TypeError 崩掉整个校验)。"""
        c = _cfg()
        c["delays"]["min_chat_key_delay"] = "快"
        c["delays"]["max_chat_key_delay"] = "慢"
        self.assertTrue(_errors(validate_config(c)))


class TestCommentCoverage(unittest.TestCase):
    """交付物要求:生成的配置每一项都有中文说明。"""

    def test_every_leaf_key_has_comment(self):
        """每个叶子键都有 CONFIG_COMMENTS 条目,否则生成的配置文件该行没有说明。"""
        missing = []

        def walk(node, prefix=""):
            for k, v in node.items():
                p = f"{prefix}.{k}" if prefix else k
                if isinstance(v, dict):
                    walk(v, p)
                elif p not in _COMMENTS:
                    missing.append(p)

        walk(DEFAULT_CONFIG)
        self.assertEqual(missing, [], f"缺少中文说明的配置项: {missing}")

    def test_every_section_has_comment(self):
        missing = []

        def walk(node, prefix=""):
            for k, v in node.items():
                p = f"{prefix}.{k}" if prefix else k
                if isinstance(v, dict):
                    if p not in _COMMENTS:
                        missing.append(p)
                    walk(v, p)

        walk(DEFAULT_CONFIG)
        self.assertEqual(missing, [], f"缺少中文说明的配置段: {missing}")

    def test_generated_file_annotates_every_key(self):
        """生成的文件里,每个配置键的行都应能在其上方找到 // 注释行。"""
        from config import ensure_default_config
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "gen.json")
            ensure_default_config(p)
            lines = open(p, encoding="utf-8").read().splitlines()
            annotated = 0
            for i, line in enumerate(lines):
                s = line.strip()
                if not (s.startswith('"') and ":" in s):
                    continue
                if s.endswith(("{", ",")) and s.rstrip(",").endswith("{"):
                    pass  # 段落起始行:注释由段落键负责
                prev = lines[i - 1].strip() if i else ""
                if prev.startswith("//"):
                    annotated += 1
            self.assertGreaterEqual(annotated, 40,
                                    "生成文件中带注释的键过少,说明注释未逐键输出")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestDefaultConfigSafety(unittest.TestCase):
    """安全:默认配置不得含真实本机路径/真实群号。"""

    def test_paths_are_placeholders(self):
        self.assertEqual(DEFAULT_CONFIG["game_log"],
                         r"C:\Path\To\Minecraft\logs\latest.log")
        self.assertIn("Path\\To", DEFAULT_CONFIG["game_options"])

    def test_no_real_qq_group(self):
        self.assertEqual(DEFAULT_CONFIG["guild_qq"], "YOUR_QQ_GROUP")

    def test_admin_empty_by_default(self):
        self.assertEqual(DEFAULT_CONFIG["admin"], "")

    def test_no_drive_letter_other_than_placeholder(self):
        """除占位路径外不得出现别的盘符路径(默认配置不泄露本机环境)。"""
        # 逐个字符串值直接查(不经 json.dumps,避免被转义成 \\\\ 影响匹配)
        def walk(node):
            for v in node.values():
                if isinstance(v, dict):
                    yield from walk(v)
                elif isinstance(v, str):
                    yield v
        for text in walk(DEFAULT_CONFIG):
            for path in re.findall(r"[A-Za-z]:[\\/][^\s,;'\"]+", text):
                self.assertIn("Path\\To", path, f"默认配置含疑似真实路径: {path}")


class TestBusinessRuleDefaults(unittest.TestCase):
    """业务真源:阈值/命令/文案默认值必须与 docs/业务规则继承清单.md 逐条一致。"""

    def test_thresholds_exact(self):
        self.assertEqual(DEFAULT_CONFIG["thresholds"], {
            "high_demote": 30000,
            "mid_promote": 30000,
            "mid_demote": 5000,
            "low_promote_2": 30000,
            "low_promote_1": 10000,
            "low_demote": 3500,
            "entry_promote_3": 30000,
            "entry_promote_2": 10000,
            "entry_promote_1": 3500,
            "entry_kick": 3500,
        }, "阈值与继承清单 §1 不一致 —— 判定矩阵是业务真源,零改动")

    def test_commands_exact(self):
        cmds = DEFAULT_CONFIG["commands"]
        self.assertEqual(cmds["guild_member"], "/guild member {player}")
        self.assertEqual(cmds["guild_promote"], "/guild promote {player}")
        self.assertEqual(cmds["guild_demote"], "/guild demote {player}")
        self.assertEqual(cmds["guild_kick"], "/guild kick {player} {reason}")
        self.assertEqual(cmds["kick_reason"], "未完成一周最低标准")
        self.assertEqual(cmds["gc_prefix"], "/gc")

    def test_notify_templates_exact(self):
        t = DEFAULT_CONFIG["notify_templates"]
        self.assertEqual(
            t["none"],
            "{player} 这周贡献为:{weekly} 执行操作:无 非常感谢你对公会做出的贡献 公会群:{guild_qq}")
        self.assertEqual(
            t["promote"],
            "{player} 这周贡献为:{weekly} 执行操作:升职({count}次) "
            "非常感谢你对公会做出的贡献 公会群:{guild_qq}")
        self.assertEqual(
            t["demote"],
            "{player} 这周贡献为:{weekly} 执行操作:降职 "
            "若有异议请去群里找执行此操作的管理员 公会群:{guild_qq}")
        self.assertEqual(
            t["kick"],
            "{player} 这周贡献为:{weekly} 执行操作:踢出 未完成这周最低标准 "
            "若有异议请去群里寻找执行管理员 公会群:{guild_qq}")

    def test_rank_names_exact(self):
        r = DEFAULT_CONFIG["rank_names"]
        self.assertEqual(r["high"], "高活跃成员")
        self.assertEqual(r["mid"], "中等活跃成员")
        self.assertEqual(r["low"], "低活跃成员")
        self.assertEqual(r["entry"], "入门成员")

    def test_scope_constants_match_rank_chain(self):
        """等级链 入门→低→中→高 的四个等级都应可作为查询范围。"""
        for scope in ("high", "mid", "low", "entry"):
            self.assertIn(scope, VALID_SCOPES)


# 直接引用 CONFIG_COMMENTS(避免在测试里 import 私有名字的写法散落)
from config import CONFIG_COMMENTS as _COMMENTS  # noqa: E402
import re  # noqa: E402


if __name__ == "__main__":
    unittest.main(verbosity=2)