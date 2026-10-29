# -*- coding: utf-8 -*-
"""
test_ui_mode.py —— UI 识别模式接线(US-007)
===========================================
覆盖:
  * 注入假截图/OCR/输入,真实走完 导航 → 扫掠 → 解析,产出与指令模式同构的 records;
  * main() 在 operation_mode="ui" 时走该阶段,第三份报告带第六节且 mode 为 ui;
  * 未配置 OCR 时明确报错,**不**降级回指令模式;
  * 字段不可读 / 屏蔽词玩家转失败项,绝不拿哨兵值去判定;
  * 暂停恢复后重新全扫(_RescanRequired → 再 collect 一次)。

零真实屏幕:截图/OCR/按键/悬停全部是假实现。
"""

import copy
import glob
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from guild_auto import main as main_mod
from guild_auto import ui_mode
from guild_auto.config import DEFAULT_CONFIG
from guild_auto.vision import adapters as adapters_mod
from guild_auto.vision.adapters import AdapterError
from guild_auto.vision.runner import CollectionResult

MEMBER_TEXT = """\
某某玩家
公会身份: 中等活跃成员
首次加入: 2026-09-26 21:38:03
上次在线: 2026-10-25 19:55:27(2.75天前)
公会经验周贡献: 6,799 公会经验
    今天: 0 公会经验
    2026-10-27: 0 公会经验
    2026-10-26: 0 公会经验
    2026-10-25: 564 公会经验
    2026-10-24: 1,310 公会经验
    2026-10-23: 4,925 公会经验
    2026-10-22: 0 公会经验
"""


class FakeAdapters:
    """假截图/OCR/输入:capture 返回预排的 (文本, 置信度),ocr 原样取出。"""

    def __init__(self, views):
        self._views = list(views)
        self.presses = []
        self.hovers = []
        self.clicks = []
        self.escapes = 0

    def _next_view(self):
        # 标题会被扫两次(导航确认 + 导航后重新确认),之后才是扫掠内容
        return self._views.pop(0) if len(self._views) > 1 else self._views[0]

    def press(self, key):
        self.presses.append(key)

    def hover(self, point):
        self.hovers.append(point)

    def click(self, point):
        self.clicks.append(point)

    def escape(self):
        self.escapes += 1

    def capture(self):
        return self._next_view()

    def ocr(self, image):
        return image

    @staticmethod
    def sleep(_seconds):
        return None


def _cfg(tmpdir):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["guild_name"] = "演示公会"
    cfg["log"] = {"enabled": False, "level": 4, "dir": os.path.join(tmpdir, "logs")}
    cfg["report_dir"] = os.path.join(tmpdir, "reports")
    cfg["game_options"] = os.path.join(tmpdir, "options.txt")
    with open(cfg["game_options"], "w", encoding="utf-8") as f:
        f.write("key_key.hotbar.7:key.keyboard.x\n")
    # 单格网格:扫掠耗时与用例断言都可控
    cfg["vision"]["grid"] = {
        "origin_x": 0,
        "origin_y": 0,
        "cell_width": 36,
        "cell_height": 36,
        "columns": 1,
        "rows": 1,
        "expected_cells": 1,
        "max_missing_ratio": 0.0,
    }
    cfg["vision"]["stay_ms"] = 120
    cfg["vision"]["low_confidence"] = 0.0
    return cfg


def _views(member_text=MEMBER_TEXT):
    return [("公会系统", 0.99), ("公会系统", 0.99), (member_text, 0.99)]


class _TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ui_mode_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = _cfg(self.tmp)


class TestStageQueryUi(_TempCase):
    def test_collects_records_isomorphic_with_command_mode(self):
        fake = FakeAdapters(_views())
        outcome = ui_mode.stage_query_ui(None, self.cfg, adapters=fake)

        self.assertEqual(len(outcome.records), 1)
        record = outcome.records[0]
        self.assertEqual(record["player"], "某某玩家")
        self.assertEqual(record["rank"], "mid")  # 显示名已映射回内部等级
        self.assertEqual(record["weekly_total"], 6799)
        self.assertEqual(record["joined_at"], "2026-09-26 21:38:03")
        self.assertEqual(record["last_online"], "2026-10-25 19:55:27")
        self.assertEqual(len(record["daily"]), 7)
        # online 是 UI 独有字段:未采样时是 None(不可读),绝不当 False
        self.assertIn("online", record)
        self.assertIsNone(record["online"])
        self.assertEqual(record["source"], "ui")

        self.assertEqual(outcome.guild["total"], 1)
        self.assertEqual(outcome.guild["groups"]["mid"], ["某某玩家"])
        self.assertEqual(outcome.failures, [])

        # 按键试探 / OCR / 导航统计都要进第六节的数据源
        self.assertEqual(outcome.stats.key_used, "x")
        self.assertEqual(outcome.stats.key_presses, ["x"])
        self.assertEqual(outcome.stats.navigation_enters, 1)
        self.assertEqual(outcome.stats.ocr_calls, 3)
        sections = outcome.stats.sections()
        titles = [s["title"] for s in sections]
        self.assertIn("按键试探", titles)
        self.assertIn("自动暂停", titles)
        self.assertIn("通知与执行", titles)

    def test_unreadable_weekly_becomes_failure_not_zero(self):
        text = MEMBER_TEXT.replace("公会经验周贡献: 6,799 公会经验", "")
        outcome = ui_mode.stage_query_ui(None, self.cfg, adapters=FakeAdapters(_views(text)))
        self.assertEqual(outcome.records, [])
        self.assertEqual(len(outcome.failures), 1)
        self.assertIn("weekly_total", outcome.failures[0]["reason"])

    def test_masked_name_becomes_blocked_failure(self):
        text = MEMBER_TEXT.replace("某某玩家", "***", 1)
        outcome = ui_mode.stage_query_ui(None, self.cfg, adapters=FakeAdapters(_views(text)))
        self.assertEqual(outcome.records, [])
        self.assertEqual(len(outcome.failures), 1)
        self.assertTrue(outcome.failures[0]["blocked"])

    def test_unknown_rank_becomes_failure(self):
        text = MEMBER_TEXT.replace("公会身份: 中等活跃成员", "公会身份: 传奇成员")
        outcome = ui_mode.stage_query_ui(None, self.cfg, adapters=FakeAdapters(_views(text)))
        self.assertEqual(outcome.records, [])
        self.assertIn("无法识别的公会身份", outcome.failures[0]["reason"])

    def test_auto_pause_guard_is_released_after_stage(self):
        from guild_auto.application import runtime

        runtime.reset_state()
        self.addCleanup(runtime.reset_state)
        ui_mode.stage_query_ui(None, self.cfg, adapters=FakeAdapters(_views()))
        self.assertFalse(runtime.auto_pause_guard(), "空闲时必须释放自动暂停守卫")


class TestNoOcrNoFallback(_TempCase):
    def test_missing_ocr_raises_clear_error(self):
        self.cfg["vision"]["ocr_command"] = ""
        with mock.patch.object(adapters_mod, "_find_pytesseract", return_value=None):
            with self.assertRaises(AdapterError) as ctx:
                ui_mode.stage_query_ui(None, self.cfg)
        self.assertIn("未配置 OCR 适配器", str(ctx.exception))

    def test_collection_failure_raises_instead_of_returning_empty(self):
        runner = mock.Mock()
        runner.collect.return_value = CollectionResult(ok=False, reason="扫掠不完整")
        with self.assertRaises(ui_mode.UiCollectionError) as ctx:
            ui_mode.stage_query_ui(None, self.cfg, adapters=FakeAdapters([]), runner=runner)
        self.assertIn("扫掠不完整", str(ctx.exception))


class TestRescanAfterPause(_TempCase):
    def test_pause_resume_triggers_full_rescan(self):
        """恢复后必须重新全扫:第一次 collect 抛 _RescanRequired,第二次成功。"""
        calls = []

        class FlakyRunner:
            def collect(self):
                calls.append(1)
                if len(calls) == 1:
                    raise ui_mode._RescanRequired
                return CollectionResult(ok=True, records=(), key_used="x")

        fake = FakeAdapters([])
        stats = ui_mode.UiRunStats()
        result = ui_mode._collect_with_rescan(FlakyRunner(), fake, stats)

        self.assertTrue(result.ok)
        self.assertEqual(stats.rescans, 1)
        self.assertEqual(stats.navigation_enters, 2)
        self.assertEqual(fake.escapes, 1, "恢复后应先关掉可能残留的界面再重扫")

    def test_gate_raises_rescan_right_after_resume(self):
        """恢复后 gate 必须先放行暂停,再抛"需重扫"让本次扫掠作废。"""
        from guild_auto.application import runtime

        runtime.reset_state()
        self.addCleanup(runtime.reset_state)
        # UI 模式下任何恢复都要重新全扫(note_resume 置 rescan 标志)
        runtime.configure_auto_pause(operation_mode="ui", enabled=True, grace_ms=0)
        runtime.note_resume()
        gate = ui_mode._make_gate()
        with self.assertRaises(ui_mode._RescanRequired):
            gate()


class _FakeRuntime:
    """够 main() 用、不碰硬件的假 Runtime。"""

    def __init__(self):
        self.watcher = mock.Mock(name="watcher")
        self.human = mock.Mock(name="human")
        self.executor = mock.Mock(name="executor")
        self.detector = None
        self.hwnd = 123
        self.listener = None
        self.async_executor = None
        self.async_system = None
        self.async_enabled = False
        self.ask_confirm = lambda p, d=False: True
        self.sleep = lambda s: None
        self.is_paused = lambda: False

    def confirm(self, prompt, default=False):
        return True

    def ask(self, prompt, default=False):
        return True

    def pause_gate(self):
        pass

    def reset_window_state_after_pause(self, hwnd=None, detector=None):
        return False

    def async_verify_submit(self, command, player, meta=None):
        return "task-id"

    def close(self):
        pass


class TestMainWiresUiMode(_TempCase):
    """main() 在 operation_mode="ui" 时真的走 UI 阶段,并把第六节写进报告。"""

    def _run(self, outcome):
        rt = _FakeRuntime()
        args = mock.Mock(
            config=os.path.join(self.tmp, "config.json"),
            admin=None,
            log=None,
            options=None,
            dry_run=False,
            debug=False,
            debug_skip=None,
            ui=False,
        )
        # 报告写出走真实实现(只是写文本),同时截下 run_meta 供断言
        real_write_analysis = main_mod.write_analysis_report
        captured = {}

        def _spy_write_analysis(report_dir, guild_name, records, cfg, run_meta):
            captured["run_meta"] = run_meta
            return real_write_analysis(report_dir, guild_name, records, cfg, run_meta)

        save_pending = mock.Mock()
        patches = [
            ("parse_args", args),
            ("prepare_config", self.cfg),
            ("runtime_mod.build_runtime", rt),
            ("find_game_window", 123),
            ("activate_window", None),
            ("start_pause_listener", mock.Mock(name="listener")),
            ("runtime_mod.pause_key_name", "u"),
            ("run_pending", (True, [])),
            ("pending_path_of", os.path.join(self.tmp, "pending.json")),
            ("console", None),
            ("logger", None),
            ("pyautogui.press", None),
            ("prepare_chat_state", None),
            ("wait_if_paused", None),
            ("reset_window_state_after_pause", None),
            ("stage_notify", []),
            ("stage_confirm_execution", (True, False)),
            ("stage_apply_action_gates", 0),
            ("stage_execute_async", []),
            ("write_contribution_report", os.path.join(self.tmp, "c.txt")),
            ("write_action_report", os.path.join(self.tmp, "a.txt")),
            ("load_pending", []),
            ("input", ""),
            ("time.sleep", None),
        ]
        with (
            mock.patch("guild_auto.ui_mode.stage_query_ui", return_value=outcome) as stage,
            mock.patch.object(main_mod, "write_analysis_report", _spy_write_analysis),
            mock.patch.object(main_mod, "save_pending", save_pending),
        ):
            for name, value in patches:
                mod_name, _, attr = name.partition(".")
                target = main_mod if not attr else getattr(main_mod, mod_name)
                key = attr or name
                if value is None:
                    patcher = mock.patch.object(target, key)
                else:
                    patcher = mock.patch.object(target, key, return_value=value)
                patcher.start()
                self.addCleanup(patcher.stop)
            main_mod.main()
        return stage, captured, save_pending, rt

    def test_ui_mode_report_has_section_six_and_real_operation_mode(self):
        self.cfg["operation_mode"] = "ui"
        stats = ui_mode.UiRunStats(key_used="x")
        outcome = ui_mode.UiQueryOutcome(
            guild={
                "guild_name": "演示公会",
                "total": 1,
                "online": None,
                "groups": {"entry": ["甲"]},
            },
            records=[
                {
                    "player": "甲",
                    "rank": "entry",
                    "weekly_total": 100,
                    "joined_at": "x",
                    "last_online": "y",
                    "daily": [],
                    "online": True,
                    "source": "ui",
                }
            ],
            failures=[],
            stats=stats,
        )
        stage, captured, save_pending, rt = self._run(outcome)

        stage.assert_called_once()
        run_meta = captured["run_meta"]
        self.assertEqual(run_meta["operation_mode"], "ui")
        self.assertIn("ui_sections", run_meta)
        titles = [section["title"] for section in run_meta["ui_sections"]]
        self.assertIn("自动暂停", titles)
        self.assertIn("通知与执行", titles)

        # 真实报告文件里必须有第六节,且模式行写的是 ui(不是硬编码 command)
        reports = glob.glob(os.path.join(self.cfg["report_dir"], "*运行分析报告.txt"))
        self.assertEqual(len(reports), 1, f"未生成分析报告: {reports}")
        with open(reports[0], encoding="utf-8") as f:
            text = f.read()
        self.assertIn("--- 六、UI识别运行记录 ---", text)
        self.assertIn("操作模式: ui", text)
        self.assertIn("本次跳过通知/执行", text)

        # UI 模式不得偷偷发聊天指令(静默降级回指令模式的证据)
        rt.human.send_chat_text.assert_not_called()
        rt.human.send_command.assert_not_called()
        # UI 模式跳过的执行不得写进待办(否则下次会在指令模式续做)
        save_pending.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
