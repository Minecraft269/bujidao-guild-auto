#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
build-variants.py —— 构建矩阵生成器(plan.txt【阶段 7】第 4 条)
================================================================
CI 里禁止手写 8 种能力组合:那份清单必须由能力注册表推导出来,否则新增
能力就要手改 workflow,漏改就少构建一个变体。本脚本是 workflow 与
`guild_auto.capabilities` 之间的唯一桥:

    python scripts/build-variants.py --list
        → GitHub Actions 动态 matrix 的 JSON({"include": [...]})
    python scripts/build-variants.py --write-const <path> --capabilities core,async
        → 把能力清单写成打包期常量 guild_auto/_build_capabilities.py,
          使产物脱离环境变量也能报出自己的变体名

用法(本地):
    PYTHONPATH=src python scripts/build-variants.py --list
"""

import argparse
import json
import os
import sys

# 允许直接 `python scripts/build-variants.py`(未 pip install -e . 时)。
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from guild_auto import capabilities  # noqa: E402

_CONST_TEMPLATE = """# -*- coding: utf-8 -*-
# 由 scripts/build-variants.py 生成,勿手工编辑。
# 打包期能力清单:产物运行时读不到构建环境变量,靠这份常量报告自身变体。
CAPABILITIES = {raw!r}
"""


def _matrix() -> str:
    """输出单行 JSON —— GITHUB_OUTPUT 每个 key 只接受单行 value。"""
    include = [
        {"variant": name, "capabilities": ",".join(caps)}
        for name, caps in capabilities.iter_variants()
    ]
    return json.dumps({"include": include}, ensure_ascii=False, separators=(",", ":"))


def _write_const(path: str, raw: str) -> None:
    # 先按注册表校验:非法标识在 CI 构建期就炸,而不是产出功能残缺的 exe。
    caps = capabilities.parse_capabilities(raw)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_CONST_TEMPLATE.format(raw=",".join(caps)))
    print(f"已写入构建期能力常量: {path} <- {','.join(caps)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="生成多构建变体的能力矩阵 / 打包常量")
    ap.add_argument("--list", action="store_true", help="输出 GitHub Actions matrix JSON")
    ap.add_argument(
        "--aliases", action="store_true", help="输出别名 → 权威拼接名 JSON(只读,不含产物命名)"
    )
    ap.add_argument("--write-const", metavar="PATH", help="写入构建期能力常量模块的路径")
    ap.add_argument("--capabilities", default=None, help="能力清单,如 core,async")
    args = ap.parse_args()

    if not args.list and not args.aliases and not args.write_const:
        ap.error("至少指定 --list / --aliases / --write-const")

    if args.list:
        print(_matrix())
    if args.aliases:
        # 只读查询:别名不是权威标识,构建产物名仍取拼接名(--list 里的 variant)。
        print(
            json.dumps(
                {"aliases": capabilities.aliases(), "authoritative_field": "variant"},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    if args.write_const:
        raw = args.capabilities
        if raw is None:
            raw = os.environ.get(capabilities.ENV_VAR)
        if raw is None:
            ap.error("--write-const 需要 --capabilities 或环境变量 GUILD_AUTO_CAPABILITIES")
        _write_const(args.write_const, raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
