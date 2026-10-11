# -*- coding: utf-8 -*-
"""扫描全部测试文件，找出无断言的 test 方法。

判据：方法体内没有任何 self.assert* / self.fail / self.skipTest / subTest 调用。
用途：审查反馈有空测试，用工具全量核实，而不是只查被点名的那两个文件。
"""
import ast
import os
import sys

TESTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests")

ASSERT_ATTRS = set()
for _s in ("Equal", "NotEqual", "True", "False", "Is", "IsNot", "IsNone",
           "IsNotNone", "In", "NotIn", "IsInstance", "IsNotInstance",
           "Raises", "RaisesRegex", "Warns", "Logs", "AlmostEqual",
           "Greater", "GreaterEqual", "Less", "LessEqual", "Regex",
           "CountEqual", "SequenceEqual", "ListEqual", "DictEqual",
           "MultiLineEqual", "TupleEqual", "SetEqual"):
    ASSERT_ATTRS.add("assert" + _s)
ASSERT_ATTRS.update(("fail", "skipTest", "subTest"))
# unittest.mock.Mock 的断言方法同样是断言(assert_called_once_with 等)。
# 少了这些会把大量优质 mock 测试误报成"无断言"，扫描结果就没人信了。
ASSERT_ATTRS.update((
    "assert_called", "assert_called_once", "assert_called_with",
    "assert_called_once_with", "assert_any_call", "assert_has_calls",
    "assert_not_called",
))


def _has_assertion(fn):
    """方法体内是否有断言。

    三种都算：
      1. ast.Assert           —— pytest 风格裸 assert 语句
      2. self.assert*()       —— unittest 风格
      3. mock.assert_*()      —— 例： assert_called_once_with
    """
    for node in ast.walk(fn):
        if isinstance(node, ast.Assert):
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ASSERT_ATTRS:
                return True
    return False


def main():
    bad = []
    total = 0
    tests_dir = os.path.abspath(TESTS_DIR)
    for fname in sorted(os.listdir(tests_dir)):
        if not (fname.startswith("test_") and fname.endswith(".py")):
            continue
        path = os.path.join(tests_dir, fname)
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), path)
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            for fn in [n for n in cls.body
                       if isinstance(n, ast.FunctionDef) and n.name.startswith("test")]:
                total += 1
                if not _has_assertion(fn):
                    bad.append(f"{fname}:{fn.lineno}  {cls.name}::{fn.name}")
    print(f"共扫描 {total} 个 test 方法")
    if bad:
        print(f"发现 {len(bad)} 个无断言测试:")
        for b in bad:
            print("  " + b)
        return 1
    print("零无断言测试")
    return 0


if __name__ == "__main__":
    sys.exit(main())
