# -*- coding: utf-8 -*-
"""
game_state.py —— 游戏界面状态检测
==================================
职责:通过"窗口截图 + Windows 系统 OCR"检测游戏当前界面状态:
  * 是否在菜单界面(识别"回到游戏/断开链接/保存并退出"等暂停菜单文字)
  * 聊天栏是否已打开(识别输入框提示文字,关键词可配置)
供发送指令前调用:在菜单界面则先按 Esc 关闭;聊天栏已打开则直接发送(不重复按聊天键)。

依赖:winsdk(Windows OCR,Win10+ 系统自带引擎;中文识别需系统装有中文 OCR 语言包)。
winsdk 不可用时自动降级(检测结果视为"非菜单、聊天栏未打开",不影响原有发送流程)。
"""
import asyncio
import io
import time

import pyautogui

import logger

# 菜单界面关键词(网易版暂停菜单固定文案;OCR 输出可能逐字带空格,匹配前会去除所有空白)
MENU_KEYWORDS = ["回到游戏", "断开连接", "断开链接", "保存并退出", "暂停菜单"]
# 聊天栏输入框提示词(不同客户端/版本可能不同,可在配置中调整)
CHAT_BAR_KEYWORDS = ["输入消息", "输入聊天", "输入内容", "输入文字", "说点什么"]

try:
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.globalization import Language
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.storage.streams import DataWriter, InMemoryRandomAccessStream
    _WINSDK_OK = True
except Exception:  # winsdk 未安装/不可用 → 降级
    _WINSDK_OK = False


class GameStateDetector:
    """游戏界面状态检测器(截图 + OCR,带结果缓存)。"""

    def __init__(self, menu_keywords=None, chat_keywords=None, cache_seconds=3.0,
                 chat_bar_state="auto"):
        self.menu_keywords = menu_keywords or MENU_KEYWORDS
        self.chat_keywords = chat_keywords or CHAT_BAR_KEYWORDS
        self.cache_seconds = cache_seconds
        self.chat_bar_state = chat_bar_state  # "auto" / "open" / "closed"
        self._cache = None  # (time, text)
        self._engine = None
        self.available = _WINSDK_OK

    # ---- OCR ----
    def _get_engine(self):
        if self._engine is None:
            self._engine = OcrEngine.try_create_from_language(Language("zh-CN"))
            if self._engine is None:  # 无中文语言包:退回用户语言
                self._engine = OcrEngine.try_create_from_user_profile_languages()
        return self._engine

    def _ocr_text(self):
        """截取屏幕并识别文字(同步包装)。"""
        img = pyautogui.screenshot()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return asyncio.run(self._ocr_async(buf.getvalue()))

    async def _ocr_async(self, png_bytes):
        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream.get_output_stream_at(0))
        writer.write_bytes(png_bytes)
        await writer.store_async()
        stream.seek(0)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        engine = self._get_engine()
        if engine is None:
            return ""
        result = await engine.recognize_async(bitmap)
        return "\n".join(line.text for line in result.lines)

    # ---- 检测 ----
    def detect(self, force=False):
        """检测界面状态。返回 (in_menu, chat_open)。
        force=True 忽略缓存重新截图(用于 Esc 关闭菜单后的二次确认)。
        OCR 不可用时返回 (False, False)。"""
        if not self.available:
            return False, False
        now = time.time()
        try:
            if force or self._cache is None or now - self._cache[0] > self.cache_seconds:
                text = self._ocr_text()
                self._cache = (now, text)
            text = self._cache[1]
            # 关键:winsdk OCR 中文输出为"逐字带空格"(如"回 到 游 戏"),
            # 匹配前必须去除所有空白,否则子串匹配永远失败
            compact = text.replace(" ", "").replace("　", "").replace("\n", "")
            in_menu = any(k and k in compact for k in self.menu_keywords)
            chat_open = any(k and k in compact for k in self.chat_keywords)
            logger.trace_return("game_state.detect", {"in_menu": in_menu, "chat_open": chat_open,
                                                      "cached": not (force or self._cache is None or now - self._cache[0] > self.cache_seconds)})
            return in_menu, chat_open
        except Exception as e:
            logger.trace_exception("game_state.detect", e)
            return False, False


def prepare_chat_state(detector, console_func=print):
    """发送指令前的菜单处理(聊天栏状态由'试错式发送'自动处理):
      * OCR 检测到菜单界面 → 按 Esc 关闭(仍存在则按 Enter 回到游戏)
      返回 True 表示处理过菜单;detector 不可用时返回 False(走原流程)。"""
    if detector is None or not getattr(detector, "available", False):
        return False
    try:
        in_menu, _ = detector.detect()
        if in_menu:
            # 去抖:再检测一次(200ms 后),仍 in_menu 才按 Esc
            # 避免单次 OCR 抖动把主界面误判为菜单
            time.sleep(0.2)
            in_menu2, _ = detector.detect(force=True)
            if not in_menu2:
                # 首次是抖动,不按 Esc
                return False
            logger.log_debug("OCR 二次确认菜单界面,按 Esc 关闭 ...")
            console_func("检测到游戏处于菜单界面,按 Esc 关闭...")
            pyautogui.press("esc")
            time.sleep(0.8)
            in_menu3, _ = detector.detect(force=True)
            if in_menu3:
                console_func("菜单仍未关闭,按 Enter 尝试回到游戏...")
                pyautogui.press("enter")
                time.sleep(0.5)
            return True
        return False
    except Exception as e:
        console_func(f"(界面状态检测异常,按原流程发送: {e})")
        return False
