# -*- coding: utf-8 -*-
"""
test_report_analysis_branches.py —— 第三份报告的分支与边界
=========================================================
`write_analysis_report`（docs/详细报告规范.md 七节）新增了 634 行渲染代码，
纳入核心层覆盖率门禁后把这些分支从 99% 拉低到 95.6%。本文件补齐的正是
**报告在缺数据 / 关配置 / 对账不平 / 写入失败**时必须表现出的行为 ——
这些分支不常见，但一旦出错，操作者拿到的是"看起来正常、结论却错"的报告。

覆盖：
  * 格式化辅助：占位符而非 0；时间解析的多种输入；非法秒数
  * 判定文本：黑名单/白名单外/保护名单/规则命中/阈值命中/无记录
  * 执行文本：失败原因原文 / 无操作 / 未执行 / 成功迁移
  * 名册摊平：8 种状态各自归类（含屏蔽词兜底与未处理）
  * 渲染开关：配置快照关闭、每日明细关闭、UI 模式第六节
  * 对账：差值 0 / 差值非 0 / 成员总数缺失
  * 写入失败：记 warning 且返回 None，不抛异常
"""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from guild_auto.config import DEFAULT_CONFIG
from guild_auto.domain import reporter as R


def _cfg(**overrides):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg.update(overrides)
    return cfg


def _meta(**overrides):
    meta = {
        "started_at": datetime(2026, 10, 17, 20, 14, 3),
        "ended_at": datetime(2026, 10, 17, 21, 7, 42),
        "operation_mode": "command",
        "build_variant": "source",
        "capabilities": ["core"],
        "stage_timings": [{"name": "查询公会列表", "seconds": 4, "ok": 1, "fail": 0, "note": ""}],
        "roster": [],
        "counts": {
            "member_total": 0,
            "online_total": 0,
            "queried": 0,
            "decided": 0,
            "query_ok": 0,
            "query_fail": 0,
            "blocked_name": 0,
        },
    }
    meta.update(overrides)
    return meta


class TestFormattingHelpers(unittest.TestCase):
    def test_num_text_uses_placeholder_for_missing(self):
        self.assertEqual(R._num_text(None), R._PLACEHOLDER)
        self.assertEqual(R._num_text(0), "0", "0 是真实值,不能被当成缺失")

    def test_fmt_seconds_hours_minutes_seconds(self):
        self.assertEqual(R._fmt_seconds(4), "0m04s")
        self.assertEqual(R._fmt_seconds(3661), "1h01m01s")
        self.assertEqual(R._fmt_seconds(-5), "0m00s", "负值按 0 处理,不出现 -0m05s")

    def test_fmt_seconds_invalid_is_placeholder(self):
        self.assertEqual(R._fmt_seconds("不是数字"), R._PLACEHOLDER)
        self.assertEqual(R._fmt_seconds(None), R._PLACEHOLDER)

    def test_parse_ts_accepts_datetime_and_text(self):
        dt = datetime(2026, 1, 2, 3, 4, 5)
        self.assertEqual(R._parse_ts(dt), dt)
        self.assertEqual(R._parse_ts("2026-01-02 03:04:05"), dt)
        self.assertEqual(R._parse_ts("2026-01-02 03:04"), datetime(2026, 1, 2, 3, 4))

    def test_parse_ts_empty_and_garbage(self):
        self.assertIsNone(R._parse_ts(None))
        self.assertIsNone(R._parse_ts(""))
        self.assertIsNone(R._parse_ts("不是时间"))

    def test_ts_text_falls_back_to_raw_then_placeholder(self):
        self.assertEqual(R._ts_text("2026-01-02 03:04:05"), "2026-01-02 03:04:05")
        self.assertEqual(R._ts_text("原始文本"), "原始文本")
        self.assertEqual(R._ts_text(None), R._PLACEHOLDER)

    def test_window_seconds_requires_both_ends(self):
        self.assertIsNone(R._window_seconds(None, "2026-01-02 03:04:05"))
        self.assertIsNone(R._window_seconds("2026-01-02 03:04:05", "坏时间"))
        self.assertEqual(R._window_seconds("2026-01-02 03:00:00", "2026-01-02 04:00:00"), 3600.0)

    def test_rank_text_specials_and_config_names(self):
        names = {"high": "高活跃"}
        self.assertEqual(R._rank_text("kicked", names), "已踢出")
        self.assertEqual(R._rank_text("unchanged", names), "不变")
        self.assertEqual(R._rank_text(None, names), R._PLACEHOLDER)
        self.assertEqual(R._rank_text("high", names), "高活跃")
        self.assertEqual(R._rank_text("unknown_rank", {}), "unknown_rank")

    def test_action_text_variants(self):
        self.assertEqual(R._action_text([]), "无操作")
        self.assertEqual(R._action_text(None), "无操作")
        self.assertEqual(R._action_text(["promote"]), "升职")
        self.assertEqual(R._action_text(["promote", "promote"]), "升职×2")
        self.assertEqual(R._action_text(["demote"]), "降职")
        self.assertEqual(R._action_text(["kick"]), "踢出")
        self.assertEqual(R._action_text(["notify"]), "notify")


class TestDecisionText(unittest.TestCase):
    def test_name_list_blocks_use_distinct_wording(self):
        cfg = _cfg()
        self.assertIn("黑名单", R._decision_text({"trigger": "blacklist"}, cfg))
        self.assertIn("白名单外", R._decision_text({"trigger": "not_in_whitelist"}, cfg))
        self.assertIn("保护名单", R._decision_text({"trigger": "whitelist_protected"}, cfg))

    def test_rule_trigger_names_the_rule(self):
        text = R._decision_text({"trigger": "rule:r1", "actions": ["kick"]}, _cfg())
        self.assertIn("r1", text)
        self.assertIn("踢出", text)

    def test_threshold_trigger_shows_boundary_and_input(self):
        text = R._decision_text(
            {"trigger": "high_demote", "weekly_total": 23746, "actions": ["demote"]}, _cfg()
        )
        self.assertIn("high_demote", text)
        self.assertIn("23746", text, "必须写出输入值")
        self.assertIn(R._THRESHOLD_OPS["high_demote"], text, "必须写出边界语义")

    def test_no_trigger_no_action_means_matrix_miss(self):
        text = R._decision_text({"weekly_total": 100}, _cfg())
        self.assertIn("阈值矩阵未命中", text)
        self.assertIn("100", text)

    def test_unknown_trigger_is_reported_not_swallowed(self):
        text = R._decision_text({"trigger": "某个新触发", "weekly_total": 1}, _cfg())
        self.assertIn("某个新触发", text)


class TestExecutionText(unittest.TestCase):
    def test_error_text_is_verbatim(self):
        self.assertEqual(R._execution_text({"error": "超时"}, {}), "超时")

    def test_no_action_and_not_executed(self):
        self.assertEqual(R._execution_text({"actions": []}, {}), "无操作")
        self.assertEqual(R._execution_text({"actions": ["kick"], "executed": False}, {}), "未执行")

    def test_success_shows_rank_migration(self):
        text = R._execution_text(
            {
                "actions": ["demote"],
                "executed": True,
                "error": None,
                "rank": "high",
                "new_rank": "mid",
            },
            {"high": "高活跃", "mid": "中等活跃"},
        )
        self.assertEqual(text, "成功 → 高活跃 → 中等活跃")


class TestBuildRoster(unittest.TestCase):
    def _roster(self):
        cfg = _cfg()
        groups = {
            "leader": ["会长甲"],
            "high": ["高活跃甲", "黑名单乙", "白名单外丙", "保护丁", "********"],
            "mid": ["查询失败己", "未处理庚", "已处理辛"],
        }
        records = [
            {"player": "高活跃甲", "rank": "high", "trigger": "high_demote"},
            {"player": "黑名单乙", "rank": "high", "trigger": "blacklist"},
            {"player": "白名单外丙", "rank": "high", "trigger": "not_in_whitelist"},
            {"player": "保护丁", "rank": "high", "trigger": "whitelist_protected"},
            {"player": "已处理辛", "rank": "mid", "trigger": "mid_promote"},
        ]
        failures = [
            {"player": "查询失败己", "reason": "等待响应超时"},
            {"player": "********", "reason": "屏蔽词"},
        ]
        return R.build_roster(groups, records, failures, cfg)

    def test_eight_statuses_are_classified(self):
        by_player = {e["player"]: e["status"] for e in self._roster()}
        self.assertEqual(by_player["会长甲"], "skip_rank")
        self.assertEqual(by_player["高活跃甲"], "processed")
        self.assertEqual(by_player["黑名单乙"], "blacklist")
        self.assertEqual(by_player["白名单外丙"], "not_in_whitelist")
        self.assertEqual(by_player["保护丁"], "protected")
        self.assertEqual(by_player["********"], "blocked", "星号名必须判为屏蔽词")
        self.assertEqual(by_player["查询失败己"], "query_failed")
        self.assertEqual(by_player["未处理庚"], "unprocessed")

    def test_reason_is_kept_for_failed_entries(self):
        entry = next(e for e in self._roster() if e["player"] == "查询失败己")
        self.assertEqual(entry.get("reason"), "等待响应超时")

    def test_empty_groups_yield_empty_roster(self):
        self.assertEqual(R.build_roster({}, [], [], _cfg()), [])


class TestOnlineText(unittest.TestCase):
    def test_number_ui_and_unknown(self):
        self.assertEqual(R._online_text({"online_total": 43}, "command"), "43")
        self.assertIn("不适用", R._online_text({"online_total": None}, "ui"))
        self.assertIn("未统计", R._online_text({}, "command"))


class TestRenderOptions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="analysis_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _write(self, records, cfg, meta):
        path = R.write_analysis_report(self.tmp, "演示公会", records, cfg, meta)
        self.assertIsNotNone(path)
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_config_snapshot_can_be_disabled(self):
        cfg = _cfg()
        cfg["report"]["include_config_snapshot"] = False
        text = self._write([], cfg, _meta())
        self.assertIn("include_config_snapshot=false", text)
        self.assertNotIn("阈值:", text)

    def test_daily_detail_flag_controls_daily_line(self):
        record = {
            "player": "甲",
            "rank": "high",
            "weekly_total": 10,
            "daily": [("今天", 5), ("2026-10-16", 5)],
            "actions": [],
        }
        cfg = _cfg()
        cfg["report"]["include_daily_detail"] = True
        self.assertIn("每日:", self._write([record], cfg, _meta()))
        cfg["report"]["include_daily_detail"] = False
        self.assertNotIn("每日:", self._write([record], cfg, _meta()))

    def test_ui_mode_renders_section_six(self):
        meta = _meta(
            operation_mode="ui",
            ui_sections=[{"title": "按键试探", "lines": ["首键失败 → 改试成功"]}],
        )
        text = self._write([], _cfg(), meta)
        self.assertIn("六、UI识别运行记录", text)
        self.assertIn("首键失败", text)

    def test_command_mode_omits_section_six(self):
        self.assertNotIn("六、UI识别运行记录", self._write([], _cfg(), _meta()))

    def test_ui_mode_without_data_says_so_instead_of_blank(self):
        text = self._write([], _cfg(), _meta(operation_mode="ui"))
        self.assertIn("未提供 UI 识别数据", text)

    def test_reconciliation_reports_zero_diff(self):
        roster = [{"player": "甲", "rank": "high", "status": "processed"}]
        meta = _meta(roster=roster)
        meta["counts"]["member_total"] = 1
        text = self._write([{"player": "甲", "rank": "high", "actions": []}], _cfg(), meta)
        self.assertIn("差值: 0 ✅ 无遗漏", text)

    def test_reconciliation_flags_nonzero_diff(self):
        roster = [{"player": "甲", "rank": "high", "status": "processed"}]
        meta = _meta(roster=roster)
        meta["counts"]["member_total"] = 2
        text = self._write([], _cfg(), meta)
        self.assertIn("差值: -1 ❌ 报告缺陷", text, "名册比成员总数少 1 时必须报缺陷")

    def test_missing_member_total_is_reported_not_zero(self):
        meta = _meta()
        meta["counts"]["member_total"] = None
        text = self._write([], _cfg(), meta)
        self.assertIn("无法对账", text)

    def test_stage_without_seconds_does_not_break_total(self):
        meta = _meta(stage_timings=[{"name": "坏阶段", "seconds": "坏"}])
        text = self._write([], _cfg(), meta)
        self.assertIn("坏阶段", text)

    def test_write_failure_returns_none_and_warns(self):
        with mock.patch("builtins.open", side_effect=OSError("磁盘满")):
            with mock.patch.object(R.logger, "log_warning") as m_warn:
                path = R.write_analysis_report(self.tmp, "演示公会", [], _cfg(), _meta())
        self.assertIsNone(path)
        m_warn.assert_called_once()

    def test_report_filename_uses_safe_guild_name(self):
        path = R.write_analysis_report(self.tmp, "a/b:c", [], _cfg(), _meta())
        self.assertIsNotNone(path)
        self.assertNotIn(os.sep + "b", os.path.basename(path))
        self.assertTrue(os.path.basename(path).endswith("运行分析报告.txt"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
