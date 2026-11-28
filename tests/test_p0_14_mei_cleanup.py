# -*- coding: utf-8 -*-
"""test_p0_14_mei_cleanup.py —— onefile 残留处置（GOALS P0-14）

锁定四条语义：
  * 启动期自愈只删「无进程持有 + 超过 10 分钟」的 `_MEI*` 残留；运行中的实例
    （有进程加载了其中模块）绝不动手；本进程自己的解包目录永远排除；
  * 进程枚举不可信（`_process_module_dirs() is None`）时**整轮不删**（fail-closed）；
  * 删除失败（典型：安全软件实时扫描持有句柄）**必须静默**：不抛、不打印、
    不弹框、不阻塞启动；自愈在 daemon 线程里跑，不拖慢首屏；
  * `scripts/diag_mei_leftover.py` 默认只看不删（`--delete` 才删）、`--min-age`
    默认 600s，判定区分 IN_USE / LOCKED / UNKNOWN / REMOVABLE / REMOVED / FAILED。

数据全部为临时目录里的合成文件，不碰真实 %TEMP%、不含真实路径。
"""

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from guild_auto import main as main_mod

REPO_ROOT = Path(__file__).resolve().parents[1]
DIAG_SCRIPT = REPO_ROOT / "scripts" / "diag_mei_leftover.py"
_STUB_FILE = "api-ms-win-core-fibers-l1-1-0.dll"
_RUNTIME_DLL = "python312.dll"


def _make_mei(
    root: Path,
    name: str = "_MEI0000test",
    age_s: float = 700.0,
    files: tuple[str, ...] = (_STUB_FILE, _RUNTIME_DLL),
) -> Path:
    """在 root 下造一个 _MEI* 残留目录；mtime 回拨以伪造年龄（合成内容）。"""
    path = root / name
    path.mkdir()
    for fname in files:
        (path / fname).write_bytes(b"synthetic")
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return path


def _load_diag():
    spec = importlib.util.spec_from_file_location("diag_mei_under_test", DIAG_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestCleanupStaleMeiDirs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="p0_14_cleanup_")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _cleanup(self, **kwargs):
        with mock.patch.object(main_mod, "_process_module_dirs", return_value=set()):
            return main_mod.cleanup_stale_mei_dirs(
                now=time.time(), min_age_s=600, roots=[str(self.root)], **kwargs
            )

    def test_removes_stale_leftover(self):
        leftover = _make_mei(self.root)
        result = self._cleanup()
        self.assertFalse(leftover.exists(), "超过 10 分钟、无进程持有的残留应被删除")
        self.assertEqual(len(result["removed"]), 1)

    def test_keeps_fresh_leftover(self):
        fresh = _make_mei(self.root, name="_MEI0000fresh", age_s=60)
        result = self._cleanup()
        self.assertTrue(fresh.exists(), "10 分钟内的残留可能是另一个实例刚解包的，不能动")
        self.assertEqual(result, {"removed": [], "in_use": [], "failed": []})

    def test_exact_age_boundary(self):
        """年龄判据是 >=600s：600s 删、599s 留（用固定 now 锁边界，不靠时钟运气）。"""
        exact = _make_mei(self.root, name="_MEI0000exact", age_s=600)
        just_under = _make_mei(self.root, name="_MEI0000under", age_s=599)
        fixed_now = time.time()
        with mock.patch.object(main_mod, "_process_module_dirs", return_value=set()):
            main_mod.cleanup_stale_mei_dirs(now=fixed_now, min_age_s=600, roots=[str(self.root)])
        self.assertFalse(exact.exists(), "恰好 600s 的残留应删除（判据 >=）")
        self.assertTrue(just_under.exists(), "599s 的残留不得删除")

    def test_keeps_dir_used_by_running_process(self):
        running = _make_mei(self.root, name="_MEI0000running")
        in_use = {main_mod._path_key(running)}
        with mock.patch.object(main_mod, "_process_module_dirs", return_value=in_use):
            result = main_mod.cleanup_stale_mei_dirs(
                now=time.time(), min_age_s=600, roots=[str(self.root)]
            )
        self.assertTrue(running.exists(), "有进程加载了其中模块 → 绝不动手")
        self.assertEqual(len(result["in_use"]), 1)
        self.assertEqual(result["removed"], [])

    def test_unreliable_enumeration_deletes_nothing(self):
        leftover = _make_mei(self.root, name="_MEI0000unknown")
        with mock.patch.object(main_mod, "_process_module_dirs", return_value=None):
            result = main_mod.cleanup_stale_mei_dirs(
                now=time.time(), min_age_s=600, roots=[str(self.root)]
            )
        self.assertTrue(leftover.exists(), "枚举不可信时必须整轮不删（fail-closed）")
        self.assertEqual(result, {"removed": [], "in_use": [], "failed": []})

    def test_never_touches_own_meipass(self):
        own = _make_mei(self.root, name="_MEI0000own")
        with mock.patch.object(sys, "_MEIPASS", str(own), create=True):
            result = self._cleanup()
        self.assertTrue(own.exists(), "本进程自己的解包目录永远排除")
        self.assertEqual(result, {"removed": [], "in_use": [], "failed": []})

    def test_delete_failure_is_recorded_not_raised(self):
        """真实失败路径：rmtree(ignore_errors=True) 静默返回、目录还在。"""
        leftover = _make_mei(self.root, name="_MEI0000locked")
        with mock.patch.object(main_mod.shutil, "rmtree", return_value=None):
            result = self._cleanup()
        self.assertTrue(leftover.exists())
        self.assertEqual(len(result["failed"]), 1)


class TestStartupSelfHeal(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="p0_14_heal_")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_stale_leftover_does_not_block_startup(self):
        leftover = _make_mei(self.root, name="_MEI0000startup")
        with (
            mock.patch.object(main_mod, "_mei_scan_roots", return_value=[str(self.root)]),
            mock.patch.object(main_mod, "_process_module_dirs", return_value=set()),
        ):
            main_mod._startup_self_heal()
        self.assertFalse(leftover.exists(), "启动自愈应清掉陈旧残留，且不抛异常")

    def test_cleanup_exception_is_swallowed_and_silent(self):
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(main_mod, "cleanup_stale_mei_dirs", side_effect=RuntimeError("boom")),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            main_mod._startup_self_heal()  # 不得抛出
        self.assertEqual(out.getvalue(), "", "自愈失败不得打印（静默）")
        self.assertEqual(err.getvalue(), "")

    def test_delete_failure_within_startup_never_notifies(self):
        leftover = _make_mei(self.root, name="_MEI0000notify")
        with (
            mock.patch.object(main_mod, "_mei_scan_roots", return_value=[str(self.root)]),
            mock.patch.object(main_mod, "_process_module_dirs", return_value=set()),
            mock.patch.object(main_mod.shutil, "rmtree", return_value=None),
            mock.patch.object(main_mod, "_notify_startup_failure") as notify,
        ):
            main_mod._startup_self_heal()
        self.assertTrue(leftover.exists())
        notify.assert_not_called()  # 删除失败绝不弹框（P0-11 的可见反馈也不参与）

    def test_main_schedules_self_heal(self):
        with (
            mock.patch.object(main_mod, "_schedule_startup_self_heal") as schedule,
            mock.patch.object(main_mod, "parse_args", side_effect=SystemExit(0)),
        ):
            with self.assertRaises(SystemExit):
                main_mod.main()
        schedule.assert_called_once()

    def test_schedule_runs_heal_in_daemon_thread(self):
        done = threading.Event()
        with mock.patch.object(main_mod, "_startup_self_heal", side_effect=done.set):
            thread = main_mod._schedule_startup_self_heal()
        self.assertIsNotNone(thread)
        self.assertTrue(thread.daemon)
        self.assertTrue(done.wait(5), "自愈线程应真正执行")
        thread.join(5)

    def test_schedule_failure_is_silent(self):
        with mock.patch.object(main_mod.threading, "Thread", side_effect=RuntimeError("no")):
            self.assertIsNone(main_mod._schedule_startup_self_heal())

    def test_main_starts_normally_when_cleanup_raises(self):
        with (
            mock.patch.object(main_mod, "cleanup_stale_mei_dirs", side_effect=RuntimeError),
            mock.patch.object(main_mod, "parse_args", side_effect=SystemExit(0)),
            mock.patch.object(main_mod, "_notify_startup_failure") as notify,
        ):
            with self.assertRaises(SystemExit):
                main_mod.main()
        notify.assert_not_called()


class TestScanRootsAndProcessScan(unittest.TestCase):
    def test_scan_roots_include_meipass_parent(self):
        with tempfile.TemporaryDirectory(prefix="p0_14_roots_") as tmp:
            mei = Path(tmp) / "_MEI0000roots"
            mei.mkdir()
            with mock.patch.object(sys, "_MEIPASS", str(mei), create=True):
                roots = main_mod._mei_scan_roots()
        got = {main_mod._path_key(r) for r in roots}
        self.assertIn(main_mod._path_key(tmp), got)
        self.assertIn(main_mod._path_key(tempfile.gettempdir()), got)

    def test_path_key_strips_extended_prefix(self):
        self.assertEqual(
            main_mod._path_key("\\\\?\\D:\\x\\_MEI1"), main_mod._path_key("D:\\x\\_MEI1")
        )
        self.assertTrue(main_mod._same_or_below("d:\\x\\_mei1", "d:\\x\\_mei1"))
        self.assertFalse(main_mod._same_or_below("d:\\x\\_mei10", "d:\\x\\_mei1"))

    @unittest.skipUnless(sys.platform == "win32", "模块枚举只在 Windows 上有意义")
    def test_process_module_dirs_is_trustworthy_or_none(self):
        dirs = main_mod._process_module_dirs()
        self.assertTrue(dirs is None or isinstance(dirs, set))
        if dirs is not None:
            self.assertIn(
                main_mod._path_key(os.path.dirname(sys.executable)),
                dirs,
                "连本进程的模块目录都没枚举到，Win32 调用链坏了",
            )


class TestDiagScript(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.diag = _load_diag()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="p0_14_diag_")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_scan_lists_only_mei_dirs(self):
        _make_mei(self.root, name="_MEI0000a", age_s=1)
        _make_mei(self.root, name="_MEI0000b", age_s=900)
        (self.root / "not_mei").mkdir()
        found = self.diag.scan(self.root, min_age=0)
        self.assertEqual([p.name for p in found], ["_MEI0000a", "_MEI0000b"])
        self.assertEqual([p.name for p in self.diag.scan(self.root, min_age=600)], ["_MEI0000b"])
        self.assertEqual(self.diag.DEFAULT_MIN_AGE, 600.0)

    def test_diagnose_removes_only_with_delete_flag(self):
        leftover = _make_mei(self.root, name="_MEI0000free")
        info = self.diag.diagnose(leftover, do_delete=False)
        self.assertEqual(info["verdict"], "REMOVABLE")
        self.assertTrue(leftover.exists(), "默认只看不删")
        info = self.diag.diagnose(leftover, do_delete=True)
        self.assertEqual(info["verdict"], "REMOVED")
        self.assertFalse(leftover.exists())

    def test_diagnose_reports_locked_file(self):
        leftover = _make_mei(self.root, name="_MEI0000locked")
        handle = open(leftover / _STUB_FILE, "rb")  # 不带 FILE_SHARE_DELETE 的独占句柄
        self.addCleanup(handle.close)
        info = self.diag.diagnose(leftover, do_delete=True)
        self.assertEqual(info["verdict"], "LOCKED", "被独占句柄持有的目录必须判 LOCKED")
        names = [name for name, _err in info["locked"]]
        self.assertIn(_STUB_FILE, names)
        self.assertTrue(leftover.exists(), "LOCKED 时不得删除目录")

    def test_diagnose_never_touches_dir_with_holder(self):
        leftover = _make_mei(self.root, name="_MEI0000busy")
        with mock.patch.object(self.diag, "process_holders", return_value=[(1234, "x.dll")]):
            info = self.diag.diagnose(leftover, do_delete=True)
        self.assertEqual(info["verdict"], "IN_USE")
        self.assertTrue(leftover.exists())

    def test_diagnose_unknown_when_enumeration_unreliable(self):
        leftover = _make_mei(self.root, name="_MEI0000unknown")
        with mock.patch.object(self.diag, "process_holders", return_value=None):
            info = self.diag.diagnose(leftover, do_delete=True)
        self.assertEqual(info["verdict"], "UNKNOWN")
        self.assertTrue(leftover.exists(), "枚举不可信时拒绝删除")

    def test_main_exit_codes(self):
        _make_mei(self.root, name="_MEI0000rc", age_s=900)
        with mock.patch.object(self.diag, "av_products", return_value=[]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = self.diag.main(["--root", str(self.root), "--min-age", "0"])
            self.assertEqual(rc, 1, f"默认只看不删，残留仍在 → 应返回 1:\n{buf.getvalue()}")
            self.assertIn("可删 1", buf.getvalue())
            self.assertIn("如需删除加 --delete", buf.getvalue())

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = self.diag.main(["--root", str(self.root), "--min-age", "0", "--delete"])
            self.assertEqual(rc, 0, f"删除后应返回 0:\n{buf.getvalue()}")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = self.diag.main(["--root", str(self.root), "--min-age", "0"])
            self.assertEqual(rc, 0)
            self.assertIn("没有残留目录", buf.getvalue())

    def test_main_returns_1_for_locked(self):
        leftover = _make_mei(self.root, name="_MEI0000lockedrc", age_s=900)
        handle = open(leftover / _STUB_FILE, "rb")
        self.addCleanup(handle.close)
        with mock.patch.object(self.diag, "av_products", return_value=[]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = self.diag.main(["--root", str(self.root), "--min-age", "0", "--delete"])
        self.assertEqual(rc, 1, f"LOCKED 残留必须返回 1:\n{buf.getvalue()}")
        self.assertIn("锁定 1", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
