# -*- coding: utf-8 -*-
"""
test_core_gate_branches.py —— 核心层覆盖率门禁的边界分支
=======================================================
core 门禁（≥98%，行+分支合并口径）在 py3.12 上掉到 97.91%：不是功能退化，
而是若干"配置缺键 / 输入畸形 / 生成器未闭合"的防御分支从未被执行。
这些分支恰恰是校验与解析存在的理由 —— 缺键时应当**安静通过**（默认值兜底），
畸形输入时应当**报错而不是抛 AttributeError**。

本文件逐条固定这些边界，让门禁有余量，不再卡在四舍五入的边界上。

覆盖：
  * schema：缺 ui / rules / 名单 / match_wildcard / blocked_name_policy 段时不得误报
  * schema：blacklist 写成字符串、blocked_name_policy 取值非法时必须报错
  * schema：game_log 含 NUL 导致 abspath 抛 ValueError 时校验不崩
  * loader：_dump_jsonc 非 dict 输入、无注释键；_strip_jsonc 未闭合块注释
  * loader：console() 在日志系统异常时仍必须打印
  * ui_binding：str_list 字段接收 list、整体校验只出 warning 时不转成 error
  * parsers：_to_int 不可解析抛 ParseError、块尾残缺名保留
  * rules：ignore 动作不产生动作、无 stop 的多规则累计、同优先级取先命中
  * rules：合法 times 的升职动作通过校验
"""

import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

from guild_auto.config import DEFAULT_CONFIG, validate_config
from guild_auto.config.loader import _dump_jsonc, _strip_jsonc, console
from guild_auto.config.ui_binding import FieldSpec, _coerce, apply_edits
from guild_auto.domain import rules as R
from guild_auto.domain.parsers import ParseError, _join_truncated_names, _to_int


def _cfg():
    return json.loads(json.dumps(DEFAULT_CONFIG))


def _errors(cfg):
    return [m for lvl, m in validate_config(cfg) if lvl == "error"]


def _member(**kw):
    base = {
        "player": "甲",
        "rank": "entry",
        "weekly_total": 100,
        "joined_at": "2026-01-01 00:00:00",
        "last_online": "2026-10-01 12:00:00",
        "online": False,
    }
    base.update(kw)
    return base


class TestSchemaMissingSections(unittest.TestCase):
    """缺整段配置 = 用户删掉了可选段，必须安静通过（默认值兜底）。"""

    def test_without_ui_section(self):
        cfg = _cfg()
        del cfg["ui"]
        self.assertEqual(_errors(cfg), [])

    def test_ui_section_only_enabled(self):
        cfg = _cfg()
        cfg["ui"] = {"enabled": True}
        self.assertEqual(_errors(cfg), [])

    def test_without_rules_keys(self):
        cfg = _cfg()
        for key in ("rules_enabled", "rules_strategy", "rules"):
            del cfg[key]
        self.assertEqual(_errors(cfg), [])

    def test_without_one_name_list(self):
        cfg = _cfg()
        del cfg["whitelist"]
        self.assertEqual(_errors(cfg), [])

    def test_without_match_wildcard(self):
        cfg = _cfg()
        del cfg["match_wildcard"]
        self.assertEqual(_errors(cfg), [])

    def test_without_blocked_name_policy(self):
        cfg = _cfg()
        del cfg["blocked_name_policy"]
        self.assertEqual(_errors(cfg), [])


class TestSchemaMalformedValues(unittest.TestCase):
    def test_blacklist_as_string_reports_error(self):
        cfg = _cfg()
        cfg["blacklist"] = "甲,乙"
        self.assertTrue(any("blacklist" in m for m in _errors(cfg)))

    def test_blocked_name_policy_invalid_reports_error(self):
        cfg = _cfg()
        cfg["blocked_name_policy"] = "随便写"
        self.assertTrue(any("blocked_name_policy" in m for m in _errors(cfg)))

    def test_game_log_unresolvable_path_does_not_crash(self):
        """含 NUL 的路径会让 os.path.abspath 抛 ValueError:校验必须吞掉它。"""
        cfg = _cfg()
        cfg["game_log"] = "坏\0路径.log"
        validate_config(cfg)  # 不抛异常即通过


class TestLoaderBranches(unittest.TestCase):
    def test_dump_jsonc_non_dict_returns_empty(self):
        self.assertEqual(_dump_jsonc(["不是 dict"]), "")

    def test_dump_jsonc_key_without_registered_comment(self):
        out = _dump_jsonc({"未登记的键": 1})
        self.assertIn("未登记的键", out)
        self.assertNotIn("//", out)

    def test_strip_jsonc_unterminated_block_comment(self):
        self.assertEqual(_strip_jsonc("/* 没有闭合"), "")

    def test_console_survives_logger_failure(self):
        from guild_auto.infrastructure import logger as logger_mod

        buf = StringIO()
        with mock.patch.object(logger_mod, "log_info", side_effect=RuntimeError("日志挂了")):
            with redirect_stdout(buf):
                console("仍然必须打印")
        self.assertIn("仍然必须打印", buf.getvalue())


class TestUiBindingBranches(unittest.TestCase):
    def test_coerce_str_list_accepts_list_input(self):
        field = FieldSpec("blacklist", "黑名单", "", "str_list", [])
        self.assertEqual(_coerce(field, ["甲", "乙"]), ["甲", "乙"])

    def test_apply_edits_keeps_warnings_out_of_errors(self):
        """validate_config 的 warning（如未知 skip_ranks）不得变成保存失败。"""
        cfg = _cfg()
        cfg["skip_ranks"] = ["不存在的等级"]
        new_cfg, errors = apply_edits(cfg, {"admin": "新管理员"})
        self.assertEqual(errors, [])
        self.assertEqual(new_cfg["admin"], "新管理员")


class TestParserBranches(unittest.TestCase):
    def test_to_int_unparseable_raises_parse_error(self):
        with self.assertRaises(ParseError):
            _to_int("不是数字", "周贡献: 不是数字")

    def test_join_truncated_names_keeps_trailing_pending(self):
        """块尾没有 ● 的残缺名必须保留，不能静默丢弃。"""
        self.assertEqual(_join_truncated_names(["甲 乙"], lambda row: False), ["甲 乙"])


class TestRuleBranches(unittest.TestCase):
    def test_actions_of_ignore_yields_nothing(self):
        """ignore = 明确什么都不做，不得产生动作（否则执行层会拿它拼命令）。"""
        self.assertEqual(R._actions_of({"actions": [{"type": "ignore"}]}), [])

    def test_all_strategy_accumulates_and_skips_non_matching(self):
        """all 策略：命中的规则全部累计；未命中的规则既不加动作也不计入 hits。"""
        cond = [{"field": "weekly_total", "op": "<", "value": 200}]
        table = [
            {"id": "a", "conditions": cond, "actions": [{"type": "promote"}], "priority": 1},
            {
                "id": "c",
                "conditions": [{"field": "weekly_total", "op": ">=", "value": 9999}],
                "actions": [{"type": "kick"}],
                "priority": 2,
            },
            {"id": "b", "conditions": cond, "actions": [{"type": "notify"}], "priority": 3},
        ]
        actions, hits, _ = R.evaluate_rules(table, _member(weekly_total=100), "all")
        self.assertEqual(hits, ["a", "b"])
        self.assertEqual(actions, ["promote", "notify"])

    def test_highest_priority_keeps_higher_priority_rule(self):
        cond = [{"field": "weekly_total", "op": "<", "value": 200}]
        table = [
            {"id": "high", "conditions": cond, "actions": [{"type": "kick"}], "priority": 5},
            {"id": "low", "conditions": cond, "actions": [{"type": "notify"}], "priority": 3},
        ]
        actions, hits, _ = R.evaluate_rules(table, _member(weekly_total=100), "highest_priority")
        self.assertEqual(hits, ["high"])
        self.assertEqual(actions, ["kick"])

    def test_validate_rules_accepts_promote_with_valid_times(self):
        errs = R.validate_rules(
            [
                {
                    "id": "r",
                    "conditions": [{"field": "weekly_total", "op": "<", "value": 200}],
                    "actions": [{"type": "promote", "times": 2}],
                }
            ]
        )
        self.assertEqual(errs, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
