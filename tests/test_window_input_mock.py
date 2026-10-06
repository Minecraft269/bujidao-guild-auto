# -*- coding: utf-8 -*-
"""
test_window_input_mock.py —— window_input.py 硬件交互层 mock 覆盖
======================================================================
对应硬性验收标准:
  硬件交互层 mock 覆盖 + 单独报告 —— 窗口查找/置前(ctypes)、键名解析、
  模拟人工输入(pyautogui/pyperclip/clipboard)。真实设备调用不计入分母。

【mock 策略声明】
  user32 / kernel32   → FakeUser32 / FakeKernel32 假对象:自己驱动回调,
                        可断言窗口枚举、exclude_pid 排除、置前序列
  os.getpid()         → patch 成固定 pid,断言"排除脚本自身进程"被真的传下去
  pyautogui.press /
  pyautogui.hotkey    → MagicMock:只记录调用,绝不真实按键
  pyperclip.copy      → MagicMock:只记录调用,绝不碰真实剪贴板
  time.sleep          → MagicMock:无真实等待;需要校验时长的用例直接断言调用参数
  random.uniform      → 固定返回,让延时可确定断言
  logger.*            → patch,测试不写真实日志文件
  options.txt         → tempfile.mkdtemp() 下的临时文件(utf-8 / gbk / 不可解码三态)
  真实设备调用:0 次(press/hotkey/screenshot/clipboard 全部被 mock)
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import window_input  # noqa: E402
from window_input import (  # noqa: E402
    GAME_PROCESS_NAMES,
    KEYEVENTF_KEYUP,
    MC_KEY_TO_PYAUTOGUI,
    SW_RESTORE,
    VK_MENU,
    HumanInput,
    activate_window,
    find_game_window,
    read_chat_key_from_options,
    resolve_chat_key,
)

MODULE = "window_input"


# ---------------------------------------------------------------------------
# 假 Win32 对象(自驱动,枚举/置前序列可断言)
# ---------------------------------------------------------------------------
class FakeUser32:
    """可控的 user32 替身。

    windows: [(hwnd, pid, visible, title), ...]
    EnumWindows 真的调用回调 —— 否则 _enum_windows 的回调体永远不执行。
    """

    def __init__(self, windows=(), foreground_ok=True):
        self.windows = list(windows)
        self.foreground_ok = foreground_ok
        self.calls = []

    def IsWindowVisible(self, hwnd):
        return int(bool(dict((w[0], w[2]) for w in self.windows).get(hwnd, False)))

    def GetWindowThreadProcessId(self, hwnd, byref_pid):
        for wh, pid, _vis, _title in self.windows:
            if wh == hwnd:
                byref_pid._obj.value = pid
                return 1
        byref_pid._obj.value = 0
        return 0

    def GetWindowTextLengthW(self, hwnd):
        for wh, _pid, _vis, title in self.windows:
            if wh == hwnd:
                return len(title)
        return 0

    def GetWindowTextW(self, hwnd, buf, _max):
        for wh, _pid, _vis, title in self.windows:
            if wh == hwnd:
                buf.value = title
                return len(title)
        return 0

    def EnumWindows(self, cb, _lparam):
        self.calls.append("EnumWindows")
        for wh, _pid, _vis, _title in self.windows:
            cb(wh, 0)
        return 1

    def ShowWindow(self, hwnd, cmd):
        self.calls.append(("ShowWindow", hwnd, cmd))
        return 1

    def keybd_event(self, vk, scan, flags, extra):
        self.calls.append(("keybd_event", vk, scan, flags, extra))
        return 1

    def SetForegroundWindow(self, hwnd):
        self.calls.append(("SetForegroundWindow", hwnd))
        return int(self.foreground_ok)


class FakeKernel32:
    """可控的 kernel32 替身;image_name 决定 QueryFullProcessImageNameW 写入什么。"""

    def __init__(self, open_ok=True, image_name=r"C:\mc\bin\javaw.exe"):
        self.open_ok = open_ok
        self.image_name = image_name
        self.closed = []

    def OpenProcess(self, access, inherit, pid):
        return 1234 if self.open_ok else 0

    def QueryFullProcessImageNameW(self, handle, flags, buf, byref_size):
        buf.value = self.image_name
        byref_size._obj.value = len(self.image_name)
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


class FakeDetector:
    """GameStateDetector 替身:只提供 available + detect()。"""

    def __init__(self, available=True, result=(False, False), raises=False):
        self.available = available
        self._result = result
        self._raises = raises
        self.detect_calls = 0

    def detect(self, force=False):
        self.detect_calls += 1
        if self._raises:
            raise RuntimeError("OCR 引擎不可用")
        return self._result


class WindowInputMockBase(unittest.TestCase):
    """公共 setUp:把 pyautogui / pyperclip / sleep / random 全部换成 mock。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="wi_mock_")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.press = MagicMock(name="pyautogui.press")
        self.hotkey = MagicMock(name="pyautogui.hotkey")
        pyautogui = MagicMock(name="pyautogui", press=self.press, hotkey=self.hotkey)
        pyperclip = MagicMock(name="pyperclip")
        sleep = MagicMock(name="time.sleep")
        logger_p = MagicMock(name="window_input.logger")
        self.uniform = MagicMock(name="random.uniform", return_value=0.5)
        for p in (patch(f"{MODULE}.pyautogui", pyautogui),
                  patch(f"{MODULE}.pyperclip", pyperclip),
                  patch(f"{MODULE}.time.sleep", sleep),
                  patch(f"{MODULE}.random.uniform", self.uniform),
                  patch(f"{MODULE}.logger", logger_p)):
            p.start()
            self.addCleanup(p.stop)
        self.pyautogui = pyautogui
        self.pyperclip = pyperclip
        self.sleep = sleep

    def make_input(self, chat_key="t", delays=None, stop_check=None):
        cfg = {"chat_key": chat_key, "delays": delays or {}}
        return HumanInput(cfg, stop_check=stop_check)

    def write_options(self, text, encoding="utf-8", name="options.txt"):
        path = os.path.join(self.tmpdir, name)
        with open(path, "wb") as f:
            f.write(text.encode(encoding))
        return path


# ---------------------------------------------------------------------------
# _get_process_name
# ---------------------------------------------------------------------------
class TestGetProcessName(WindowInputMockBase):
    """进程名获取:成功 / OpenProcess 失败 / 异常 三条路径。"""

    def _run(self, k32):
        with patch(f"{MODULE}.user32", FakeUser32()), patch(f"{MODULE}.kernel32", k32):
            return window_input._get_process_name(777)

    def test_success_returns_lowercased_basename(self):
        """成功路径:只取 basename 并小写 —— 白名单比较按小写进行。"""
        k32 = FakeKernel32(open_ok=True, image_name=r"C:\Program Files\Java\bin\JavaW.EXE")
        self.assertEqual(self._run(k32), "javaw.exe")

    def test_open_process_denied_returns_empty(self):
        """OpenProcess 失败(权限不足/进程已退出)→ 空串,不得抛。"""
        self.assertEqual(self._run(FakeKernel32(open_ok=False)), "")

    def test_handle_closed_after_query(self):
        """成功路径必须 CloseHandle,否则句柄泄漏。"""
        k32 = FakeKernel32()
        self._run(k32)
        self.assertEqual(k32.closed, [1234])

    def test_exception_returns_empty(self):
        """user32 抛异常(如窗口已销毁)→ 空串兜底,不中断窗口查找。"""
        boom = MagicMock(name="user32")
        boom.GetWindowThreadProcessId.side_effect = OSError("窗口已销毁")
        with patch(f"{MODULE}.user32", boom):
            self.assertEqual(window_input._get_process_name(777), "")


# ---------------------------------------------------------------------------
# _enum_windows
# ---------------------------------------------------------------------------
class TestEnumWindows(WindowInputMockBase):
    """窗口枚举:可见过滤 / 空标题过滤 / exclude_pid 排除自身进程。"""

    def test_returns_visible_titled_windows(self):
        u32 = FakeUser32([(10, 1, True, "Minecraft 1.20"), (11, 2, True, "记事本")])
        with patch(f"{MODULE}.user32", u32):
            self.assertEqual(window_input._enum_windows(),
                             [(10, "Minecraft 1.20"), (11, "记事本")])

    def test_invisible_window_skipped(self):
        u32 = FakeUser32([(10, 1, False, "隐藏的游戏窗口")])
        with patch(f"{MODULE}.user32", u32):
            self.assertEqual(window_input._enum_windows(), [])

    def test_empty_title_skipped(self):
        """无标题窗口(空标题多为工具窗口/托盘)不参与关键词匹配。"""
        u32 = FakeUser32([(10, 1, True, ""), (11, 2, True, "Minecraft")])
        with patch(f"{MODULE}.user32", u32):
            self.assertEqual(window_input._enum_windows(), [(11, "Minecraft")])

    def test_exclude_pid_drops_own_process_window(self):
        """打包 exe 后自身窗口标题可能含关键词,必须按 pid 排除。"""
        u32 = FakeUser32([(10, 1, True, "bujidao.exe 控制台"),
                          (11, 2, True, "Minecraft 1.20")])
        with patch(f"{MODULE}.user32", u32):
            self.assertEqual(window_input._enum_windows(exclude_pid=1),
                             [(11, "Minecraft 1.20")])


# ---------------------------------------------------------------------------
# find_game_window
# ---------------------------------------------------------------------------
class TestFindGameWindow(WindowInputMockBase):
    """关键词匹配 / 游戏进程白名单优先 / 回退全窗口 / 无匹配。"""

    def test_empty_keywords_returns_none(self):
        """空关键词列表:不枚举窗口(无谓的 Win32 调用),直接 None。"""
        with patch(f"{MODULE}._enum_windows") as en:
            self.assertIsNone(find_game_window([]))
            en.assert_not_called()

    def test_excludes_own_pid(self):
        """always 排除脚本自身进程 —— 与 docstring 的打包 exe 场景一致。"""
        with patch(f"{MODULE}._enum_windows", return_value=[]) as en, \
                patch(f"{MODULE}.os.getpid", return_value=4242):
            find_game_window(["minecraft"])
        en.assert_called_once_with(exclude_pid=4242)

    def test_keyword_match_case_insensitive(self):
        with patch(f"{MODULE}._enum_windows",
                   return_value=[(10, "Minecraft 1.20 - bujidao")]), \
                patch(f"{MODULE}._get_process_name", return_value="notepad.exe"):
            self.assertEqual(find_game_window(["BuJiDao"]), 10)

    def test_game_process_whitelist_wins_over_title_match(self):
        """重点场景1:标题含关键词的记事本 vs java.exe 的游戏窗口 → 必须选游戏窗口。

        先命中的关键词顺序在这里无关:游戏窗口白名单过滤在关键词匹配之前。
        """
        windows = [(10, "bujidao 记事本草稿"), (11, "Minecraft 1.20")]
        names = {10: "notepad.exe", 11: "javaw.exe"}
        with patch(f"{MODULE}._enum_windows", return_value=windows), \
                patch(f"{MODULE}._get_process_name",
                      side_effect=lambda h: names[h]):
            self.assertEqual(find_game_window(["bujidao", "minecraft"]), 11)

    def test_whitelist_present_skips_non_game_titles(self):
        """白名单存在时,非游戏进程窗口即使标题命中也不进候选池 ——
        先命中的关键词(此处 notepad)因此被跳过,最终选 java.exe 的游戏窗口。"""
        windows = [(10, "minecraft 使用说明"), (11, "Minecraft 1.20")]
        names = {10: "explorer.exe", 11: "java.exe"}
        with patch(f"{MODULE}._enum_windows", return_value=windows), \
                patch(f"{MODULE}._get_process_name", side_effect=lambda h: names[h]):
            self.assertEqual(find_game_window(["minecraft"]), 11)

    def test_falls_back_to_all_windows_when_no_game_process(self):
        """无白名单进程时回退全部窗口(兼容非 Java 客户端)—— 哪怕别的程序标题
        更早命中关键词也照用。这与 docstring 的回退措辞一致:白名单只是**优先**,
        不是硬排除;真正的自身进程排除由 exclude_pid 负责。"""
        windows = [(10, "Minecraft 移植版")]
        with patch(f"{MODULE}._enum_windows", return_value=windows), \
                patch(f"{MODULE}._get_process_name", return_value="unknowngame.exe"):
            self.assertEqual(find_game_window(["minecraft"]), 10)

    def test_no_match_returns_none(self):
        with patch(f"{MODULE}._enum_windows",
                   return_value=[(10, "记事本")]), \
                patch(f"{MODULE}._get_process_name", return_value="notepad.exe"):
            self.assertIsNone(find_game_window(["minecraft"]))

    def test_no_windows_at_all_returns_none(self):
        with patch(f"{MODULE}._enum_windows", return_value=[]):
            self.assertIsNone(find_game_window(["minecraft"]))


# ---------------------------------------------------------------------------
# activate_window
# ---------------------------------------------------------------------------
class TestActivateWindow(WindowInputMockBase):
    """置前:ShowWindow 还原 + Alt 解锁序列 + 返回值。"""

    def test_restore_alt_unlock_then_foreground(self):
        u32 = FakeUser32(foreground_ok=True)
        with patch(f"{MODULE}.user32", u32):
            self.assertTrue(activate_window(4321))
        self.assertEqual(
            u32.calls,
            [("ShowWindow", 4321, SW_RESTORE),
             ("keybd_event", VK_MENU, 0, 0, 0),
             ("keybd_event", VK_MENU, 0, KEYEVENTF_KEYUP, 0),
             ("SetForegroundWindow", 4321)])

    def test_alt_key_released(self):
        """Alt 解锁必须成对(按下+抬起),否则 Alt 卡住影响后续打字。"""
        u32 = FakeUser32()
        with patch(f"{MODULE}.user32", u32):
            activate_window(1)
        downs = [c for c in u32.calls if c[0] == "keybd_event" and c[3] == 0]
        ups = [c for c in u32.calls if c[0] == "keybd_event" and c[3] == KEYEVENTF_KEYUP]
        self.assertEqual(len(downs), 1)
        self.assertEqual(len(ups), 1)

    def test_returns_false_when_foreground_refused(self):
        """SetForegroundWindow 被系统拒绝 → 返回 False,由调用方决定是否重试。"""
        with patch(f"{MODULE}.user32", FakeUser32(foreground_ok=False)):
            self.assertFalse(activate_window(4321))

    def test_sleeps_after_foreground(self):
        """置前后必须留出焦点切换时间(固定 0.4s)。"""
        with patch(f"{MODULE}.user32", FakeUser32()):
            activate_window(4321)
        self.sleep.assert_called_once_with(0.4)


# ---------------------------------------------------------------------------
# 键名映射
# ---------------------------------------------------------------------------
class TestKeyMap(unittest.TestCase):

    def test_letters_mapped_to_themselves(self):
        for c in "abcdefghijklmnopqrstuvwxyz":
            self.assertEqual(MC_KEY_TO_PYAUTOGUI[f"key.keyboard.{c}"], c, c)

    def test_digits_mapped_to_themselves(self):
        for i in range(10):
            self.assertEqual(MC_KEY_TO_PYAUTOGUI[f"key.keyboard.{i}"], str(i))

    def test_function_keys_f1_to_f12(self):
        self.assertEqual(MC_KEY_TO_PYAUTOGUI["key.keyboard.f1"], "f1")
        self.assertEqual(MC_KEY_TO_PYAUTOGUI["key.keyboard.f12"], "f12")

    def test_enter_and_escape(self):
        """重点场景4:options.txt 的 key.keyboard.enter → pyautogui 的 enter。"""
        self.assertEqual(MC_KEY_TO_PYAUTOGUI["key.keyboard.enter"], "enter")
        self.assertEqual(MC_KEY_TO_PYAUTOGUI["key.keyboard.escape"], "esc")

    def test_punctuation_and_modifiers(self):
        self.assertEqual(MC_KEY_TO_PYAUTOGUI["key.keyboard.slash"], "/")
        self.assertEqual(MC_KEY_TO_PYAUTOGUI["key.keyboard.backslash"], "\\")
        self.assertEqual(MC_KEY_TO_PYAUTOGUI["key.keyboard.left.control"], "ctrl")
        self.assertEqual(MC_KEY_TO_PYAUTOGUI["key.keyboard.right.control"], "ctrlright")

    def test_game_process_whitelist_contents(self):
        self.assertEqual(GAME_PROCESS_NAMES,
                         {"java.exe", "javaw.exe", "minecraft.exe",
                          "minecraftlauncher.exe"})


# ---------------------------------------------------------------------------
# read_chat_key_from_options
# ---------------------------------------------------------------------------
class TestReadChatKeyFromOptions(WindowInputMockBase):

    def test_utf8_file(self):
        p = self.write_options("version:3465\nkey_key.chat:key.keyboard.t\n")
        self.assertEqual(read_chat_key_from_options(p), "t")

    def test_gbk_file_falls_through_utf8(self):
        """GBK 字节在 utf-8 下必然解码失败 → 落到 gbk 分支。"""
        p = self.write_options("key_key.chat:key.keyboard.enter\n# 布吉岛",
                               encoding="gbk")
        self.assertEqual(read_chat_key_from_options(p), "enter")

    def test_undecodable_bytes_use_replace_fallback(self):
        """三种编码都解不开 → errors='replace' 兜底,不得抛 UnicodeDecodeError。"""
        p = os.path.join(self.tmpdir, "options.txt")
        with open(p, "wb") as f:
            f.write(b"# \xff\xff\nkey_key.chat:key.keyboard.space\n")
        self.assertEqual(read_chat_key_from_options(p), "space")

    def test_missing_file_returns_none(self):
        self.assertIsNone(
            read_chat_key_from_options(os.path.join(self.tmpdir, "nope.txt")))

    def test_missing_chat_line_returns_none(self):
        p = self.write_options("version:3465\nkey_keyboard.sneak:true\n")
        self.assertIsNone(read_chat_key_from_options(p))

    def test_empty_chat_value_returns_none(self):
        p = self.write_options("key_key.chat:\n")
        self.assertIsNone(read_chat_key_from_options(p))

    def test_unmappable_mc_key_returns_none(self):
        """键名认识但 MC_KEY_TO_PYAUTOGUI 里没有(如自定义绑定 f13)→ None,由上层回退。"""
        p = self.write_options("key_key.chat:key.keyboard.f13\n")
        self.assertIsNone(read_chat_key_from_options(p))

    def test_stops_at_first_chat_line(self):
        """只取第一处 key_key.chat,后面的同名行不得覆盖。"""
        p = self.write_options("key_key.chat:key.keyboard.t\nkey_key.chat:key.keyboard.y\n")
        self.assertEqual(read_chat_key_from_options(p), "t")

    def test_prefix_match_is_exact_not_substring(self):
        """key_key.chatwalk: 不是聊天键配置,不得误命中。"""
        p = self.write_options("key_key.chatwalk:key.keyboard.t\n")
        self.assertIsNone(read_chat_key_from_options(p))


# ---------------------------------------------------------------------------
# resolve_chat_key
# ---------------------------------------------------------------------------
class TestResolveChatKey(WindowInputMockBase):

    def test_auto_reads_options_txt(self):
        p = self.write_options("key_key.chat:key.keyboard.enter\n")
        self.assertEqual(resolve_chat_key({"chat_key": "auto", "game_options": p}),
                         "enter")

    def test_auto_falls_back_to_t_when_file_missing(self):
        self.assertEqual(
            resolve_chat_key({"chat_key": "auto",
                              "game_options": os.path.join(self.tmpdir, "nope.txt")}),
            "t")

    def test_auto_falls_back_to_t_when_key_unmappable(self):
        p = self.write_options("key_key.chat:key.keyboard.f13\n")
        self.assertEqual(resolve_chat_key({"chat_key": "auto", "game_options": p}), "t")

    def test_auto_falls_back_to_t_when_options_path_empty(self):
        self.assertEqual(resolve_chat_key({"chat_key": "auto"}), "t")

    def test_chinese_alias(self):
        self.assertEqual(resolve_chat_key({"chat_key": "回车"}), "enter")

    def test_chinese_alias_whitespace_trimmed(self):
        self.assertEqual(resolve_chat_key({"chat_key": " 空格 "}), "space")

    def test_direct_key_name_lowercased(self):
        self.assertEqual(resolve_chat_key({"chat_key": "T"}), "t")

    def test_unknown_key_passes_through_lowercased(self):
        """无法识别的键名原样交给 pyautogui(用户自定义键位),不猜不改。"""
        self.assertEqual(resolve_chat_key({"chat_key": "F9"}), "f9")

    def test_missing_chat_key_defaults_to_auto(self):
        p = self.write_options("key_key.chat:key.keyboard.slash\n")
        self.assertEqual(resolve_chat_key({"game_options": p}), "/")


# ---------------------------------------------------------------------------
# HumanInput._rand / wait_between_commands / open_chat / _open_chat
# ---------------------------------------------------------------------------
class TestDelays(WindowInputMockBase):

    def test_rand_defaults_when_no_delays_configured(self):
        h = self.make_input()
        self.assertEqual(h._rand("min_a", "max_a"), 0.5)
        self.uniform.assert_called_once_with(0.3, 0.6)

    def test_rand_uses_configured_bounds(self):
        h = self.make_input(delays={"min_a": 1.0, "max_a": 2.0})
        h._rand("min_a", "max_a")
        self.uniform.assert_called_once_with(1.0, 2.0)

    def test_wait_between_commands_sleeps_in_range(self):
        h = self.make_input(delays={"min_send_interval": 5.0, "max_send_interval": 6.0})
        h.wait_between_commands()
        self.uniform.assert_called_once_with(5.0, 6.0)
        self.sleep.assert_called_once_with(0.5)

    def test_open_chat_presses_chat_key_then_waits(self):
        h = self.make_input(chat_key="enter",
                            delays={"min_chat_key_delay": 0.11, "max_chat_key_delay": 0.22})
        h.open_chat()
        self.press.assert_called_once_with("enter")
        self.uniform.assert_called_once_with(0.11, 0.22)
        self.sleep.assert_called_once_with(0.5)


class TestOpenChatDetector(WindowInputMockBase):
    """_open_chat 的 detector 分支:聊天栏已开则跳过按 toggle 键。"""

    def test_skips_press_when_chat_already_open(self):
        """重点场景:Alt+L 是 toggle,已开时再按会误关 → 必须跳过。"""
        h = self.make_input(chat_key="l")
        det = FakeDetector(available=True, result=(True, True))
        h._open_chat(detector=det)
        self.assertEqual(det.detect_calls, 1)
        self.press.assert_not_called()

    def test_presses_when_chat_closed(self):
        h = self.make_input(chat_key="l")
        det = FakeDetector(available=True, result=(False, False))
        h._open_chat(detector=det)
        self.press.assert_called_once_with("l")

    def test_detector_unavailable_still_presses(self):
        """OCR 引擎不可用(winsdk 缺失)→ detector.available=False,照常按键。"""
        h = self.make_input(chat_key="t")
        det = FakeDetector(available=False)
        h._open_chat(detector=det)
        self.assertEqual(det.detect_calls, 0)
        self.press.assert_called_once_with("t")

    def test_detector_exception_falls_back_to_press(self):
        """detect() 抛异常不得中断发送 —— 降级为直接按聊天键。"""
        h = self.make_input(chat_key="t")
        det = FakeDetector(available=True, raises=True)
        h._open_chat(detector=det)
        self.press.assert_called_once_with("t")


# ---------------------------------------------------------------------------
# _copy 剪贴板重试
# ---------------------------------------------------------------------------
class TestCopyRetry(WindowInputMockBase):

    def test_first_attempt_success_no_sleep(self):
        h = self.make_input()
        h._copy("你好")
        self.pyperclip.copy.assert_called_once_with("你好")
        self.sleep.assert_not_called()

    def test_retries_then_succeeds(self):
        """重点场景5:剪贴板被占用时前两次失败、第三次成功 → 不抛。"""
        h = self.make_input()
        self.pyperclip.copy.side_effect = [
            PyperclipBusy("占用"), PyperclipBusy("占用"), None]
        h._copy("你好")
        self.assertEqual(self.pyperclip.copy.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(0.3), call(0.3)])

    def test_exhausted_retries_raises_last_error(self):
        """重试耗尽才抛,且抛的是最后一次的真实异常。"""
        h = self.make_input()
        boom = PyperclipBusy("一直占用")
        self.pyperclip.copy.side_effect = boom
        with self.assertRaises(PyperclipBusy) as cm:
            h._copy("你好", retries=3)
        self.assertIs(cm.exception, boom)
        self.assertEqual(self.pyperclip.copy.call_count, 3)
        # 中间只 sleep 2 次,最后一次失败直接 raise 不再睡
        self.assertEqual(self.sleep.call_args_list, [call(0.3), call(0.3)])

    def test_no_retry_sleep_when_retries_is_one(self):
        """retries=1 时首次失败即抛,不应有多余 sleep。"""
        h = self.make_input()
        self.pyperclip.copy.side_effect = PyperclipBusy("占用")
        with self.assertRaises(PyperclipBusy):
            h._copy("你好", retries=1)
        self.sleep.assert_not_called()


class PyperclipBusy(Exception):
    """模拟 pyperclip.copy 在剪贴板被占用时的异常(生产上多为 PyperclipException)。"""


# ---------------------------------------------------------------------------
# send_chat_text
# ---------------------------------------------------------------------------
class TestSendChatText(WindowInputMockBase):

    def test_happy_path_press_paste_enter(self):
        """开栏 → ctrl+v → enter,顺序固定。"""
        h = self.make_input(chat_key="t")
        h.send_chat_text("/guild promote 甲")
        self.assertEqual(self.press.call_args_list, [call("t"), call("enter")])
        self.hotkey.assert_called_once_with("ctrl", "v")
        self.pyperclip.copy.assert_called_once_with("/guild promote 甲")

    def test_paste_delay_uses_configured_bounds(self):
        """粘贴→回车之间的延时用配置边界,不是默认 0.3/0.6。"""
        h = self.make_input(delays={"min_paste_to_enter": 0.7, "max_paste_to_enter": 0.9})
        h.send_chat_text("hi")
        self.assertIn(call(0.7, 0.9), self.uniform.call_args_list)

    def test_detector_chat_open_skips_chat_key_press(self):
        """detector 报 chat_open=True → 只按 enter,不按聊天键(toggle 防误关)。"""
        h = self.make_input(chat_key="l")
        h.send_chat_text("hi", detector=FakeDetector(result=(True, True)))
        self.assertEqual(self.press.call_args_list, [call("enter")])

    def test_param_stop_check_aborts_before_any_action(self):
        """重点场景6:传入的 stop_check 为真 → 一步都不做,返回 False。"""
        h = self.make_input()
        self.assertFalse(h.send_chat_text("hi", stop_check=lambda: True))
        self.press.assert_not_called()
        self.hotkey.assert_not_called()
        self.pyperclip.copy.assert_not_called()

    def _trip_after(self, n):
        """返回一个调用 n 次后恒为 True 的 stop_check。"""
        state = {"n": 0}

        def check():
            state["n"] += 1
            return state["n"] > n
        return check

    def test_self_stop_check_aborts_before_copy(self):
        h = self.make_input(stop_check=self._trip_after(0))
        self.assertFalse(h.send_chat_text("hi"))
        self.press.assert_called_once_with("t")  # 已开栏
        self.pyperclip.copy.assert_not_called()
        self.hotkey.assert_not_called()

    def test_self_stop_check_aborts_before_paste(self):
        h = self.make_input(stop_check=self._trip_after(1))
        self.assertFalse(h.send_chat_text("hi"))
        self.pyperclip.copy.assert_called_once_with("hi")
        self.hotkey.assert_not_called()
        self.assertNotIn(call("enter"), self.press.call_args_list)

    def test_self_stop_check_aborts_before_enter(self):
        """暂停发生在粘贴之后 → 不得再按 enter(否则把半截内容发出去)。"""
        h = self.make_input(stop_check=self._trip_after(2))
        self.assertFalse(h.send_chat_text("hi"))
        self.hotkey.assert_called_once_with("ctrl", "v")
        self.assertEqual(self.press.call_args_list, [call("t")])

    def test_self_stop_check_false_all_the_way_completes(self):
        h = self.make_input(stop_check=lambda: False)
        h.send_chat_text("hi")
        self.assertEqual(self.press.call_args_list, [call("t"), call("enter")])

    def test_clipboard_failure_propagates_and_skips_enter(self):
        """剪贴板彻底写不进去时不得继续粘贴/回车。"""
        h = self.make_input()
        self.pyperclip.copy.side_effect = PyperclipBusy("占用")
        with self.assertRaises(PyperclipBusy):
            h.send_chat_text("hi")
        self.hotkey.assert_not_called()
        self.assertEqual(self.press.call_args_list, [call("t")])


# ---------------------------------------------------------------------------
# paste_and_enter / send_command
# ---------------------------------------------------------------------------
class TestPasteAndEnter(WindowInputMockBase):

    def test_pastes_without_pressing_chat_key(self):
        """试探式发送:不按 chat_key,只 ctrl+v + enter,并返回 True。"""
        h = self.make_input(chat_key="t")
        self.assertTrue(h.paste_and_enter("/guild member 甲"))
        self.press.assert_called_once_with("enter")
        self.hotkey.assert_called_once_with("ctrl", "v")
        self.pyperclip.copy.assert_called_once_with("/guild member 甲")

    def _trip_after(self, n):
        state = {"n": 0}

        def check():
            state["n"] += 1
            return state["n"] > n
        return check

    def test_stop_before_copy_returns_false(self):
        h = self.make_input()
        self.assertFalse(h.paste_and_enter("hi", stop_check=lambda: True))
        self.pyperclip.copy.assert_not_called()
        self.press.assert_not_called()

    def test_stop_after_copy_skips_paste(self):
        """第 2 道守卫:已写剪贴板但被暂停 → 不得粘贴。"""
        h = self.make_input()
        self.assertFalse(h.paste_and_enter("hi", stop_check=self._trip_after(1)))
        self.pyperclip.copy.assert_called_once_with("hi")
        self.hotkey.assert_not_called()

    def test_stop_after_paste_skips_enter(self):
        """第 3 道守卫:已粘贴但被暂停 → 不得回车(否则发出半截/旧内容)。"""
        h = self.make_input()
        self.assertFalse(h.paste_and_enter("hi", stop_check=self._trip_after(2)))
        self.hotkey.assert_called_once_with("ctrl", "v")
        self.press.assert_not_called()

    def test_stop_check_consulted_once_per_step(self):
        """契约:paste_and_enter 恰有 3 道暂停门(复制前 / 粘贴前 / 回车前),
        enter 之后不再检查 —— 命令已发出,停不停都已生效。"""
        h = self.make_input()
        check = MagicMock(return_value=False)
        self.assertTrue(h.paste_and_enter("hi", stop_check=check))
        self.assertEqual(check.call_count, 3)

    def test_no_stop_check_returns_true(self):
        h = self.make_input()
        self.assertTrue(h.paste_and_enter("hi"))
        self.assertEqual(self.press.call_args_list, [call("enter")])


class TestSendCommand(WindowInputMockBase):

    def test_send_and_wait(self):
        h = self.make_input(chat_key="t")
        h.send_command("/guild promote 甲")
        self.assertEqual(self.press.call_args_list, [call("t"), call("enter")])
        self.uniform.assert_any_call(0.3, 0.6)

    def test_send_without_wait_skips_interval(self):
        h = self.make_input(chat_key="t")
        with patch.object(HumanInput, "wait_between_commands") as w:
            h.send_command("/guild promote 甲", wait=False)
        w.assert_not_called()

    def test_send_with_wait_calls_interval(self):
        h = self.make_input(chat_key="t")
        with patch.object(HumanInput, "wait_between_commands") as w:
            h.send_command("/guild promote 甲", wait=True)
        w.assert_called_once_with()

    def test_no_pyautogui_module_call_leaked(self):
        """自检:整个测试文件从不触碰真实 pyautogui 动作入口。"""
        h = self.make_input()
        h.send_chat_text("hi")
        h.paste_and_enter("hi")
        h.send_command("/guild kick 乙")
        for forbidden in ("click", "moveTo", "write", "typewrite", "screenshot"):
            self.assertFalse(getattr(self.pyautogui, forbidden).called, forbidden)


if __name__ == "__main__":
    unittest.main()
