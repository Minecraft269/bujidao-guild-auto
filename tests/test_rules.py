# -*- coding: utf-8 -*-
"""
test_rules.py —— 自定义规则引擎（核心层）
========================================
锁定阶段 4 的硬性要求：
  1. 内置阈值规则 = 规则表里的"一条规则"，与自定义规则**共用同一接口**
  2. 条件求值纯函数零 IO；"读不到"一律判否，绝不当 0/真
  3. 三种优先级策略：first_match / highest_priority / all；stop 只在**命中后**生效
  4. 语法错误可被启动校验拦截，并指明规则 id 与字段
  5. 结果可追溯：每条命中都能给出规则 id 与条件明细
"""
import json
import unittest

from guild_auto.config import DEFAULT_CONFIG
from guild_auto.domain import rules as R
from guild_auto.domain.decisions import decide_actions, decide_for_player


def _cfg(**overrides):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg.update(overrides)
    return cfg


def _member(**kw):
    base = {"player": "甲", "rank": "entry", "weekly_total": 100,
            "joined_at": "2026-01-01 00:00:00",
            "last_online": "2026-10-01 12:00:00", "online": False}
    base.update(kw)
    return base


class TestCheckCondition(unittest.TestCase):
    def test_numeric_ops(self):
        m = _member(weekly_total=100)
        self.assertTrue(R.check_condition({"field": "weekly_total", "op": "<", "value": 200}, m)[0])
        self.assertTrue(R.check_condition({"field": "weekly_total", "op": "<=", "value": 100}, m)[0])
        self.assertFalse(R.check_condition({"field": "weekly_total", "op": "<", "value": 100}, m)[0])
        self.assertTrue(R.check_condition({"field": "weekly_total", "op": ">=", "value": 100}, m)[0])

    def test_numeric_unreadable_is_false(self):
        ok, detail = R.check_condition({"field": "weekly_total", "op": "<", "value": 1},
                                       _member(weekly_total=None))
        self.assertFalse(ok)
        self.assertIn("不可读", detail)

    def test_numeric_bool_is_not_number(self):
        ok, _ = R.check_condition({"field": "weekly_total", "op": "<", "value": 1},
                                  _member(weekly_total=True))
        self.assertFalse(ok, "bool 不应被当成数字")

    def test_rank_in_and_not_in(self):
        m = _member(rank="low")
        self.assertTrue(
            R.check_condition({"field": "rank", "op": "in", "value": ["low", "entry"]}, m)[0])
        self.assertFalse(R.check_condition({"field": "rank", "op": "in", "value": ["high"]}, m)[0])
        self.assertTrue(
            R.check_condition({"field": "rank", "op": "not_in", "value": ["high"]}, m)[0])

    def test_rank_in_requires_list(self):
        ok, detail = R.check_condition({"field": "rank", "op": "in", "value": "low"}, _member())
        self.assertFalse(ok)
        self.assertIn("需要列表", detail)

    def test_online_is_not_supported(self):
        """解析层不产出 online 字段 → 该条件不支持（避免"文档宣称但恒不可读"）。"""
        ok, detail = R.check_condition({"field": "online", "op": "==", "value": True},
                                       _member(online=True))
        self.assertFalse(ok)
        self.assertIn("未知字段", detail)

    def test_player_matches_wildcard_casefold(self):
        self.assertTrue(
            R.check_condition({"field": "player", "op": "matches", "value": "test*"},
                              _member(player="TestPlayer"))[0])
        self.assertFalse(
            R.check_condition({"field": "player", "op": "matches", "value": "x*"},
                              _member(player="小明"))[0])

    def test_date_before_after(self):
        m = _member(joined_at="2026-01-01 00:00:00")
        self.assertTrue(R.check_condition({"field": "joined_at", "op": "before",
                                           "value": "2026-02-01"}, m)[0])
        self.assertFalse(R.check_condition({"field": "joined_at", "op": "after",
                                            "value": "2026-02-01"}, m)[0])

    def test_date_unreadable_or_bad_threshold(self):
        ok, detail = R.check_condition({"field": "joined_at", "op": "before",
                                        "value": "2026-01-01"},
                                       _member(joined_at="不是日期"))
        self.assertFalse(ok)
        self.assertIn("不可读", detail)
        ok, detail = R.check_condition({"field": "joined_at", "op": "before", "value": "乱写"},
                                       _member())
        self.assertFalse(ok)
        self.assertIn("非法", detail)

    def test_unknown_field_and_bad_op(self):
        self.assertFalse(R.check_condition({"field": "nope", "op": "<", "value": 1}, _member())[0])
        ok, detail = R.check_condition({"field": "weekly_total", "op": "≈", "value": 1}, _member())
        self.assertFalse(ok)
        self.assertIn("算子非法", detail)
        self.assertFalse(R.check_condition("不是字典", _member())[0])


class TestConditionBranchCoverage(unittest.TestCase):
    """补齐算子/不可读分支（核心层门禁要求）。"""

    def test_parse_date_none(self):
        self.assertIsNone(R._parse_date(None))

    def test_cmp_eq_ne(self):
        self.assertTrue(R._cmp("==", 1, 1))
        self.assertTrue(R._cmp("!=", 1, 2))
        self.assertIsNone(R._cmp("≈", 1, 1))

    def test_rank_eq_ne(self):
        m = _member(rank="low")
        self.assertTrue(R.check_condition({"field": "rank", "op": "==", "value": "low"}, m)[0])
        self.assertTrue(R.check_condition({"field": "rank", "op": "!=", "value": "high"}, m)[0])

    def test_rank_unreadable(self):
        ok, detail = R.check_condition({"field": "rank", "op": "==", "value": "low"},
                                       _member(rank=None))
        self.assertFalse(ok)
        self.assertIn("不可读", detail)

    def test_rank_bad_op(self):
        ok, detail = R.check_condition({"field": "rank", "op": "<", "value": "low"}, _member())
        self.assertFalse(ok)
        self.assertIn("算子非法", detail)

    def test_online_condition_rejected_by_validation(self):
        errs = R.validate_rules([{"id": "r", "conditions": [
            {"field": "online", "op": "==", "value": True}],
            "actions": [{"type": "notify"}]}])
        self.assertIn("conditions[0].field", {e["field"] for e in errs})

    def test_player_eq_ne(self):
        m = _member(player="Alice")
        self.assertTrue(R.check_condition({"field": "player", "op": "==", "value": "Alice"}, m)[0])
        self.assertTrue(R.check_condition({"field": "player", "op": "!=", "value": "Bob"}, m)[0])

    def test_player_empty_unreadable(self):
        ok, detail = R.check_condition({"field": "player", "op": "==", "value": "x"},
                                       _member(player=""))
        self.assertFalse(ok)
        self.assertIn("不可读", detail)

    def test_player_bad_op(self):
        ok, detail = R.check_condition({"field": "player", "op": "<", "value": "x"}, _member())
        self.assertFalse(ok)
        self.assertIn("算子非法", detail)

    def test_date_after_and_bad_op(self):
        m = _member(joined_at="2026-03-01")
        ok, detail = R.check_condition({"field": "joined_at", "op": "after",
                                        "value": "2026-02-01"}, m)
        self.assertTrue(ok)
        ok, detail = R.check_condition({"field": "joined_at", "op": "==", "value": "2026-02-01"}, m)
        self.assertFalse(ok)
        self.assertIn("算子非法", detail)

    def test_actions_ignore_non_dict(self):
        rule = {"actions": ["不是对象", {"type": "notify"}]}
        self.assertEqual(R._actions_of(rule), ["notify"])


class TestRuleMatching(unittest.TestCase):
    def test_conditions_are_and(self):
        rule = {"conditions": [
            {"field": "weekly_total", "op": "<", "value": 500},
            {"field": "rank", "op": "in", "value": ["entry"]},
        ]}
        ok, trace = R.rule_matches(rule, _member(weekly_total=100, rank="entry"))
        self.assertTrue(ok)
        self.assertEqual(len(trace), 2)
        ok, trace = R.rule_matches(rule, _member(weekly_total=100, rank="low"))
        self.assertFalse(ok)
        self.assertFalse(trace[-1]["ok"], "失败的条件要留痕")

    def test_empty_conditions_is_not_a_match(self):
        """空/缺失 conditions 必须判否 —— 否则漏写就命中所有人（kick 即全员踢）。"""
        ok, trace = R.rule_matches({"conditions": []}, _member())
        self.assertFalse(ok)
        self.assertIn("conditions", trace[0]["detail"])
        ok, _ = R.rule_matches({}, _member())
        self.assertFalse(ok)

    def test_actions_expansion(self):
        rule = {"actions": [{"type": "promote", "times": 2}, {"type": "notify"}]}
        self.assertEqual(R._actions_of(rule), ["promote", "promote", "notify"])

    def test_ignore_produces_no_action(self):
        """ignore 的语义是"什么都不做"，不能产生动作（否则执行层拿它拼命令会崩）。"""
        self.assertEqual(R._actions_of({"actions": [{"type": "ignore"}]}), [])
        self.assertEqual(R._actions_of({"actions": [{"type": "kick", "reason": "x"}]}), ["kick"])

    def test_notify_is_kept_out_of_execution(self):
        """notify 保留为动作标记，但执行层只认 promote/demote/kick。"""
        from guild_auto.application.stages import EXECUTABLE_ACTIONS
        self.assertIn("notify", R.ACTIONS)
        self.assertNotIn("notify", EXECUTABLE_ACTIONS)


class TestBuiltinSharesInterface(unittest.TestCase):
    def test_builtin_delegates_to_decide_actions(self):
        cfg = _cfg()
        cfg["thresholds"] = {"entry_kick": 3500}
        rule = R.builtin_default_rule(cfg)
        actions, trigger, _ = R.evaluate(rule, _member(weekly_total=0))
        self.assertEqual(actions, ["kick"])
        self.assertEqual(trigger, "entry_kick")

    def test_builtin_is_just_a_rule_in_the_table(self):
        cfg = _cfg()
        cfg["thresholds"] = {"entry_kick": 3500}
        table = [R.builtin_default_rule(cfg)]
        actions, hits, traces = R.evaluate_rules(table, _member(weekly_total=0))
        self.assertEqual(actions, ["kick"])
        self.assertEqual(hits, [R.BUILTIN_RULE_ID])
        self.assertEqual(traces[0]["rule_id"], R.BUILTIN_RULE_ID)


class TestStrategies(unittest.TestCase):
    def _table(self):
        return [
            {"id": "a", "priority": 10, "stop": True,
             "conditions": [{"field": "weekly_total", "op": "<", "value": 500}],
             "actions": [{"type": "demote"}]},
            {"id": "b", "priority": 99,
             "conditions": [{"field": "weekly_total", "op": "<", "value": 500}],
             "actions": [{"type": "kick"}]},
        ]

    def test_first_match_takes_list_order(self):
        actions, hits, _ = R.evaluate_rules(self._table(), _member(weekly_total=100),
                                            "first_match")
        self.assertEqual(actions, ["demote"])
        self.assertEqual(hits, ["a"])

    def test_highest_priority_wins(self):
        actions, hits, _ = R.evaluate_rules(self._table(), _member(weekly_total=100),
                                            "highest_priority")
        self.assertEqual(actions, ["kick"])
        self.assertEqual(hits, ["b"])

    def test_all_stops_on_matched_stop_rule(self):
        actions, hits, _ = R.evaluate_rules(self._table(), _member(weekly_total=100), "all")
        self.assertEqual(actions, ["demote"])
        self.assertEqual(hits, ["a"])

    def test_all_without_stop_applies_all(self):
        table = self._table()
        table[0]["stop"] = False
        actions, hits, _ = R.evaluate_rules(table, _member(weekly_total=100), "all")
        self.assertEqual(actions, ["demote", "kick"])
        self.assertEqual(hits, ["a", "b"])

    def test_non_match_does_not_trigger_stop(self):
        """stop 的语义是"命中后停止"，未命中不得中断后续规则。"""
        table = [
            {"id": "first", "stop": True,
             "conditions": [{"field": "weekly_total", "op": ">", "value": 99999}],
             "actions": [{"type": "kick"}]},
            {"id": "second",
             "conditions": [{"field": "weekly_total", "op": "<", "value": 500}],
             "actions": [{"type": "demote"}]},
        ]
        actions, hits, _ = R.evaluate_rules(table, _member(weekly_total=100), "first_match")
        self.assertEqual(hits, ["second"])
        self.assertEqual(actions, ["demote"])

    def test_disabled_rules_are_skipped(self):
        table = self._table()
        table[0]["enabled"] = False
        _, hits, _ = R.evaluate_rules(table, _member(weekly_total=100), "first_match")
        self.assertEqual(hits, ["b"])

    def test_no_match_returns_empty(self):
        actions, hits, _ = R.evaluate_rules(self._table(), _member(weekly_total=99999),
                                            "highest_priority")
        self.assertEqual((actions, hits), ([], []))

    def test_trace_records_matched_rules(self):
        _, _, traces = R.evaluate_rules(self._table(), _member(weekly_total=100), "all")
        self.assertEqual([t["rule_id"] for t in traces], ["a"])


class TestValueTypeValidation(unittest.TestCase):
    """条件 value 的类型校验（运行期崩溃与静默判反的根因）。"""

    def _err(self, field, op, value):
        return R._value_type_error(field, op, value)

    def test_numeric(self):
        self.assertIsNone(self._err("weekly_total", "<", 1000))
        self.assertIsNotNone(self._err("weekly_total", "<", "1000"))
        self.assertIsNotNone(self._err("weekly_total", "<", True))

    def test_rank(self):
        self.assertIsNone(self._err("rank", "in", ["low"]))
        self.assertIsNone(self._err("rank", "==", "low"))
        self.assertIsNotNone(self._err("rank", "in", "low"))
        self.assertIsNotNone(self._err("rank", "in", [1]))
        self.assertIsNotNone(self._err("rank", "==", 1))

    def test_player(self):
        self.assertIsNone(self._err("player", "matches", "测试*"))
        self.assertIsNotNone(self._err("player", "==", 123))

    def test_date(self):
        self.assertIsNone(self._err("joined_at", "before", "2026-01-01"))
        self.assertIsNotNone(self._err("joined_at", "before", "乱写"))

    def test_unknown_field_returns_none(self):
        self.assertIsNone(self._err("nope", "<", 1))

    def test_validation_surfaces_value_errors(self):
        errs = R.validate_rules([{
            "id": "r", "conditions": [
                {"field": "rank", "op": "in", "value": "low"}],
            "actions": [{"type": "notify"}]}])
        self.assertIn("conditions[0].value", {e["field"] for e in errs})


class TestValidateRules(unittest.TestCase):
    GOOD = {"id": "r1", "enabled": True, "priority": 1, "stop": True,
            "conditions": [{"field": "weekly_total", "op": "<", "value": 100}],
            "actions": [{"type": "kick", "reason": "x"}]}

    def test_good_rule_passes(self):
        self.assertEqual(R.validate_rules([self.GOOD]), [])

    def test_none_is_ok(self):
        self.assertEqual(R.validate_rules(None), [])

    def test_not_a_list(self):
        errs = R.validate_rules({"id": "x"})
        self.assertEqual(len(errs), 1)
        self.assertIn("列表", errs[0]["message"])

    def test_non_object_rule(self):
        self.assertEqual(R.validate_rules(["x"])[0]["field"], "")

    def test_missing_and_duplicate_id(self):
        errs = R.validate_rules([{"actions": [{"type": "notify"}]}])
        self.assertTrue(any(e["field"] == "id" for e in errs))
        errs = R.validate_rules([self.GOOD, dict(self.GOOD)])
        self.assertTrue(any("重复" in e["message"] for e in errs))

    def test_bad_priority_and_bools(self):
        r = dict(self.GOOD, priority="高", stop="是")
        fields = {e["field"] for e in R.validate_rules([r])}
        self.assertIn("priority", fields)
        self.assertIn("stop", fields)

    def test_bad_conditions(self):
        r = dict(self.GOOD, conditions={"field": "x"})
        self.assertTrue(any(e["field"] == "conditions" for e in R.validate_rules([r])))
        r = dict(self.GOOD, conditions=["不是对象"])
        self.assertTrue(any(e["field"] == "conditions[0]" for e in R.validate_rules([r])))
        r = dict(self.GOOD, conditions=[{"field": "nope", "op": "<", "value": 1}])
        self.assertIn("conditions[0].field", {e["field"] for e in R.validate_rules([r])})
        r = dict(self.GOOD, conditions=[{"field": "weekly_total", "op": "≈", "value": 1}])
        self.assertIn("conditions[0].op", {e["field"] for e in R.validate_rules([r])})
        r = dict(self.GOOD, conditions=[{"field": "weekly_total", "op": "<"}])
        self.assertIn("conditions[0].value", {e["field"] for e in R.validate_rules([r])})

    def test_bad_actions(self):
        r = dict(self.GOOD, actions=[])
        self.assertIn("actions", {e["field"] for e in R.validate_rules([r])})
        r = dict(self.GOOD, actions=["不是对象"])
        self.assertIn("actions[0]", {e["field"] for e in R.validate_rules([r])})
        r = dict(self.GOOD, actions=[{"type": "乱来"}])
        self.assertIn("actions[0].type", {e["field"] for e in R.validate_rules([r])})
        r = dict(self.GOOD, actions=[{"type": "promote", "times": 0}])
        self.assertIn("actions[0].times", {e["field"] for e in R.validate_rules([r])})


class TestDecideForPlayerIntegration(unittest.TestCase):
    """规则开关对 decide_for_player 的行为影响。"""

    def _cfg2(self):
        cfg = _cfg()
        cfg["thresholds"] = {"entry_kick": 3500, "entry_promote_1": 10000}
        return cfg

    def test_rules_disabled_matches_legacy_exactly(self):
        cfg = self._cfg2()
        for weekly in (0, 3000, 10000, 20000):
            actions, _ = decide_for_player("甲", "entry", weekly, cfg)
            self.assertEqual(actions, decide_actions("entry", weekly, cfg["thresholds"])[0])

    def test_custom_rule_takes_precedence_over_builtin(self):
        cfg = self._cfg2()
        cfg["rules_enabled"] = True
        cfg["rules"] = [{"id": "r1", "enabled": True, "priority": 100, "stop": True,
                         "conditions": [{"field": "weekly_total", "op": "<", "value": 1}],
                         "actions": [{"type": "demote"}]}]
        actions, trigger = decide_for_player("甲", "entry", 0, cfg)
        self.assertEqual(actions, ["demote"])
        self.assertEqual(trigger, "rule:r1")

    def test_builtin_fallback_when_no_custom_match(self):
        cfg = self._cfg2()
        cfg["rules_enabled"] = True
        cfg["rules"] = [{"id": "r1", "enabled": True,
                         "conditions": [{"field": "weekly_total", "op": "<", "value": 1}],
                         "actions": [{"type": "demote"}]}]
        actions, trigger = decide_for_player("乙", "entry", 20000, cfg)
        # 与纯阈值判定逐字一致（不硬编码期望，避免阈值夹具不全导致误判）
        self.assertEqual(actions, decide_actions("entry", 20000, cfg["thresholds"])[0])
        self.assertTrue(trigger.startswith("rule:builtin"))

    def test_blacklist_still_wins_over_rules(self):
        cfg = self._cfg2()
        cfg["rules_enabled"] = True
        cfg["blacklist"] = ["甲"]
        cfg["rules"] = [{"id": "r1", "enabled": True,
                         "conditions": [{"field": "weekly_total", "op": "<", "value": 1}],
                         "actions": [{"type": "kick"}]}]
        actions, trigger = decide_for_player("甲", "entry", 0, cfg)
        self.assertEqual((actions, trigger), ([], "blacklist"))

    def test_protected_strips_non_promote_from_rule_actions(self):
        cfg = self._cfg2()
        cfg["rules_enabled"] = True
        cfg["whitelist_protected"] = ["甲"]
        cfg["rules"] = [{"id": "r1", "enabled": True, "stop": True,
                         "conditions": [{"field": "weekly_total", "op": "<", "value": 1}],
                         "actions": [{"type": "kick"}]}]
        actions, trigger = decide_for_player("甲", "entry", 0, cfg)
        self.assertEqual(actions, [])
        self.assertEqual(trigger, "whitelist_protected")


if __name__ == "__main__":
    unittest.main(verbosity=2)