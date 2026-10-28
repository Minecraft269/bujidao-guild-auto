# -*- coding: utf-8 -*-
"""
test_ui_binding.py —— UI 配置绑定与校验（纯逻辑）
================================================
UI 配置绑定与校验属核心层（门禁 ≥98%）。本文件覆盖：
  * 配置摊平成字段：种类判定、标签取自 CONFIG_COMMENTS、枚举项带 choices
  * 值渲染：bool / list / dict / 标量
  * 写回：严格类型转换（"abc" 不得静默变 0）、枚举校验、未知路径报错
  * 跨字段校验：写回后仍走 validate_config（如阈值被改成字符串必须报错）
  * 首次运行放行判据：diff_against_default
"""
import json
import unittest

from guild_auto.config import DEFAULT_CONFIG
from guild_auto.config.ui_binding import (
    FieldSpec,
    apply_edits,
    diff_against_default,
    field_by_path,
    flatten_config,
    render_field_value,
)


def _cfg():
    return json.loads(json.dumps(DEFAULT_CONFIG))


class TestFlatten(unittest.TestCase):
    def test_flattens_nested_sections(self):
        fields = flatten_config(_cfg())
        paths = {f.path for f in fields}
        for p in ("admin", "thresholds.high_demote", "switches.kick_enabled",
                  "commands.guild_kick", "ui.enabled", "blocked_name_policy"):
            self.assertIn(p, paths, f"缺少字段 {p}")

    def test_kinds(self):
        by_path = {f.path: f for f in flatten_config(_cfg())}
        self.assertEqual(by_path["switches.kick_enabled"].kind, "bool")
        self.assertEqual(by_path["thresholds.high_demote"].kind, "int")
        self.assertEqual(by_path["admin"].kind, "str")
        self.assertEqual(by_path["skip_ranks"].kind, "str_list")
        self.assertEqual(by_path["rank_names.entry"].kind, "str")  # dict 递归展开为单键

    def test_label_comes_from_comments(self):
        f = field_by_path(_cfg(), "thresholds.high_demote")
        self.assertTrue(f.label, "标签不能为空")
        self.assertTrue(f.comment, "说明不能为空（纪律第 2 条）")

    def test_enum_fields_have_choices(self):
        by_path = {f.path: f for f in flatten_config(_cfg())}
        self.assertEqual(by_path["switches.notify_mode"].choices, ("summary", "per_player"))
        self.assertIsNone(by_path["admin"].choices)

    def test_every_field_has_comment(self):
        """纪律第 2 条：每个配置键都必须有中文说明。"""
        missing = [f.path for f in flatten_config(_cfg()) if not f.comment]
        self.assertEqual(missing, [], f"以下键缺 CONFIG_COMMENTS: {missing}")


class TestRender(unittest.TestCase):
    def test_bool(self):
        self.assertEqual(render_field_value(FieldSpec("k", "l", "c", "bool", True)), "true")
        self.assertEqual(render_field_value(FieldSpec("k", "l", "c", "bool", False)), "false")

    def test_list(self):
        self.assertEqual(
            render_field_value(FieldSpec("k", "l", "c", "str_list", ["甲", "乙"])), "甲, 乙"
        )

    def test_dict(self):
        out = render_field_value(FieldSpec("k", "l", "c", "str_dict", {"a": "1", "b": "2"}))
        self.assertEqual(out, "a=1\nb=2")

    def test_scalar(self):
        self.assertEqual(render_field_value(FieldSpec("k", "l", "c", "int", 42)), "42")


class TestApplyEdits(unittest.TestCase):
    def test_valid_edits_applied(self):
        cfg = _cfg()
        new, errors = apply_edits(cfg, {"admin": "测试管理员", "thresholds.high_demote": "31000"})
        self.assertEqual(errors, [])
        self.assertEqual(new["admin"], "测试管理员")
        self.assertEqual(new["thresholds"]["high_demote"], 31000)
        self.assertEqual(cfg["admin"], "", "原 cfg 不应被就地修改")

    def test_invalid_int_reports_error(self):
        _, errors = apply_edits(_cfg(), {"thresholds.high_demote": "abc"})
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["path"], "thresholds.high_demote")
        self.assertIn("整数", errors[0]["message"])

    def test_invalid_bool_reports_error(self):
        _, errors = apply_edits(_cfg(), {"switches.kick_enabled": "也许"})
        self.assertEqual(len(errors), 1)
        self.assertIn("布尔", errors[0]["message"])

    def test_bool_accepts_chinese(self):
        new, errors = apply_edits(_cfg(), {"switches.kick_enabled": "是"})
        self.assertEqual(errors, [])
        self.assertIs(new["switches"]["kick_enabled"], True)

    def test_enum_rejects_unknown(self):
        _, errors = apply_edits(_cfg(), {"switches.notify_mode": "逐条"})
        self.assertEqual(len(errors), 1)
        self.assertIn("notify_mode", errors[0]["message"])

    def test_list_parsing(self):
        new, errors = apply_edits(_cfg(), {"skip_ranks": "leader, admin"})
        self.assertEqual(errors, [])
        self.assertEqual(new["skip_ranks"], ["leader", "admin"])

    def test_list_chinese_comma(self):
        new, _ = apply_edits(_cfg(), {"blacklist": "甲，乙"})
        self.assertEqual(new["blacklist"], ["甲", "乙"])

    def test_nested_key_edit(self):
        """dict 段递归展开后，每个键都是独立字段。"""
        new, errors = apply_edits(_cfg(), {"rank_names.entry": "新人"})
        self.assertEqual(errors, [])
        self.assertEqual(new["rank_names"]["entry"], "新人")

    def test_unknown_path_rejected(self):
        _, errors = apply_edits(_cfg(), {"no.such.key": 1})
        self.assertEqual(len(errors), 1)
        self.assertIn("未知配置项", errors[0]["message"])

    def test_cross_field_validation_still_runs(self):
        """单字段类型都对，但整体校验失败时也必须报错（如 game_log 指向自身日志）。"""
        # log.level 超出 1-6 时，validate_config 必须报错（path 为空表示整体错误）
        _, errors = apply_edits(_cfg(), {"log.level": "99"})
        self.assertTrue(any("1~6" in e["message"] for e in errors),
                        f"整体校验未生效: {errors}")


class TestNumericListRegression(unittest.TestCase):
    """数值列表（contribution_range）往返必须保持数值类型。

    这是 UI 测试逼出的真实缺陷：渲染成 "0, 50000" 再写回会变成 ["0","50000"]，
    validate_config 直接拒（元素必须为数字）。修复后必须锁死。
    """

    def test_numeric_list_roundtrip_keeps_ints(self):
        cfg = _cfg()
        from guild_auto.config.ui_binding import render_field_value

        field = field_by_path(cfg, "debug.simulation.contribution_range")
        raw = render_field_value(field)
        new, errors = apply_edits(cfg, {"debug.simulation.contribution_range": raw})
        self.assertEqual(errors, [])
        self.assertEqual(new["debug"]["simulation"]["contribution_range"], [0, 50000])
        self.assertIsInstance(new["debug"]["simulation"]["contribution_range"][0], int)

    def test_numeric_list_rejects_non_numeric(self):
        _, errors = apply_edits(_cfg(), {"debug.simulation.contribution_range": "a, b"})
        self.assertEqual(len(errors), 1)
        self.assertIn("数字", errors[0]["message"])

    def test_full_form_roundtrip_is_lossless(self):
        """整张表单原样提交不得产生任何错误（UI 保存的底线）。"""
        from guild_auto.config.ui_binding import render_field_value

        cfg = _cfg()
        edits = {f.path: render_field_value(f) for f in flatten_config(cfg)}
        new, errors = apply_edits(cfg, edits)
        self.assertEqual(errors, [], f"表单往返出错: {errors}")
        self.assertEqual(new, cfg, "往返后配置应完全一致")


class TestFieldSpecAndEdgeBranches(unittest.TestCase):
    """FieldSpec 协议、float 分支、无注释回退、单值列表。"""

    def test_repr_and_eq(self):
        a = FieldSpec("a.b", "标签", "说明", "int", 1)
        b = FieldSpec("a.b", "标签", "说明", "int", 1)
        c = FieldSpec("a.b", "标签", "说明", "int", 2)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertIn("a.b", repr(a))

    def test_label_fallback_without_comment(self):
        cfg = _cfg()
        cfg["自定义无注释键"] = "x"
        f = field_by_path(cfg, "自定义无注释键")
        self.assertEqual(f.label, "自定义无注释键", "无注释时应退回路径末段")

    def test_bool_accepts_real_bool(self):
        new, errors = apply_edits(_cfg(), {"switches.kick_enabled": True})
        self.assertEqual(errors, [])
        self.assertIs(new["switches"]["kick_enabled"], True)

    def test_float_field_ok_and_error(self):
        cfg = _cfg()
        cfg["自定义浮点"] = 0.5
        new, errors = apply_edits(cfg, {"自定义浮点": "1.25"})
        self.assertEqual(errors, [])
        self.assertAlmostEqual(new["自定义浮点"], 1.25)
        _, errors = apply_edits(cfg, {"自定义浮点": "abc"})
        self.assertEqual(len(errors), 1)
        self.assertIn("数字", errors[0]["message"])

    def test_single_value_list_without_separator(self):
        new, errors = apply_edits(_cfg(), {"blacklist": "单个玩家"})
        self.assertEqual(errors, [])
        self.assertEqual(new["blacklist"], ["单个玩家"])

    def test_field_by_path_missing_returns_none(self):
        self.assertIsNone(field_by_path(_cfg(), "不存在.的.键"))


class TestDiffAgainstDefault(unittest.TestCase):
    def test_default_cfg_has_no_diff(self):
        self.assertEqual(diff_against_default(_cfg()), [])

    def test_changed_field_detected(self):
        cfg = _cfg()
        cfg["admin"] = "X"
        self.assertEqual(diff_against_default(cfg), ["admin"])

    def test_forged_syntax_error_detected(self):
        _, errors = apply_edits(_cfg(), {"thresholds.mid_promote": "30000x"})
        self.assertEqual(len(errors), 1)
        self.assertIn("整数", errors[0]["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)