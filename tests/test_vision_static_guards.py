# -*- coding: utf-8 -*-
"""
test_vision_static_guards.py —— vision 层的两条静态纪律
=======================================================
验收(docs/UI识别模式详解.md §十一 / 十二):
  1. 禁止图标模板匹配 / 像素比对:材质包会改变全部图标外观。
     —— vision/ 下不得出现 cv2.matchTemplate、template 等关键字;
  2. 依赖单向:core/domain 层不得 import vision。
"""

import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src", "guild_auto")
VISION_DIR = os.path.join(SRC, "vision")

# 模板匹配 / 像素比对类关键字(大小写不敏感)。中文"模板"不在其中:
# 注释里写"不做模板匹配"是说明,不是实现。
FORBIDDEN_TOKENS = (
    "cv2",
    "matchtemplate",
    "template",
    "screenshot_template",
    "imagehash",
    "pyautogui.locate",
    "locateonscreen",
)


def _python_files(directory):
    for name in sorted(os.listdir(directory)):
        if name.endswith(".py"):
            yield os.path.join(directory, name)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestNoTemplateMatching(unittest.TestCase):
    def test_vision_layer_has_no_template_matching_keywords(self):
        hits = []
        for path in _python_files(VISION_DIR):
            text = _read(path).lower()
            for token in FORBIDDEN_TOKENS:
                if token in text:
                    hits.append(f"{os.path.basename(path)}: {token}")
        self.assertEqual(hits, [], f"vision 层出现模板匹配/像素比对关键字: {hits}")

    def test_vision_layer_contains_only_python_sources(self):
        """不得夹带图标模板图片/截图素材(那会重新引入模板匹配路径)。"""
        extras = [
            name
            for name in os.listdir(VISION_DIR)
            if os.path.isfile(os.path.join(VISION_DIR, name)) and not name.endswith(".py")
        ]
        self.assertEqual(extras, [], f"vision 目录出现非源码文件: {extras}")


class TestDependencyDirection(unittest.TestCase):
    def test_core_layers_do_not_import_vision(self):
        offenders = []
        for layer in ("domain", "application", "concurrency", "config"):
            layer_dir = os.path.join(SRC, layer)
            if not os.path.isdir(layer_dir):
                continue
            for path in _python_files(layer_dir):
                if "guild_auto.vision" in _read(path):
                    offenders.append(os.path.relpath(path, REPO))
        self.assertEqual(offenders, [], f"core 层出现对 vision 的依赖: {offenders}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
