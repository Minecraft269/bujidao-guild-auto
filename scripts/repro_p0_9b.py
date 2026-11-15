# -*- coding: utf-8 -*-
"""P0-9b 首启回归 —— 最小复现/验收脚本

直接跑:
    python scripts/repro_p0_9b.py

P0-9 把「模板占位符」定成启动 error，而出厂默认 game_log 就是占位符 —— 结果首次运行
永远进不了 GUI 配置页（P0-4d 的首启引导被作废）。P0-9b 的口径：
  created=True 的这一次把占位符降级为 warning 放行进配置页；点「开始」后按非首启
  再校一次，占位符仍是 error 就拒绝启动、不进 S8 等待；判定仍只有一份。

四项断言（末尾 PASS/FAIL 汇总，退出码 0/1）：
  1 空目录首启 → 不退出、日志里是占位符 warning（不是 error），并提示进配置页
  2 首启后不改配置点「开始」→ 拒绝启动（exit 1）、指出 game_log、无 S8 等待
  3 非首启仍是 error → 直接复用 scripts/repro_p0_9.py，要求 3/3 全绿
  4 占位符判定仍只有一处（schema 里 _PLACEHOLDER_SEGMENTS 定义 + 使用各一次）
"""

import contextlib
import copy
import io
import os
import subprocess
import sys
import tempfile
import time
import traceback
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from guild_auto import main as main_mod  # noqa: E402
from guild_auto.config.defaults import DEFAULT_CONFIG  # noqa: E402
from guild_auto.infrastructure import logger  # noqa: E402


def _args(config_path):
    return mock.Mock(
        config=config_path,
        admin=None,
        log=None,
        options=None,
        dry_run=False,
        debug=False,
        debug_skip=None,
        ui=None,
    )


# ---------------------------------------------------------------------------
# 1) 空目录首启：不退出 + 日志里是 warning
# ---------------------------------------------------------------------------
def check_first_run_does_not_exit():
    d = tempfile.mkdtemp(prefix="p0_9b_first_")
    path = os.path.join(d, "config.json")
    assert not os.path.exists(path), "前提：目录里不该有 config.json"
    old_cwd = os.getcwd()
    t0 = time.monotonic()
    buf = io.StringIO()
    try:
        os.chdir(d)  # 让 prepare_config 生成的 logs/ 落在临时目录里

        def _exit(code=0):
            raise AssertionError(f"首启不应退出，却 exit({code})")

        # 用**默认** console_fn：它既打印也按级别写日志文件（注入替身会绕过落盘，
        # 验收要求"日志含 warning"，必须走真链路）。
        with contextlib.redirect_stdout(buf):
            cfg = main_mod.prepare_config(_args(path), exit_fn=_exit)
    finally:
        os.chdir(old_cwd)
    took = time.monotonic() - t0
    text = buf.getvalue()
    assert "占位符" in text and "[警告]" in text, f"首启没有占位符 warning: {text[-400:]}"
    assert "[错误]" not in text, "首启仍把占位符当成错误"
    assert "配置页" in text, "首启没有继续进入配置页"
    assert cfg["game_log"] == DEFAULT_CONFIG["game_log"]
    log_text = ""
    log_path = os.path.join(d, "logs", "latest.log")
    if os.path.exists(log_path):
        with open(log_path, encoding="utf-8", errors="replace") as f:
            log_text = f.read()
    warn_lines = [ln for ln in log_text.splitlines() if "占位符" in ln and "Warning" in ln]
    assert warn_lines, "日志文件里没有占位符 warning 行"
    return f"未退出 用时 {took:.2f}s；日志行: {warn_lines[-1][:110]}"


# ---------------------------------------------------------------------------
# 2) 首启后不改配置点「开始」：拒绝启动、不进 S8 等待
# ---------------------------------------------------------------------------
def check_start_rejected_with_placeholder():
    printed = []
    placeholder_cfg = copy.deepcopy(DEFAULT_CONFIG)

    class _Bridge:
        def __init__(self, *a, **k):
            self.server = None
            self.pushed = []

        def launch(self, **k):
            return True  # 用户在配置页点了「开始」

        def push_state(self, **kw):
            self.pushed.append(kw)

        def event(self, *a, **k):
            pass

    from guild_auto.ui import binding as ui_binding

    args = _args("unused.json")
    patches = [
        mock.patch.object(main_mod, "parse_args", lambda: args),
        mock.patch.object(main_mod, "prepare_config", lambda a: copy.deepcopy(placeholder_cfg)),
        mock.patch.object(main_mod, "console", lambda *a, **k: printed.append(str(a[0]))),
        mock.patch.object(ui_binding, "gui_executable", lambda: "Gui.exe"),
        mock.patch.object(ui_binding, "GuiBridge", _Bridge),
        mock.patch.object(ui_binding, "set_active", lambda b: None),
        mock.patch.object(ui_binding, "RuntimeController", lambda: object()),
        mock.patch.object(
            main_mod.config_mod, "load_config", lambda p: copy.deepcopy(placeholder_cfg)
        ),
        mock.patch.object(
            main_mod.runtime_mod, "build_runtime", side_effect=AssertionError("不该进入 S7/S8")
        ),
    ]
    for p in patches:
        p.start()
    try:
        try:
            main_mod.main()
            code = None
        except SystemExit as e:
            code = e.code
        except AssertionError:
            raise AssertionError("占位符未修正却进入了 S7/S8（复校没生效）")
    finally:
        for p in reversed(patches):
            p.stop()

    text = "\n".join(printed)
    assert code == 1, f"应拒绝启动 exit(1)，实际 exit({code})"
    assert "game_log" in text and "占位符" in text, f"拒绝理由没点出 game_log: {text[-400:]}"
    assert "S8 日志等待" not in text, "被拒绝后仍进了 S8 等待"
    line = [ln for ln in printed if "占位符" in ln][0]
    return f"exit(1)；日志: {line[:110]}"


# ---------------------------------------------------------------------------
# 3) 非首启仍是 error：复用 repro_p0_9（3/3）
# ---------------------------------------------------------------------------
def check_non_first_run_unchanged():
    r = subprocess.run(
        [sys.executable, os.path.join(REPO, "scripts", "repro_p0_9.py")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=REPO,
        timeout=300,
        env={**os.environ, "PYTHONPATH": os.path.join(REPO, "src")},
    )
    assert r.returncode == 0, f"repro_p0_9 回退了：\n{r.stdout[-800:]}\n{r.stderr[-400:]}"
    assert "3 passed / 0 failed" in r.stdout, r.stdout[-400:]
    return "repro_p0_9.py 3/3 全绿，非首启口径未回退"


# ---------------------------------------------------------------------------
# 4) 占位符判定仍只有一处
# ---------------------------------------------------------------------------
def check_single_source():
    p = os.path.join(REPO, "src", "guild_auto", "config", "schema.py")
    with open(p, encoding="utf-8") as f:
        src = f.read()
    n = src.count("_PLACEHOLDER_SEGMENTS")
    assert n == 2, f"_PLACEHOLDER_SEGMENTS 出现 {n} 次（应为 定义+使用 各一次）"
    assert src.count("def _looks_like_placeholder") == 1
    return "_PLACEHOLDER_SEGMENTS 定义+使用各 1 处"


CHECKS = [
    ("空目录首启 → 不退出、占位符是 warning", check_first_run_does_not_exit),
    ("首启不改配置点「开始」→ 拒绝、不进 S8", check_start_rejected_with_placeholder),
    ("非首启仍是 error（复用 repro_p0_9）", check_non_first_run_unchanged),
    ("占位符判定唯一真源", check_single_source),
]


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    for _n in ("log_debug", "log_trace"):
        setattr(logger, _n, lambda *a, **k: None)

    results = []
    for name, fn in CHECKS:
        try:
            results.append((name, "pass", fn()))
        except AssertionError as e:
            results.append((name, "fail", str(e)))
        except Exception:
            results.append((name, "fail", traceback.format_exc(limit=3)))

    print()
    for name, status, detail in results:
        print(f"[{status.upper()}] {name}: {detail}")
    failed = [r for r in results if r[1] == "fail"]
    print(f"\n{len(results) - len(failed)} passed / {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
