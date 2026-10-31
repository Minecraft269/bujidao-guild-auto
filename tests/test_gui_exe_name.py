# -*- coding: utf-8 -*-
"""GUI 可执行名单一真源的守卫（GUI设计规范 §9）。

曾经三处各写一个名字：磁盘产物是 Gui.exe、package.py 复制成 Gui.exe、
CI 却去找 bujidao-guild-gui.exe、运行时白名单里又没有 Gui.exe。
结果就是实机「双击 启动.cmd 不出界面，日志说未找到 Gui.exe」。

真源定为 src/guild_auto/ui/binding.py 的 GUI_EXE_NAME；这组测试把
Cargo bin name / tauri mainBinaryName / package.py / build.yml 焊在它上面。
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from guild_auto.ui.binding import GUI_EXE_NAME, GUI_EXE_NAMES

REPO = Path(__file__).resolve().parents[1]
TAURI_CONF = REPO / "gui" / "src-tauri" / "tauri.conf.json"
CARGO_TOML = REPO / "gui" / "src-tauri" / "Cargo.toml"
PACKAGE_PY = REPO / "scripts" / "package.py"
BUILD_YML = REPO / ".github" / "workflows" / "build.yml"


def test_canonical_name_is_first_in_whitelist():
    """白名单第一位必须是真源，历史名字只作兼容后备。"""
    assert GUI_EXE_NAMES[0] == GUI_EXE_NAME == "Gui.exe"


def test_tauri_main_binary_name_matches():
    """tauri bundle 按 mainBinaryName 找二进制；对不上就打不进 MSI。"""
    conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    assert conf["mainBinaryName"] + ".exe" == GUI_EXE_NAME


def test_cargo_bin_name_matches():
    """cargo 的 bin name 决定 target/release 下真实产出的文件名。"""
    text = CARGO_TOML.read_text(encoding="utf-8")
    m = re.search(r'^\[\[bin\]\]\n(?:.*\n)*?name = "([^"]+)"', text, re.M)
    assert m, "Cargo.toml 缺 [[bin]] name"
    assert m.group(1) + ".exe" == GUI_EXE_NAME


def test_package_py_has_no_hardcoded_name():
    """package.py 不得出现等于该文件名的字符串字面量（docstring 与提示语不算）。"""
    tree = ast.parse(PACKAGE_PY.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr):
                docstrings.add(id(body[0].value))
    offenders = [
        (n.lineno, n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and n.value == GUI_EXE_NAME
        and id(n) not in docstrings
    ]
    assert not offenders, f"package.py 硬编码了产物名: {offenders}"


def test_build_yml_has_no_hardcoded_name():
    """build.yml 里名字只能来自 binding；注释与 step 标题不算（它们不参与取文件）。"""
    code_lines = [
        ln
        for ln in BUILD_YML.read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#")
    ]
    for name in GUI_EXE_NAMES:
        patterns = (
            f"release/{name}",
            f"release\\{name}",
            f"-Filter '{name}'",
            f'-Filter "{name}"',
            f"'{name}'",
            f'"{name}"',
        )
        for pattern in patterns:
            hits = [ln.strip() for ln in code_lines if pattern in ln]
            assert not hits, f"build.yml 硬编码了 {pattern}: {hits}"
    joined = "\n".join(code_lines)
    assert "GUI_EXE_NAME" in joined, "build.yml 应从 binding 取名字"
