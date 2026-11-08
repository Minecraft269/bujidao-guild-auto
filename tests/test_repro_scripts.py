# -*- coding: utf-8 -*-
"""test_repro_scripts.py —— 复现脚本的「没验不能算过」语义(协议 F.4 / P0-5 第三件)

`scripts/repro_p0_3.py` 早先在没有真实样本时输出 `SKIP`,却**把它计入 PASS 且 EXIT=0**
(脚本自注"不影响退出码")—— 等于"没验也算过"。这里锁定修正后的契约:

  * 缺样本 → 该项独立 `[SKIP]`、**不计入 PASS 数**、退出码非零(2)
  * 有样本 → 5 项全 PASS、退出码 0

样本用合成目录喂进去(不写死任何本机路径,CLAUDE.md §6)。
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "repro_p0_3.py")


def _run(extra_env):
    env = {k: v for k, v in os.environ.items() if k != "GUILD_REAL_LOG_DIR"}
    env.update(extra_env)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return subprocess.run(
        [sys.executable, SCRIPT],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=REPO,
        timeout=180,
    )


def _synthetic_samples_dir():
    """造一份"GBK 正文 + GBK 未分配字节"的样本(与真实网易版日志同一类)。

    0xAC 0xDF 在 gbk 里未分配 → gbk 严格解码失败 → 判定 gb18030、0 个替换符。
    """
    d = tempfile.mkdtemp(prefix="repro_samples_")
    body = "[01 Jan 2026] [main/INFO]: [CHAT] 公会面板中文\n".encode("gbk")
    body += b"\xac\xdf" + "[CHAT] 成员贡献\n".encode("gbk")
    with open(os.path.join(d, "latest.log.1"), "wb") as f:
        f.write(body)
    return d


class TestReproSkipsAreNotPasses(unittest.TestCase):
    def test_missing_samples_is_nonzero_and_not_counted_as_pass(self):
        r = _run({})
        self.assertNotEqual(r.returncode, 0, f"缺样本却 EXIT=0,等于没验也算过:\n{r.stdout}")
        self.assertIn("[SKIP]", r.stdout, f"缺少独立的 SKIPPED 状态:\n{r.stdout}")
        self.assertIn("4 passed", r.stdout, f"未验项被计入了 PASS:\n{r.stdout}")
        self.assertIn("1 skipped", r.stdout, f"汇总行缺少 skipped 计数:\n{r.stdout}")

    def test_with_samples_all_pass(self):
        d = _synthetic_samples_dir()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        r = _run({"GUILD_REAL_LOG_DIR": d})
        self.assertEqual(r.returncode, 0, f"有样本却没全过:\n{r.stdout}\n{r.stderr}")
        self.assertIn("5 passed", r.stdout, f"样本项未计入 PASS:\n{r.stdout}")
        self.assertIn("0 skipped", r.stdout, f"有样本却仍报 skipped:\n{r.stdout}")
        self.assertIn("gb18030", r.stdout, f"合成样本应判 gb18030:\n{r.stdout}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
