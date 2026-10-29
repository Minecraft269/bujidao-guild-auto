# -*- coding: utf-8 -*-
"""
test_capabilities.py —— 能力注册表 / 变体矩阵 / 启动门禁(阶段 7)
================================================================
覆盖:
  * 8 种能力组合的拼接名逐个断言(拼接顺序固定,名称是唯一权威标识);
  * 未设置能力清单时默认全能力(源码运行);
  * 打包期常量作为环境变量缺省时的回退;
  * 三种"配置要求了构建不支持的能力"场景启动即报错,且错误信息含能力标识;
  * 启动横幅与第三份报告 run_meta 同源(避免两处漂移);
  * 构建矩阵由注册表推导(scripts/build-variants.py),不是手写清单;
  * domain / application 不得模块级 import ui / vision / concurrency。

数据全部为合成值,无真实数据。
"""

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from guild_auto import capabilities as cap_mod
from guild_auto.config import DEFAULT_CONFIG

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build-variants.py"

# 计划文档给出的 8 种组合 —— 顺序即拼接顺序。
EXPECTED_VARIANTS = [
    ("Lite", ("core",)),
    ("Lite-Async", ("core", "async")),
    ("Lite-Gui", ("core", "gui")),
    ("Lite-Vision", ("core", "vision")),
    ("Lite-Async-Gui", ("core", "async", "gui")),
    ("Lite-Async-Vision", ("core", "async", "vision")),
    ("Lite-Gui-Vision", ("core", "gui", "vision")),
    ("Lite-Async-Gui-Vision", ("core", "async", "gui", "vision")),
]


@contextmanager
def caps_env(value):
    """临时设置能力环境变量;value=None 表示彻底未设置(源码运行)。"""
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop(cap_mod.ENV_VAR, None)
        if value is not None:
            os.environ[cap_mod.ENV_VAR] = value
        # 仓库里没有 _build_capabilities.py;显式压掉回退,避免本地残留影响断言。
        with mock.patch.object(cap_mod, "_build_time_capabilities", return_value=None):
            yield


def _cfg():
    return json.loads(json.dumps(DEFAULT_CONFIG))


class TestVariantNames(unittest.TestCase):
    def test_all_eight_combinations_in_fixed_order(self):
        for name, caps in EXPECTED_VARIANTS:
            self.assertEqual(cap_mod.build_variant_name(caps), name)

    def test_input_order_does_not_change_name(self):
        shuffled = ("vision", "core", "gui", "async")
        self.assertEqual(cap_mod.build_variant_name(shuffled), "Lite-Async-Gui-Vision")

    def test_core_only_is_base_name(self):
        self.assertEqual(cap_mod.build_variant_name(("core",)), "Lite")

    def test_unknown_capability_rejected(self):
        with self.assertRaises(cap_mod.CapabilityError):
            cap_mod.build_variant_name(("core", "asycn"))

    def test_iter_variants_matches_expected_list(self):
        self.assertEqual(list(cap_mod.iter_variants()), EXPECTED_VARIANTS)

    def test_name_derived_from_enabled_when_caps_omitted(self):
        with caps_env("core,vision"):
            self.assertEqual(cap_mod.build_variant_name(), "Lite-Vision")


class TestEnabledCapabilities(unittest.TestCase):
    def test_unset_means_all_capabilities(self):
        with caps_env(None):
            self.assertEqual(cap_mod.enabled_capabilities(), ("core", "async", "gui", "vision"))

    def test_env_list_parsed_in_registry_order(self):
        with caps_env("vision,async"):
            self.assertEqual(cap_mod.enabled_capabilities(), ("core", "async", "vision"))
            self.assertTrue(cap_mod.has("async"))
            self.assertFalse(cap_mod.has("gui"))

    def test_core_is_implicit_even_if_omitted(self):
        with caps_env("gui"):
            self.assertEqual(cap_mod.enabled_capabilities(), ("core", "gui"))

    def test_empty_value_means_core_only(self):
        with caps_env(""):
            self.assertEqual(cap_mod.enabled_capabilities(), ("core",))

    def test_unknown_token_raises_with_token_name(self):
        with caps_env("core,web"):
            with self.assertRaises(cap_mod.CapabilityError) as ctx:
                cap_mod.enabled_capabilities()
        self.assertIn("web", str(ctx.exception))

    def test_build_time_constant_used_when_env_unset(self):
        """产物运行时读不到 CI env:打包期常量必须作为回退。"""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(cap_mod.ENV_VAR, None)
            with mock.patch.object(cap_mod, "_build_time_capabilities", return_value="core,async"):
                self.assertEqual(cap_mod.enabled_capabilities(), ("core", "async"))


class TestRequire(unittest.TestCase):
    def test_present_capability_passes(self):
        with caps_env("core,vision"):
            cap_mod.require("vision", "UI识别模式")

    def test_missing_capability_message_names_cap_and_feature(self):
        with caps_env("core"):
            with self.assertRaises(cap_mod.CapabilityError) as ctx:
                cap_mod.require("vision", "UI识别模式")
        message = str(ctx.exception)
        self.assertIn("vision", message)
        self.assertIn("UI识别模式", message)
        self.assertIn("Lite", message, "报错必须写明当前构建变体")


class TestStartupGates(unittest.TestCase):
    """真实入口串联:能力缺失 + 配置强制要求 → prepare_config 退出 1。"""

    def _run_prepare(self, cfg, ui_flag=False):
        import guild_auto.main as main_mod

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "config.json")
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
            ui=ui_flag,
        )
        exit_fn = mock.Mock()
        console_fn = mock.Mock()
        main_mod.prepare_config(args, console_fn=console_fn, exit_fn=exit_fn)
        printed = " ".join(str(c.args[0]) for c in console_fn.call_args_list)
        return exit_fn, printed, console_fn

    def test_ui_operation_mode_without_vision_errors(self):
        cfg = _cfg()
        cfg["operation_mode"] = "ui"
        with caps_env("core"):
            exit_fn, printed, _ = self._run_prepare(cfg)
        exit_fn.assert_called_once_with(1)
        self.assertIn("vision", printed)
        self.assertIn("UI 识别模式", printed)

    def test_async_enabled_without_async_errors(self):
        cfg = _cfg()
        cfg["async"]["enabled"] = True
        with caps_env("core,gui"):
            exit_fn, printed, _ = self._run_prepare(cfg)
        exit_fn.assert_called_once_with(1)
        self.assertIn("async", printed)
        self.assertIn("异步模式", printed)

    def test_ui_flag_without_gui_errors(self):
        with caps_env("core,async"):
            exit_fn, printed, _ = self._run_prepare(_cfg(), ui_flag=True)
        exit_fn.assert_called_once_with(1)
        self.assertIn("gui", printed)
        self.assertIn("--ui", printed)

    def test_ui_mode_requires_ocr_adapter(self):
        """主流程已接线,但未配置 OCR → 启动即报错,不得静默降级回指令模式。

        与 tests/test_vision_config_capabilities.py 的启动门禁同一条护栏:
        能力矩阵放行只代表"构建含 vision",不代表有可用的文字识别能力。
        """
        cfg = _cfg()
        cfg["operation_mode"] = "ui"
        with (
            caps_env(None),
            mock.patch("guild_auto.vision.adapters.has_configured_ocr", return_value=False),
        ):
            exit_fn, printed, _ = self._run_prepare(cfg)
        exit_fn.assert_called_once_with(1)
        self.assertIn("UI 识别模式", printed)
        self.assertIn("OCR", printed)

    def test_ui_mode_starts_when_ocr_configured(self):
        """能力齐 + OCR 可用 → UI 模式不再被启动门禁拦下(允许进入主流程)。"""
        cfg = _cfg()
        cfg["operation_mode"] = "ui"
        with (
            caps_env(None),
            mock.patch("guild_auto.vision.adapters.has_configured_ocr", return_value=True),
        ):
            exit_fn, _printed, _ = self._run_prepare(cfg)
        exit_fn.assert_not_called()

    def test_banner_matches_variant_meta(self):
        with caps_env("core,async"):
            _, printed, _ = self._run_prepare(_cfg())
            meta = cap_mod.variant_meta()
            banner = cap_mod.build_banner()
        self.assertEqual(meta["build_variant"], "Lite-Async")
        self.assertEqual(meta["capabilities"], ["core", "async"])
        self.assertEqual(banner, "构建变体: Lite-Async    已启用能力: core,async")
        self.assertIn(banner, printed, "启动横幅必须打印变体与能力清单")


class TestReportMetaSource(unittest.TestCase):
    """第三份报告的 build_variant/capabilities 必须取真实值,不能写死。"""

    def test_main_uses_capabilities_meta(self):
        source = (REPO_ROOT / "src" / "guild_auto" / "main.py").read_text(encoding="utf-8")
        run_meta_block = source[source.index('"started_at": run_started') :]
        self.assertIn("capabilities_mod.variant_meta()", run_meta_block)
        self.assertNotIn('"build_variant": "source"', source, "构建变体不得再写死")


class TestVariantMatrixScript(unittest.TestCase):
    """CI matrix 必须由注册表推导 —— 脚本输出与 iter_variants() 完全一致。"""

    def test_list_outputs_registry_derived_matrix(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--list"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        matrix = json.loads(proc.stdout.strip())
        self.assertEqual(
            [(item["variant"], item["capabilities"]) for item in matrix["include"]],
            [(name, ",".join(caps)) for name, caps in EXPECTED_VARIANTS],
        )

    def test_write_const_bakes_capabilities(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "pkg", "_build_capabilities.py")
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--write-const",
                    target,
                    "--capabilities",
                    "async,core",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
            )
            namespace = {}
            with open(target, encoding="utf-8") as f:
                exec(compile(f.read(), target, "exec"), namespace)
            self.assertEqual(namespace["CAPABILITIES"], "core,async")


class TestLayerIsolation(unittest.TestCase):
    """core 层(domain / application)不得模块级 import 可选单元。

    函数内延迟 import 允许(避免循环依赖/可选依赖),因此只扫模块顶层 AST 节点。
    """

    FORBIDDEN = ("guild_auto.ui", "guild_auto.vision", "guild_auto.concurrency")

    def _violations(self, path):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:  # 只看模块级;函数体里的 import 不在 tree.body
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith(self.FORBIDDEN):
                    yield f"{path.name}:{node.lineno} from {node.module}"
                elif node.module == "guild_auto":
                    for alias in node.names:
                        if f"guild_auto.{alias.name}".startswith(self.FORBIDDEN):
                            yield f"{path.name}:{node.lineno} from guild_auto import {alias.name}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(self.FORBIDDEN):
                        yield f"{path.name}:{node.lineno} import {alias.name}"

    def test_domain_and_application_are_isolated(self):
        violations = []
        for layer in ("domain", "application"):
            for path in sorted((REPO_ROOT / "src" / "guild_auto" / layer).glob("*.py")):
                violations.extend(self._violations(path))
        self.assertEqual(
            violations, [], "core 层出现模块级可选单元 import:\n" + "\n".join(violations)
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
