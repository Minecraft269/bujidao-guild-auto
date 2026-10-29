# -*- coding: utf-8 -*-
"""
test_vision_config_capabilities.py —— 阶段 6 配置项与构建能力门禁
=================================================================
覆盖:
  * 新增配置键的默认值与校验(stay_ms 下限、比例区间、枚举、1~9 槽位);
  * 非法值必须报 error 而不是崩;
  * operation_mode="ui" 在主流程已接线但缺 OCR 适配器时启动即报错,禁止静默降级。
"""

import copy
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from guild_auto.config import CONFIG_COMMENTS, DEFAULT_CONFIG, validate_config
from guild_auto.vision.capabilities import (
    CapabilityError,
    ensure_operation_mode_supported,
    has_vision_capability,
)


def _cfg():
    return copy.deepcopy(DEFAULT_CONFIG)


def _errors(issues):
    return [msg for level, msg in issues if level == "error"]


class TestVisionDefaults(unittest.TestCase):
    def test_operation_mode_defaults_to_command(self):
        self.assertEqual(DEFAULT_CONFIG["operation_mode"], "command")

    def test_vision_defaults(self):
        vision = DEFAULT_CONFIG["vision"]
        self.assertEqual(vision["hotbar_slot"], 7)
        self.assertEqual(vision["stay_ms"], 120)
        self.assertEqual(vision["sweep_gap_ratio"], 1.0)
        self.assertEqual(vision["low_confidence"], 0.7)
        self.assertEqual(vision["max_misclick"], 3)
        self.assertEqual(vision["kick_verify"], "any")

    def test_default_config_is_valid(self):
        self.assertEqual(_errors(validate_config(_cfg())), [])

    def test_every_vision_key_documented(self):
        for key in ("operation_mode", "vision"):
            self.assertIn(key, CONFIG_COMMENTS)
        for key in DEFAULT_CONFIG["vision"]:
            self.assertIn(f"vision.{key}", CONFIG_COMMENTS)


class TestVisionValidation(unittest.TestCase):
    def test_operation_mode_rejects_typo(self):
        cfg = _cfg()
        cfg["operation_mode"] = "UI"
        self.assertTrue(any("operation_mode" in m for m in _errors(validate_config(cfg))))

    def test_ui_mode_is_accepted_by_schema(self):
        cfg = _cfg()
        cfg["operation_mode"] = "ui"
        self.assertEqual(_errors(validate_config(cfg)), [])

    def test_stay_ms_below_render_floor_rejected(self):
        cfg = _cfg()
        cfg["vision"]["stay_ms"] = 60
        self.assertTrue(any("stay_ms" in m for m in _errors(validate_config(cfg))))

    def test_hotbar_slot_range(self):
        for bad in (0, 10, 2.5, "7"):
            cfg = _cfg()
            cfg["vision"]["hotbar_slot"] = bad
            self.assertTrue(
                any("hotbar_slot" in m for m in _errors(validate_config(cfg))),
                f"hotbar_slot={bad!r} 未被校验",
            )

    def test_sweep_gap_ratio_range(self):
        for bad in (0, 1.5, -0.1, "1.0"):
            cfg = _cfg()
            cfg["vision"]["sweep_gap_ratio"] = bad
            self.assertTrue(
                any("sweep_gap_ratio" in m for m in _errors(validate_config(cfg))),
                f"sweep_gap_ratio={bad!r} 未被校验",
            )

    def test_low_confidence_range(self):
        for bad in (-0.1, 1.1):
            cfg = _cfg()
            cfg["vision"]["low_confidence"] = bad
            self.assertTrue(any("low_confidence" in m for m in _errors(validate_config(cfg))))

    def test_max_misclick_must_be_positive(self):
        cfg = _cfg()
        cfg["vision"]["max_misclick"] = 0
        self.assertTrue(any("max_misclick" in m for m in _errors(validate_config(cfg))))

    def test_kick_verify_enum(self):
        cfg = _cfg()
        cfg["vision"]["kick_verify"] = "guess"
        self.assertTrue(any("kick_verify" in m for m in _errors(validate_config(cfg))))

    def test_vision_section_wrong_type_reports_instead_of_crashing(self):
        for bad in ("字符串", ["列表"], 42):
            cfg = _cfg()
            cfg["vision"] = bad
            self.assertTrue(
                any("vision" in m for m in _errors(validate_config(cfg))),
                f"vision={bad!r} 未报错",
            )


class TestCapabilityGate(unittest.TestCase):
    def test_command_mode_always_supported(self):
        ensure_operation_mode_supported("command")

    def test_ui_mode_supported_when_pipeline_wired(self):
        """US-007 已接线:含 vision 的构建放行 ui 模式(启动门禁另有 OCR 检查)。"""
        self.assertTrue(has_vision_capability())
        ensure_operation_mode_supported("ui")

    def test_ui_mode_rejected_when_build_lacks_vision(self):
        """构建裁剪掉 vision 时仍须报错,不得静默按指令模式跑。"""
        with self.assertRaises(CapabilityError):
            ensure_operation_mode_supported("ui", vision=False)

    def test_ui_mode_allowed_when_capability_injected(self):
        ensure_operation_mode_supported("ui", vision=True)

    def test_invalid_mode_rejected(self):
        with self.assertRaises(CapabilityError):
            ensure_operation_mode_supported("ui-ish")


class TestStartupGate(unittest.TestCase):
    """真实入口串联:operation_mode="ui" 且没有 OCR 时启动即退出(exit 1)。"""

    def _run(self, *, ocr_available):
        import guild_auto.main as main_mod

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "config.json")
        cfg = _cfg()
        cfg["operation_mode"] = "ui"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)

        args = mock.Mock(
            config=path,
            admin=None,
            log=None,
            options=None,
            dry_run=False,
            debug=False,
            debug_skip=None,
        )
        exit_fn = mock.Mock()
        console_fn = mock.Mock()
        with mock.patch(
            "guild_auto.vision.adapters.has_configured_ocr", return_value=ocr_available
        ):
            main_mod.prepare_config(args, console_fn=console_fn, exit_fn=exit_fn)
        printed = " ".join(str(cargs.args[0]) for cargs in console_fn.call_args_list)
        return exit_fn, printed

    def test_ui_mode_without_ocr_blocks_startup_with_clear_error(self):
        exit_fn, printed = self._run(ocr_available=False)
        exit_fn.assert_called_once_with(1)
        self.assertIn("UI 识别模式", printed, f"启动报错未说明原因: {printed}")
        self.assertIn("OCR", printed, f"启动报错未说明原因: {printed}")

    def test_ui_mode_with_ocr_configured_starts(self):
        exit_fn, _printed = self._run(ocr_available=True)
        exit_fn.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
