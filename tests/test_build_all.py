# -*- coding: utf-8 -*-
"""
test_build_all.py —— 本地全变体构建脚本(scripts/build_all.py, GOALS P6-2)
=========================================================================
覆盖:
  * 变体集合/产物名由注册表现算,不是写死清单(扩展注册表后自动多出组合);
  * 只有全能力变体带 -Full 后缀;
  * 单个变体失败不中断其余,INDEX.md 标 FAILED、main() 返回非零失败数;
  * INDEX.md 的 sha256/字节数与实测一致;
  * build.bat 的 all 入口指向 build_all.py,默认行为仍在。

数据全部为合成值,不跑真实 PyInstaller。
"""

import hashlib
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from guild_auto import capabilities as cap_mod

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build_all.py"
BUILD_BAT = REPO_ROOT / "build.bat"


def _load_build_all():
    """按文件路径加载脚本模块(scripts/ 不是包)。"""
    spec = importlib.util.spec_from_file_location("build_all_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # dataclass 在装饰期要通过 sys.modules[cls.__module__] 取模块命名空间
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestPlanFromRegistry(unittest.TestCase):
    """计划必须与注册表逐条一致 —— 脚本里没有变体名/固定条数。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_build_all()

    def test_plan_matches_registry(self):
        expected = [(name, caps) for name, caps in cap_mod.iter_variants()]
        actual = [(v.name, v.capabilities) for v in self.mod.plan()]
        self.assertEqual(actual, expected)

    def test_exe_name_follows_naming_rule(self):
        for v in self.mod.plan():
            self.assertEqual(v.exe_name, f"{self.mod.APP_NAME}-{v.name}{v.suffix}.exe")

    def test_only_full_variant_gets_suffix(self):
        full = cap_mod.aliases()["full"]
        for v in self.mod.plan():
            self.assertEqual(v.suffix, "-Full" if v.name == full else "")

    def test_registry_extension_adds_variants_without_code_change(self):
        """临时注册一个假能力 probe → 计划自动翻倍,脚本一行不改。"""
        base = self.mod.plan()
        with (
            mock.patch.dict(cap_mod._CAPABILITIES, {"probe": "Probe"}),
            mock.patch.object(cap_mod, "_APPEND_ORDER", (*cap_mod._APPEND_ORDER, "probe")),
        ):
            grown = self.mod.plan()
        self.assertEqual(len(grown), len(base) * 2)
        probe_names = [v.name for v in grown if v.name.endswith("-Probe") or "-Probe-" in v.name]
        self.assertEqual(len(probe_names), len(base))

    def test_source_has_no_variant_name_literals(self):
        src = SCRIPT.read_text(encoding="utf-8")
        for name, _caps in cap_mod.iter_variants():
            self.assertNotIn(f'"{name}"', src)
            self.assertNotIn(f"'{name}'", src)

    def test_cli_list_matches_plan(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--list"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        rows = [line.split("\t") for line in proc.stdout.strip().splitlines()]
        self.assertEqual([r[0] for r in rows], [v.name for v in self.mod.plan()])
        self.assertEqual([r[1] for r in rows], [v.exe_name for v in self.mod.plan()])


class TestSelect(unittest.TestCase):
    """--only 只能按注册表里的名字挑;未知名字必须报错,不得静默忽略。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_build_all()

    def test_unknown_variant_rejected(self):
        with self.assertRaises(ValueError):
            self.mod.select(self.mod.plan(), ["No-Such-Variant"])

    def test_keeps_registry_order(self):
        variants = self.mod.plan()
        picked = self.mod.select(variants, [variants[2].name, variants[0].name])
        self.assertEqual([v.name for v in picked], [variants[0].name, variants[2].name])

    def test_empty_means_all(self):
        variants = self.mod.plan()
        self.assertEqual(self.mod.select(variants, []), variants)


class TestCopyRetry(unittest.TestCase):
    """Windows 杀软短暂占用新 exe → 复制重试,而不是把成功产物判失败。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_build_all()

    def test_retries_permission_error_then_succeeds(self):
        calls = []

        def fake_copy2(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("locked")

        with mock.patch.object(self.mod.shutil, "copy2", side_effect=fake_copy2):
            self.mod._copy_with_retry(Path("src"), Path("dst"))
        self.assertEqual(len(calls), 3)

    def test_gives_up_after_timeout(self):
        with (
            mock.patch.object(self.mod.shutil, "copy2", side_effect=PermissionError("locked")),
            mock.patch.object(self.mod.time, "sleep", lambda _s: None),
        ):
            with self.assertRaises(PermissionError):
                self.mod._copy_with_retry(Path("src"), Path("dst"), timeout=0)


class TestFailureIsolation(unittest.TestCase):
    """一个变体失败 → 其余照常完成,汇总非零,索引标 FAILED。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_build_all()

    def _fake_build(self, fail_name):
        def build_fn(v, root):
            if v.name == fail_name:
                raise RuntimeError("模拟构建失败: icon 缺失")
            target = Path(root) / v.name / v.exe_name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fake-exe-" + v.name.encode("utf-8"))
            return self.mod.Row(variant=v.name, path=target, seconds=1.5)

        return build_fn

    def test_failure_does_not_stop_others(self):
        variants = self.mod.plan()
        self.assertGreater(len(variants), 1)
        fail_name = variants[1].name
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            rows = self.mod.run_plan(
                variants,
                out,
                verify=True,
                build_fn=self._fake_build(fail_name),
                verify_fn=lambda v, exe: (True, f"横幅={v.name}"),
            )
            self.assertEqual(len(rows), len(variants))
            self.assertEqual([r.variant for r in rows if not r.ok], [fail_name])
            index = self.mod.render_index(rows, out, "2026-11-21 12:00:00", 12.0)
        self.assertIn(f"| {fail_name} | - | - | - | 0.0s | - | FAILED: ", index)
        self.assertEqual(index.count("| OK |"), len(variants) - 1)
        self.assertIn(f"成功 / 失败: {len(variants) - 1} / 1", index)

    def test_main_returns_failure_count_and_writes_index(self):
        variants = self.mod.plan()[:2]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            ok_file = out / "ok.bin"
            ok_file.write_bytes(b"payload")

            def fake_run(plan_variants, out_root, **kwargs):
                return [
                    self.mod.Row(
                        variant=variants[0].name, path=ok_file, seconds=2.0, verify="横幅=x"
                    ),
                    self.mod.Row(variant=variants[1].name, error="模拟失败"),
                ]

            with (
                mock.patch.object(self.mod, "plan", return_value=variants),
                mock.patch.object(self.mod, "run_plan", side_effect=fake_run),
            ):
                rc = self.mod.main(["--out", tmp, "--no-verify"])
            index = (out / "INDEX.md").read_text(encoding="utf-8")
        self.assertEqual(rc, 1)
        self.assertIn("FAILED: 模拟失败", index)


class TestIndexContent(unittest.TestCase):
    """INDEX.md 的字节数/sha256 必须与实测一致。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_build_all()

    def test_index_sha256_and_size_match_disk(self):
        payload = b"index-test-payload"
        expected_sha = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            target = out / "SomeVariant" / "x.exe"
            target.parent.mkdir(parents=True)
            target.write_bytes(payload)
            row = self.mod.Row(
                variant="SomeVariant", path=target, seconds=3.25, verify="横幅=SomeVariant"
            )
            index = self.mod.render_index([row], out, "2026-11-21 12:00:00", 3.25)
        self.assertIn(f"| {len(payload)} |", index)
        self.assertIn(expected_sha, index)
        self.assertIn("3.2s", index)


class TestBuildBatWiring(unittest.TestCase):
    """build.bat 必须有 all 入口,默认构建行为保持原样。"""

    def test_all_entry_points_to_build_all(self):
        text = BUILD_BAT.read_bytes().decode("gbk")
        self.assertIn("build_all.py", text)
        self.assertIn(":all", text)
        # 默认行为(不带参数)仍构建单个全能力变体:APP_NAME 与 pyinstaller 都在
        self.assertIn("set APP_NAME=", text)
        self.assertIn("pyinstaller", text.lower())


if __name__ == "__main__":
    unittest.main()
