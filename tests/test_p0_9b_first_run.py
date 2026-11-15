# -*- coding: utf-8 -*-
"""P0-9b 首启回归修复 —— 回归测试

P0-9 把「模板占位符」定成启动 error，但出厂默认 game_log 就是占位符 → 首次运行
永远进不了 GUI 配置页，P0-4d 的首启引导被作废。P0-9b 的口径：

  * created=True（本次刚生成默认配置）→ 占位符**降级为 warning**，放行进配置页；
  * 用户点「开始」后按"非首启"**再校一次**，占位符仍是 error 就拒绝启动、不进 S8 等待；
  * created=False 保持 P0-9 原样：占位符即 error；
  * 判定仍只有一份（schema._looks_like_placeholder），这里只降级别。
"""

import copy
import os
import tempfile
import unittest
from unittest import mock

from guild_auto import main as main_mod
from guild_auto.config import validate_config
from guild_auto.config.defaults import DEFAULT_CONFIG
from tests.support_cfg import default_cfg, valid_game_log


def _placeholder_cfg():
    return copy.deepcopy(DEFAULT_CONFIG)  # 默认值本身就是占位符


class TestSchemaDowngradeEntry(unittest.TestCase):
    def test_first_run_downgrades_to_warning(self):
        issues = validate_config(_placeholder_cfg(), allow_placeholder_game_log=True)
        levels = [lvl for lvl, m in issues if "占位符" in m]
        self.assertEqual(levels, ["warning"], f"首启应是 warning，实际 {levels}")
        msg = [m for _lvl, m in issues if "占位符" in m][0]
        self.assertIn("game_log", msg)
        self.assertIn("首次运行已放行", msg)

    def test_non_first_run_stays_error(self):
        issues = validate_config(_placeholder_cfg())
        levels = [lvl for lvl, m in issues if "占位符" in m]
        self.assertEqual(levels, ["error"], "非首启必须仍是 error（P0-9 口径不变）")

    def test_real_path_unaffected_by_downgrade_flag(self):
        cfg = default_cfg()
        cfg["game_log"] = valid_game_log()
        self.assertEqual(
            [
                m
                for lvl, m in validate_config(cfg, allow_placeholder_game_log=True)
                if lvl == "error"
            ],
            [],
        )


class TestPrepareConfigFirstRun(unittest.TestCase):
    def test_first_run_does_not_exit_and_warns(self):
        """空目录首启：不退出（放行进配置页），控制台/日志里是 warning 不是 error。"""
        d = tempfile.mkdtemp(prefix="p0_9b_")
        path = os.path.join(d, "config.json")
        printed = []

        def _exit(code=0):
            raise AssertionError(f"首启不应退出，却 exit({code})")

        cfg = main_mod.prepare_config(
            mock.Mock(
                config=path,
                admin=None,
                log=None,
                options=None,
                dry_run=False,
                debug=False,
                debug_skip=None,
                ui=None,
            ),
            console_fn=lambda *a, **k: printed.append((str(a[0]), k.get("level"))),
            exit_fn=_exit,
        )
        joined = "\n".join(t for t, _lvl in printed)
        self.assertIn("占位符", joined)
        self.assertIn("[警告]", joined, "首启占位符必须是警告不是错误")
        self.assertNotIn("[错误]", joined)
        self.assertIn("配置页", joined, "首启仍应进入配置页")
        self.assertEqual(cfg["game_log"], DEFAULT_CONFIG["game_log"])


class TestPrestartRecheck(unittest.TestCase):
    """点「开始」后的复校：让 main() 走 UI 分支，桥返回 True。"""

    class _Bridge:
        instances = []

        def __init__(self, *a, **k):
            self.pushed = []
            self.server = None
            TestPrestartRecheck._Bridge.instances.append(self)

        def launch(self, **k):
            return True  # 用户点了「开始」

        def push_state(self, **kw):
            self.pushed.append(kw)

        def event(self, *a, **k):
            pass

    def _run_main(self, cfg):
        from guild_auto.ui import binding as ui_binding

        self._Bridge.instances.clear()
        args = mock.Mock()
        args.ui = False
        args.config = "X.json"
        args.debug = False
        args.debug_skip = None
        args.dry_run = False
        patches = [
            mock.patch.object(main_mod, "parse_args", lambda: args),
            mock.patch.object(main_mod, "prepare_config", lambda a: copy.deepcopy(cfg)),
            mock.patch.object(main_mod, "console", lambda *a, **k: None),
            mock.patch.object(main_mod.logger, "log_error"),
            mock.patch.object(main_mod.logger, "log_info"),
            mock.patch.object(main_mod.logger, "log_debug"),
            mock.patch.object(ui_binding, "gui_executable", lambda: "Gui.exe"),
            mock.patch.object(ui_binding, "GuiBridge", self._Bridge),
            mock.patch.object(ui_binding, "set_active", lambda b: None),
            mock.patch.object(ui_binding, "RuntimeController", lambda: object()),
            mock.patch.object(main_mod.config_mod, "load_config", lambda p: copy.deepcopy(cfg)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_start_with_placeholder_is_rejected_before_s8(self):
        self._run_main(_placeholder_cfg())
        with mock.patch.object(
            main_mod.runtime_mod, "build_runtime", side_effect=AssertionError("不该进入 S8")
        ):
            with self.assertRaises(SystemExit) as ctx:
                main_mod.main()
        self.assertEqual(ctx.exception.code, 1, "占位符未修正就点开始：必须拒绝启动")
        pushed = self._Bridge.instances[0].pushed
        self.assertTrue(any("game_log" in str(p) for p in pushed), f"界面没收到原因: {pushed}")

    def test_start_with_real_path_proceeds(self):
        cfg = default_cfg()
        cfg["game_log"] = valid_game_log()
        self._run_main(cfg)
        with mock.patch.object(
            main_mod.runtime_mod, "build_runtime", side_effect=RuntimeError("stop-here")
        ) as br:
            with self.assertRaises(RuntimeError):
                main_mod.main()
        br.assert_called_once()  # 走到了 S7，没有被首启校验误拦


class TestSingleSourceOfTruth(unittest.TestCase):
    def test_placeholder_rule_lives_in_one_place(self):
        """降级不得复制第二套判定：schema 里只有一处 _PLACEHOLDER_SEGMENTS。"""
        import guild_auto.config.schema as schema_mod

        with open(schema_mod.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertEqual(src.count("_PLACEHOLDER_SEGMENTS"), 2, "应只有定义 + 一处使用")
        self.assertEqual(src.count("def _looks_like_placeholder"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
