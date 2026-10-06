# -*- coding: utf-8 -*-
"""
test_game_state_mock.py —— game_state.py(截图 + Windows OCR)硬件交互层 mock 测试
==============================================================================
铁律(全部在本文件内强制):
  * pyautogui.screenshot / press / hotkey 一律替换成记录调用的假函数(模块级属性替换,
    真实硬件永远不会被触达);time 替换成假时钟(sleep 只记账不真等,顺带断言时长取值)。
  * winsdk 的 BitmapDecoder / Language / OcrEngine / DataWriter / InMemoryRandomAccessStream
    全部替换成同步假实现;OcrEngine.try_create_from_language 返回一个假的 recognizer。
  * 模块级 winsdk import 的两条分支(成功 / 失败降级)用「独立模块名重新执行 game_state.py」
    覆盖,不污染其它测试持有的 game_state 引用(sys.modules 里的 winsdk 项原样存取还原)。

覆盖:__init__ 默认/自定义/空值回退、_get_engine 中文→用户语言→全 None 三级回退、
      _ocr_text(BytesIO+asyncio.run)、_ocr_async(写流/解码/识别/引擎为 None)、
      detect(不可用降级/关键词归一化半角+全角+换行/缓存命中与过期/force/异常降级)、
      prepare_chat_state(不可用/非菜单/抖动不按 Esc/确认按 Esc/仍在则按 Enter/异常降级)。
"""
import importlib.abc
import importlib.util
import io
import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import game_state  # noqa: E402
from game_state import (  # noqa: E402
    CHAT_BAR_KEYWORDS,
    MENU_KEYWORDS,
    GameStateDetector,
    prepare_chat_state,
)

GAME_STATE_PATH = game_state.__file__

# 逐字带空格的 OCR 中文输出:winsdk 对中文常输出「回 到 游 戏」
SPACED_MENU = "回 到 游 戏"
# 全角空格(U+3000)OCR 也可能输出
FULLWIDTH_MENU = "回　到　游　戏"
# 换行分帧
NEWLINE_MENU = "回\n到\n游\n戏"


# ---------------------------------------------------------------------------
# 假 winsdk:winsdk import 成功分支需要这些符号真实存在
# ---------------------------------------------------------------------------
def _make_winsdk_stub():
    names = ["winsdk", "winsdk.windows", "winsdk.windows.graphics",
             "winsdk.windows.graphics.imaging", "winsdk.windows.globalization",
             "winsdk.windows.media", "winsdk.windows.media.ocr",
             "winsdk.windows.storage", "winsdk.windows.storage.streams"]
    mods = {n: types.ModuleType(n) for n in names}
    mods["winsdk.windows.graphics.imaging"].BitmapDecoder = object()
    mods["winsdk.windows.globalization"].Language = object()
    mods["winsdk.windows.media.ocr"].OcrEngine = object()
    mods["winsdk.windows.storage.streams"].DataWriter = object()
    mods["winsdk.windows.storage.streams"].InMemoryRandomAccessStream = object()
    for name, mod in mods.items():
        if "." in name:
            parent, _, child = name.rpartition(".")
            setattr(mods[parent], child, mod)
    return mods


class _BlockWinsdk(importlib.abc.MetaPathFinder):
    """让 `import winsdk...` 必然抛 ImportError —— 覆盖模块级 import 的失败降级分支。"""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "winsdk" or fullname.startswith("winsdk."):
            raise ImportError("test: winsdk 不可用")
        return None


def load_game_state_probe(with_winsdk):
    """以独立模块名重新执行 game_state.py(不碰 sys.modules['game_state'] 引用)。"""
    saved = {k: v for k, v in sys.modules.items()
             if k == "winsdk" or k.startswith("winsdk.")}
    finder = None
    try:
        for k in saved:
            del sys.modules[k]
        if with_winsdk:
            sys.modules.update(_make_winsdk_stub())
        else:
            finder = _BlockWinsdk()
            sys.meta_path.insert(0, finder)
        name = "game_state_probe_win" if with_winsdk else "game_state_probe_nowin"
        spec = importlib.util.spec_from_file_location(name, GAME_STATE_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if finder is not None:
            sys.meta_path.remove(finder)
        for k in [k for k in sys.modules if k == "winsdk" or k.startswith("winsdk.")]:
            del sys.modules[k]
        sys.modules.update(saved)


# ---------------------------------------------------------------------------
# 假 OCR 栈 / 假截图 / 假时钟
# ---------------------------------------------------------------------------
class _FakeImage:
    """替代 pyautogui.screenshot():save() 只往 BytesIO 写假 PNG 字节,不碰真实屏幕。"""

    def __init__(self, payload=b"\x89PNG\r\n\x1a\n-fake"):
        self.payload = payload
        self.saves = []

    def save(self, buf, format=None):  # noqa: A002 — 必须与 PIL.Image.save(fp, format) 签名一致,否则 mock 不真实
        self.saves.append((buf, format))
        buf.write(self.payload)


class _FakeClock:
    """假时钟:sleep 只记账(顺带把虚拟时间推后),让去抖/等待时长可断言。"""

    def __init__(self, now=1000.0):
        self.now = now
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


class _FakeStream:
    def __init__(self):
        self.output_requests = []
        self.seeks = []

    def get_output_stream_at(self, pos):
        self.output_requests.append(pos)
        return f"<stream@{pos}>"

    def seek(self, pos):
        self.seeks.append(pos)


class _FakeWriter:
    instances = []

    def __init__(self, sink):
        self.sink = sink
        self.written = None
        self.stored = False
        _FakeWriter.instances.append(self)

    def write_bytes(self, data):
        self.written = data

    async def store_async(self):
        self.stored = True


class _FakeDecoder:
    calls = []
    bitmap = "BITMAP"

    @classmethod
    async def create_async(cls, stream):
        cls.calls.append(stream)
        return cls()

    async def get_software_bitmap_async(self):
        return self.bitmap


class _FakeEngine:
    """假 OCR 引擎:每次 recognize_async 取一帧(frame = 一行 OCR 结果列表),
    帧队列取空后重复最后一帧。lines 保留供断言参考。"""

    def __init__(self, frames):
        self.frames = [list(f) for f in frames]
        self.seen_bitmaps = []
        self.remaining = [list(f) for f in frames]

    async def recognize_async(self, bitmap):
        self.seen_bitmaps.append(bitmap)
        if len(self.remaining) > 1:
            lines = self.remaining.pop(0)
        else:
            lines = self.remaining[0]
        return types.SimpleNamespace(
            lines=[types.SimpleNamespace(text=t) for t in lines])


class _FakeOcrEngine:
    language_result = None
    profile_result = None
    calls = []

    @classmethod
    def try_create_from_language(cls, lang):
        cls.calls.append(("language", lang))
        return cls.language_result

    @classmethod
    def try_create_from_user_profile_languages(cls):
        cls.calls.append(("profile", None))
        return cls.profile_result


class _FakeLanguage:
    def __init__(self, tag):
        self.tag = tag

    def __eq__(self, other):
        return isinstance(other, _FakeLanguage) and other.tag == self.tag

    def __repr__(self):
        return f"_FakeLanguage({self.tag!r})"


class GameStateTestBase(unittest.TestCase):
    """把被测模块的 pyautogui / time / winsdk 全部换成假实现。"""

    def setUp(self):
        self.mod = game_state
        self.shots = []          # pyautogui.screenshot 调用记录
        self.presses = []        # pyautogui.press / hotkey 调用记录
        self.clock = _FakeClock()
        self.image = _FakeImage()

        def _screenshot(*args, **kwargs):
            self.shots.append((args, kwargs))
            return self.image

        def _press(*args, **kwargs):
            self.presses.append(("press", args, kwargs))

        def _hotkey(*args, **kwargs):
            self.presses.append(("hotkey", args, kwargs))

        self._patch(self.mod, "pyautogui", types.SimpleNamespace(
            screenshot=_screenshot, press=_press, hotkey=_hotkey))
        self._patch(self.mod, "time", self.clock)
        self._patch(self.mod, "OcrEngine", _FakeOcrEngine)
        self._patch(self.mod, "Language", _FakeLanguage)
        self._patch(self.mod, "BitmapDecoder", _FakeDecoder)
        self._patch(self.mod, "DataWriter", _FakeWriter)
        self._patch(self.mod, "InMemoryRandomAccessStream", _FakeStream)

        _FakeOcrEngine.calls = []
        _FakeOcrEngine.language_result = None
        _FakeOcrEngine.profile_result = None
        _FakeDecoder.calls = []
        _FakeDecoder.bitmap = "BITMAP"
        _FakeWriter.instances = []

    def _patch(self, target, attr, value):
        patcher = patch.object(target, attr, value, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_detector(self, ocr_lines=(), **kwargs):
        """构造 available=True 的检测器,OCR 走真实 _ocr_text 流程(截图→PNG→假引擎)。
        ocr_lines 每个元素是一「帧」OCR 文本;detect 通过截图次数(frames)计数。"""
        kwargs.setdefault("cache_seconds", 3.0)
        det = GameStateDetector(**kwargs)
        det.available = True
        engine = _FakeEngine([[t] for t in ocr_lines])
        _FakeOcrEngine.language_result = engine
        det.engine = engine
        return det

    @property
    def frames(self):
        """实际发生的截图次数 = 真正跑了 _ocr_text 的次数(缓存命中时不变)。"""
        return len(self.shots)


# ---------------------------------------------------------------------------
# 1. 模块级 winsdk import 的成功 / 失败两条分支
# ---------------------------------------------------------------------------
class TestWinsdkImportBranch(unittest.TestCase):
    def test_import_failure_falls_back_to_unavailable(self):
        mod = load_game_state_probe(with_winsdk=False)
        self.assertIs(mod._WINSDK_OK, False)
        # 降级后构造出的检测器必须自报不可用,detect 才走 (False, False) 分支
        self.assertFalse(mod.GameStateDetector().available)

    def test_import_success_binds_all_winsdk_symbols(self):
        mod = load_game_state_probe(with_winsdk=True)
        self.assertIs(mod._WINSDK_OK, True)
        for name in ("BitmapDecoder", "Language", "OcrEngine",
                     "DataWriter", "InMemoryRandomAccessStream"):
            self.assertTrue(hasattr(mod, name), f"winsdk 符号 {name} 未绑定")
        self.assertTrue(mod.GameStateDetector().available)


# ---------------------------------------------------------------------------
# 2. __init__:默认 / 自定义 / 空值回退 / available 跟随模块标志
# ---------------------------------------------------------------------------
class TestInit(GameStateTestBase):
    def test_init_defaults(self):
        det = GameStateDetector()
        self.assertIs(det.menu_keywords, MENU_KEYWORDS)
        self.assertIs(det.chat_keywords, CHAT_BAR_KEYWORDS)
        self.assertEqual(det.cache_seconds, 3.0)
        self.assertEqual(det.chat_bar_state, "auto")
        self.assertIsNone(det._cache)
        self.assertIsNone(det._engine)
        self.assertEqual(det.available, game_state._WINSDK_OK)

    def test_init_custom_overrides(self):
        det = GameStateDetector(menu_keywords=["设置"], chat_keywords=["发言"],
                                cache_seconds=9.5, chat_bar_state="open")
        self.assertEqual(det.menu_keywords, ["设置"])
        self.assertEqual(det.chat_keywords, ["发言"])
        self.assertEqual(det.cache_seconds, 9.5)
        self.assertEqual(det.chat_bar_state, "open")
        # 构造期不得触发任何 OCR 引擎创建
        self.assertIsNone(det._engine)
        self.assertEqual(_FakeOcrEngine.calls, [])

    def test_init_empty_values_fall_back_to_defaults(self):
        det = GameStateDetector(menu_keywords=[], chat_keywords=(),
                                cache_seconds=0, chat_bar_state="closed")
        self.assertIs(det.menu_keywords, MENU_KEYWORDS)   # 空列表/空元组 → 默认
        self.assertIs(det.chat_keywords, CHAT_BAR_KEYWORDS)
        self.assertEqual(det.cache_seconds, 0)            # 0 是有效值,不被回退
        self.assertEqual(det.chat_bar_state, "closed")

    def test_available_follows_module_flag(self):
        self._patch(self.mod, "_WINSDK_OK", False)
        self.assertFalse(GameStateDetector().available)


# ---------------------------------------------------------------------------
# 3. _get_engine:zh-CN → 用户语言 → 全 None 三级回退
# ---------------------------------------------------------------------------
class TestGetEngine(GameStateTestBase):
    def test_zh_hans_engine_created_once(self):
        engine = _FakeEngine(["回到游戏"])
        _FakeOcrEngine.language_result = engine
        det = GameStateDetector()
        self.assertIs(det._get_engine(), engine)
        self.assertIs(det._get_engine(), engine)
        # 只创建一次,且用的是 zh-CN;不回落用户语言
        self.assertEqual([c[0] for c in _FakeOcrEngine.calls], ["language"])
        self.assertEqual(_FakeOcrEngine.calls[0][1], _FakeLanguage("zh-CN"))

    def test_fallback_to_user_profile_languages(self):
        profile = _FakeEngine(["hello"])
        _FakeOcrEngine.language_result = None
        _FakeOcrEngine.profile_result = profile
        det = GameStateDetector()
        self.assertIs(det._get_engine(), profile)
        self.assertEqual([c[0] for c in _FakeOcrEngine.calls], ["language", "profile"])
        self.assertIs(det._engine, profile)

    def test_both_engines_none(self):
        det = GameStateDetector()
        self.assertIsNone(det._get_engine())
        self.assertEqual([c[0] for c in _FakeOcrEngine.calls], ["language", "profile"])


# ---------------------------------------------------------------------------
# 4. _ocr_text / _ocr_async:截图 → PNG 字节 → 识别 → 文本拼接
# ---------------------------------------------------------------------------
class TestOcrPipeline(GameStateTestBase):
    def test_ocr_text_full_pipeline(self):
        engine = _FakeEngine([["回 到 游 戏", "输入消息"]])
        _FakeOcrEngine.language_result = engine
        det = GameStateDetector()

        text = det._ocr_text()

        # 截图被调用一次,PNG 走了 BytesIO
        self.assertEqual(len(self.shots), 1)
        self.assertEqual(len(self.image.saves), 1)
        buf, fmt = self.image.saves[0]
        self.assertIsInstance(buf, io.BytesIO)
        self.assertEqual(fmt, "PNG")
        # PNG 字节原样写进 DataWriter,并被 store_async 提交
        self.assertEqual(len(_FakeWriter.instances), 1)
        writer = _FakeWriter.instances[0]
        self.assertEqual(writer.written, self.image.payload)
        self.assertTrue(writer.stored)
        # 流回到 0 交给解码器,解码出的 bitmap 交给引擎
        self.assertEqual(_FakeDecoder.calls[0].seeks, [0])
        self.assertEqual(_FakeDecoder.calls[0].output_requests, [0])
        self.assertEqual(engine.seen_bitmaps, ["BITMAP"])
        # 多行结果以 \n 拼接
        self.assertEqual(text, "回 到 游 戏\n输入消息")

    def test_ocr_text_empty_when_no_engine(self):
        det = GameStateDetector()
        self.assertEqual(det._ocr_text(), "")   # 两个引擎都 None
        self.assertEqual(len(self.shots), 1)    # 截图照常发生
        self.assertEqual([c[0] for c in _FakeOcrEngine.calls], ["language", "profile"])

    def test_ocr_async_joins_lines_with_newline(self):
        engine = _FakeEngine([["A", "", "B"]])
        det = GameStateDetector()
        det._engine = engine
        text = self._asyncio_run(det._ocr_async(b"png-bytes"))
        self.assertEqual(text, "A\n\nB")        # 空行也保留
        self.assertEqual(engine.seen_bitmaps, ["BITMAP"])
        self.assertEqual(_FakeDecoder.calls[0].seeks, [0])

    def test_ocr_async_no_engine_returns_empty_without_recognize(self):
        det = GameStateDetector()
        det._engine = None
        _FakeOcrEngine.language_result = None
        _FakeOcrEngine.profile_result = None
        self.assertEqual(self._asyncio_run(det._ocr_async(b"png")), "")

    @staticmethod
    def _asyncio_run(coro):
        import asyncio
        return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 5. detect:降级 / 归一化 / 缓存 / force / 异常
# ---------------------------------------------------------------------------
class TestDetect(GameStateTestBase):
    def test_unavailable_returns_false_false_without_screenshot(self):
        det = self.make_detector(ocr_lines=["回到游戏"])
        det.available = False
        self.assertEqual(det.detect(), (False, False))
        self.assertEqual(self.shots, [])   # 不可用时连截图都不做

    def test_normalizes_halfwidth_spaces(self):
        det = self.make_detector(ocr_lines=[SPACED_MENU])
        self.assertEqual(det.detect(), (True, False))

    def test_normalizes_fullwidth_spaces(self):
        det = self.make_detector(ocr_lines=[FULLWIDTH_MENU])
        self.assertEqual(det.detect(), (True, False))

    def test_normalizes_newlines(self):
        det = self.make_detector(ocr_lines=[NEWLINE_MENU])
        self.assertEqual(det.detect(), (True, False))

    def test_normalizes_chat_keyword(self):
        det = self.make_detector(ocr_lines=["输 入　消 息"])
        self.assertEqual(det.detect(), (False, True))

    def test_plain_text_no_match(self):
        det = self.make_detector(ocr_lines=["公会仓库"])
        self.assertEqual(det.detect(), (False, False))

    def test_empty_and_none_keywords_never_match(self):
        det = self.make_detector(ocr_lines=["回到游戏"],
                                 menu_keywords=["", None], chat_keywords=[""])
        # 空关键词若不守卫会把任何文本判成命中,这里必须全部落空
        self.assertEqual(det.detect(), (False, False))

    def test_custom_keywords(self):
        det = self.make_detector(ocr_lines=["系 统 设 置"], menu_keywords=["系统设置"])
        self.assertEqual(det.detect(), (True, False))

    def test_cache_hit_reuses_frame(self):
        det = self.make_detector(ocr_lines=["回到游戏", "公会仓库"])
        self.assertEqual(det.detect(), (True, False))
        self.clock.advance(2.9)                       # 未超 cache_seconds=3.0
        # 帧 2 只在被重新截图时才消耗;命中缓存就说明它没被碰
        self.assertEqual(det.detect(), (True, False))
        self.assertEqual(self.frames, 1)
        self.assertEqual(len(det.engine.remaining), 1)  # 第二帧仍是未消费的

    def test_cache_expiry_rescreens(self):
        det = self.make_detector(ocr_lines=["回到游戏", "公会仓库"])
        self.assertEqual(det.detect(), (True, False))
        self.clock.advance(3.01)                      # 超过 cache_seconds
        self.assertEqual(det.detect(), (False, False))
        self.assertEqual(self.frames, 2)

    def test_custom_cache_seconds_window(self):
        det = self.make_detector(ocr_lines=["回到游戏", "公会仓库"],
                                 cache_seconds=0.5)
        det.detect()
        self.clock.advance(0.4)
        det.detect()
        self.assertEqual(self.frames, 1)
        self.clock.advance(0.2)
        det.detect()
        self.assertEqual(self.frames, 2)

    def test_force_bypasses_cache(self):
        det = self.make_detector(ocr_lines=["回到游戏", "公会仓库"])
        self.assertEqual(det.detect(), (True, False))
        self.assertEqual(det.detect(force=True), (False, False))
        self.assertEqual(self.frames, 2)
        # 强制刷新后的结果写回缓存,下一次普通检测不再截图
        self.assertEqual(det.detect(), (False, False))
        self.assertEqual(self.frames, 2)

    def test_screenshot_exception_degrades(self):
        det = GameStateDetector()
        det.available = True

        def _boom():
            raise RuntimeError("screen grab failed")

        det._ocr_text = _boom
        self.assertEqual(det.detect(), (False, False))   # 不崩,按非菜单处理

    def test_keyword_match_error_degrades(self):
        det = GameStateDetector(menu_keywords=[object()], chat_keywords=["输入消息"])
        det.available = True
        det._ocr_text = lambda: "输入消息"
        self.assertEqual(det.detect(), (False, False))   # object() 不可做 in 判断 → 降级


# ---------------------------------------------------------------------------
# 6. prepare_chat_state:菜单去抖 / Esc / Enter / 各级降级
# ---------------------------------------------------------------------------
class TestPrepareChatState(GameStateTestBase):
    def setUp(self):
        super().setUp()
        self.logs = []

    def console(self, msg):
        self.logs.append(msg)

    def test_none_detector(self):
        self.assertFalse(prepare_chat_state(None, self.console))
        self.assertEqual(self.logs, [])

    def test_unavailable_detector_skips_entirely(self):
        det = self.make_detector(ocr_lines=["回到游戏"])
        det.available = False
        self.assertFalse(prepare_chat_state(det, self.console))
        self.assertEqual(self.shots, [])     # 不可用 → 走原发送流程,不截图
        self.assertEqual(self.presses, [])

    def test_not_in_menu_does_not_press(self):
        det = self.make_detector(ocr_lines=["公会仓库"])
        self.assertFalse(prepare_chat_state(det, self.console))
        self.assertEqual(self.presses, [])
        self.assertEqual(self.clock.sleeps, [])   # 非菜单不做任何等待
        self.assertEqual(self.logs, [])

    def test_single_frame_jitter_does_not_press_esc(self):
        # 首次判定菜单、二次判定非菜单 → OCR 抖动,不按 Esc(否则反复按 Esc 死循环)
        det = self.make_detector(ocr_lines=["回到游戏", "公会仓库"])
        self.assertFalse(prepare_chat_state(det, self.console))
        self.assertEqual(self.presses, [])
        self.assertEqual(self.frames, 2)               # 检测 + 去抖复查,不再多截
        self.assertEqual(self.clock.sleeps, [0.2])      # 只等了去抖的 200ms
        self.assertEqual(self.logs, [])

    def test_confirmed_menu_presses_esc_only(self):
        det = self.make_detector(ocr_lines=["回到游戏", "回到游戏", "公会仓库"])
        self.assertTrue(prepare_chat_state(det, self.console))
        self.assertEqual([p[1] for p in self.presses], [("esc",)])
        self.assertEqual(self.clock.sleeps, [0.2, 0.8])
        self.assertTrue(any("按 Esc 关闭" in m for m in self.logs))
        self.assertEqual(self.frames, 3)   # 检测 / 去抖复查 / Esc 后复查,恰好 3 次截图

    def test_menu_persisting_presses_enter(self):
        det = self.make_detector(ocr_lines=["回到游戏"])
        self.assertTrue(prepare_chat_state(det, self.console))
        self.assertEqual([p[1] for p in self.presses], [("esc",), ("enter",)])
        self.assertEqual(self.clock.sleeps, [0.2, 0.8, 0.5])
        self.assertTrue(any("按 Enter" in m for m in self.logs))
        self.assertEqual(self.frames, 3)

    def test_detect_exception_degrades_to_original_flow(self):
        det = GameStateDetector()
        det.available = True

        def _boom(*args, **kwargs):
            raise RuntimeError("ocr boom")

        det.detect = _boom
        self.assertFalse(prepare_chat_state(det, self.console))
        self.assertEqual(self.presses, [])
        self.assertEqual(len(self.logs), 1)
        self.assertIn("界面状态检测异常", self.logs[0])
        self.assertIn("ocr boom", self.logs[0])

    def test_default_console_is_print(self):
        # console_func 是定义期默认值(就是内建 print),不传时按 print 输出
        import contextlib
        import inspect
        self.assertIs(inspect.signature(prepare_chat_state)
                      .parameters["console_func"].default, print)
        det = self.make_detector(ocr_lines=["回到游戏", "回到游戏", "公会仓库"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertTrue(prepare_chat_state(det))
        self.assertIn("按 Esc 关闭", buf.getvalue())


# ---------------------------------------------------------------------------
# 7. 源码不变式:关键词归一化必须同时处理半角空格 / 全角空格 / 换行
# ---------------------------------------------------------------------------
class TestNormalizeInvariant(unittest.TestCase):
    def test_source_keeps_all_three_whitespace_strips(self):
        # 上面若干行为测试能挡住"少 replace 一种空白"之外的情况;这里再钉住源码语句,
        # 防止有人把三种归一化合成一个 re.sub 而悄悄改变了语义。
        with open(GAME_STATE_PATH, encoding="utf-8") as f:
            src = f.read()
        line = [ln for ln in src.splitlines() if "compact = text.replace" in ln]
        self.assertEqual(len(line), 1, "找不到唯一的 compact 归一化语句")
        for token in ('replace(" ", "")', 'replace("　", "")', 'replace("\\n", "")'):
            self.assertIn(token, line[0], f"归一化缺少 {token}")


if __name__ == "__main__":
    unittest.main()