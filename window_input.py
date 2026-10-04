# -*- coding: utf-8 -*-
"""
window_input.py —— 窗口操作与模拟输入
======================================
职责:
  * 按关键词查找游戏窗口并置前(ctypes,零额外依赖)
  * 聊天键解析:auto(读 options.txt key_key.chat)/ 中文别名 / 直接键名
  * 模拟人工输入:聊天键 → 剪贴板粘贴 → 回车,带随机延时
"""
import ctypes
import os
import random
import time
from ctypes import wintypes

import pyautogui
import pyperclip

import logger
from config import KEY_ALIASES

# ---------------------------------------------------------------------------
# Windows 窗口 API(ctypes)
# ---------------------------------------------------------------------------
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
SW_RESTORE = 9
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
# 游戏进程白名单(Java 版 Minecraft;进程名小写)
GAME_PROCESS_NAMES = {"java.exe", "javaw.exe", "minecraft.exe", "minecraftlauncher.exe"}


def _get_process_name(hwnd):
    """获取窗口所属进程名(小写);失败返回空串。"""
    try:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
            return os.path.basename(buf.value).lower()
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return ""


def _enum_windows(exclude_pid=None):
    """枚举所有可见窗口,返回 [(hwnd, 标题), ...]。
    exclude_pid:排除指定进程的窗口(如脚本自身——打包为 exe 后窗口标题为 exe 名,
    若 exe 名含关键词,会被误判为游戏窗口)。"""
    results = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            if exclude_pid is not None:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == exclude_pid:
                    return True  # 跳过脚本自身窗口
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                results.append((hwnd, buf.value))
        return True

    user32.EnumWindows(_cb, 0)
    return results


def find_game_window(keywords):
    """按关键词查找游戏窗口,返回 hwnd 或 None。
    规则:
      * 始终排除脚本自身进程的窗口(打包 exe 名含关键词时,避免把脚本控制台窗口当游戏窗口);
      * 优先只匹配游戏进程(Java:java.exe/javaw.exe;及 minecraft.exe 等白名单)的窗口,
        避免其他程序标题含关键词时误判;
      * 若不存在白名单进程窗口,回退到全部窗口按标题匹配(兼容非 Java 客户端)。
    匹配顺序按配置列表:先命中的关键词优先。"""
    if not keywords:
        return None
    logger.trace_call("window_input.find_game_window", kwargs={"keywords": list(keywords)})
    lowered = [str(k).lower() for k in keywords]
    windows = _enum_windows(exclude_pid=os.getpid())
    game_windows = [w for w in windows if _get_process_name(w[0]) in GAME_PROCESS_NAMES]
    candidates = game_windows if game_windows else windows
    for win in candidates:
        title = win[1].lower()
        for kw in lowered:
            if kw in title:
                logger.trace_return("window_input.find_game_window",
                                    {"hwnd": win[0], "title": win[1]})
                return win[0]
    logger.trace_return("window_input.find_game_window", None)
    return None


def activate_window(hwnd):
    """置前并恢复窗口(最小化则还原)。SetForegroundWindow 受限时用 Alt 键解锁。"""
    logger.trace_code_location("window_input.activate_window", f"hwnd={hwnd}")
    user32.ShowWindow(hwnd, SW_RESTORE)
    # 绕过 SetForegroundWindow 的焦点限制:模拟一次 Alt 按下/抬起
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    ok = user32.SetForegroundWindow(hwnd)
    time.sleep(0.4)
    return bool(ok)


# ---------------------------------------------------------------------------
# 键名解析
# ---------------------------------------------------------------------------
# Minecraft options.txt 键名 → pyautogui 键名
MC_KEY_TO_PYAUTOGUI = {
    "key.keyboard.enter": "enter",
    "key.keyboard.space": "space",
    "key.keyboard.escape": "esc",
    "key.keyboard.tab": "tab",
    "key.keyboard.backspace": "backspace",
    "key.keyboard.delete": "delete",
    "key.keyboard.home": "home",
    "key.keyboard.end": "end",
    "key.keyboard.up": "up",
    "key.keyboard.down": "down",
    "key.keyboard.left": "left",
    "key.keyboard.right": "right",
    "key.keyboard.slash": "/",
    "key.keyboard.period": ".",
    "key.keyboard.comma": ",",
    "key.keyboard.minus": "-",
    "key.keyboard.equal": "=",
    "key.keyboard.left.bracket": "[",
    "key.keyboard.right.bracket": "]",
    "key.keyboard.backslash": "\\",
    "key.keyboard.semicolon": ";",
    "key.keyboard.apostrophe": "'",
    "key.keyboard.grave.accent": "`",
    "key.keyboard.left.shift": "shift",
    "key.keyboard.right.shift": "shiftright",
    "key.keyboard.left.control": "ctrl",
    "key.keyboard.right.control": "ctrlright",
    "key.keyboard.left.alt": "alt",
    "key.keyboard.right.alt": "altright",
    "key.keyboard.caps.lock": "capslock",
    "key.keyboard.print.screen": "printscreen",
    "key.keyboard.scroll.lock": "scrolllock",
    "key.keyboard.pause": "pause",
    "key.keyboard.insert": "insert",
    "key.keyboard.page.up": "pageup",
    "key.keyboard.page.down": "pagedown",
    "key.keyboard.num.lock": "numlock",
}
for _i in range(10):
    MC_KEY_TO_PYAUTOGUI[f"key.keyboard.{_i}"] = str(_i)
for _c in "abcdefghijklmnopqrstuvwxyz":
    MC_KEY_TO_PYAUTOGUI[f"key.keyboard.{_c}"] = _c
for _i in range(1, 13):
    MC_KEY_TO_PYAUTOGUI[f"key.keyboard.f{_i}"] = f"f{_i}"


def read_chat_key_from_options(options_path):
    """读取 options.txt 的 key_key.chat 值,返回 pyautogui 键名;失败返回 None。
    编码兼容:依次尝试 utf-8 / gbk / gb2312。"""
    try:
        raw = None
        with open(options_path, "rb") as f:
            data = f.read()
        logger.trace_file_read(options_path, len(data))
        for enc in ("utf-8", "gbk", "gb2312"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = data.decode("gbk", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("key_key.chat:"):
                raw = line.split(":", 1)[1].strip()
                break
    except OSError:
        return None
    if not raw:
        return None
    return MC_KEY_TO_PYAUTOGUI.get(raw)


def resolve_chat_key(cfg):
    """解析聊天键配置,返回 pyautogui 键名。
    支持:
      * "auto" —— 读 options.txt 的 key_key.chat(读取失败回退默认 't')
      * 中文别名 —— 如 "回车" → "enter"(见 config.KEY_ALIASES)
      * 直接 pyautogui 键名 —— 如 "t" / "enter" / "space"
    """
    key = str(cfg.get("chat_key", "auto")).strip()
    if key == "auto":
        resolved = read_chat_key_from_options(cfg.get("game_options", ""))
        if resolved:
            return resolved
        return "t"  # 回退:原脚本默认聊天键
    if key in KEY_ALIASES:
        return KEY_ALIASES[key]
    return key.lower()


# ---------------------------------------------------------------------------
# 模拟人工输入
# ---------------------------------------------------------------------------
class HumanInput:
    """模拟人工的键鼠输入(剪贴板粘贴中文,随机延时防检测)。"""

    def __init__(self, cfg, stop_check=None):
        self.chat_key = resolve_chat_key(cfg)
        self.delays = cfg.get("delays", {})
        self.stop_check = stop_check  # 暂停门回调

    def _rand(self, key_min, key_max):
        return random.uniform(self.delays.get(key_min, 0.3), self.delays.get(key_max, 0.6))

    def wait_between_commands(self):
        """命令间随机等待。"""
        time.sleep(self._rand("min_send_interval", "max_send_interval"))

    def _open_chat(self, detector=None):
        """按聊天键打开聊天栏。
        修复用户原话"输入指令但不发送+死循环":
        Alt+L 等 toggle 键第二次按会**关闭**已开的聊天栏,导致重试时 _open_chat 误关 → 后续命令
        输入无焦点。修复:若传 detector 且检测到 chat_open=True 则跳过按 chat_key,直接
        继续后续(避免 toggle 误关)。
        """
        if detector is not None and getattr(detector, "available", False):
            try:
                in_menu, chat_open = detector.detect()
                if chat_open:
                    # 聊天栏已开,不按 chat_key(避免 Alt+L 误关)
                    return
            except Exception:
                pass
        pyautogui.press(self.chat_key)
        time.sleep(self._rand("min_chat_key_delay", "max_chat_key_delay"))

    def open_chat(self):
        """显式打开聊天栏(按聊天键)。用于菜单关闭后、粘贴发送前的明确开栏。"""
        self._open_chat()

    def _copy(self, text, retries=5):
        """写入剪贴板,带重试(其他程序占用剪贴板时 OpenClipboard 失败,如"粘贴任务无法粘贴")。"""
        for i in range(retries):
            try:
                pyperclip.copy(text)
                return
            except Exception:
                if i >= retries - 1:
                    raise
                time.sleep(0.3)

    def send_chat_text(self, text, detector=None, stop_check=None):
        """发送一条聊天/命令:开聊天 → 粘贴 → 回车(标准流程)。
        first_command=True 时由 send_and_capture 改用 paste_and_enter 路径
        (跳过 _open_chat,避免与用户 chat_key 配置冲突)。
        detector:可选 GameStateDetector,若聊天栏已开则跳过 _open_chat 避免 toggle 误关
        """
        t0 = time.monotonic()
        logger.log_debug(f"[send_chat_text] 准备发送 text_len={len(text)} text={text[:60]!r}{'…' if len(text) > 60 else ''}")
        logger.trace_code_location("HumanInput.send_chat_text", f"text_len={len(text)}")
        logger.trace_code_location("HumanInput.send_chat_text.1_open_chat", "press chat key (若已开则跳过)")
        if stop_check and stop_check():
            return False
        self._open_chat(detector=detector)
        t1 = time.monotonic()
        logger.trace_code_location("HumanInput.send_chat_text.2_copy", "clipboard")
        # 修复"first_command 不发送"+"按 u 立即暂停":
        # 每步动作前检查 stop_check,避免按 u 后还在按 enter(后台已排队)
        if self.stop_check and self.stop_check():
            return False
        self._copy(text)
        t2 = time.monotonic()
        time.sleep(0.05)
        logger.trace_code_location("HumanInput.send_chat_text.3_paste", "ctrl+v")
        if self.stop_check and self.stop_check():
            return False
        pyautogui.hotkey("ctrl", "v")
        time.sleep(self._rand("min_paste_to_enter", "max_paste_to_enter"))
        logger.trace_code_location("HumanInput.send_chat_text.4_enter", "press enter")
        if self.stop_check and self.stop_check():
            return False
        pyautogui.press("enter")
        t3 = time.monotonic()
        logger.log_debug(f"[send_chat_text] 完成: 开栏={int((t1-t0)*1000)}ms 复制={int((t2-t1)*1000)}ms 粘贴回车={int((t3-t2)*1000)}ms 总={int((t3-t0)*1000)}ms")

    def paste_and_enter(self, text, stop_check=None):
        """试探式发送:直接粘贴 + 回车(不按 chat_key)。
        stop_check: 可选回调,每步动作前检查(避免按 u 期间仍继续)
        返回 True 表示执行到底,False 表示中途被 stop_check 打断。
        修复"first_command 跳过了 wait_for_chat_block":之前函数末尾无 return,
        调用方 `if not ok: pass` 把 None 视作 falsy 跳过等待 → 日志看不到 READ 输出。
        """
        logger.log_debug(f"试探式粘贴发送: {text[:60]}{'…' if len(text) > 60 else ''}")
        if stop_check and stop_check():
            return False
        self._copy(text)
        time.sleep(0.05)
        if stop_check and stop_check():
            return False
        pyautogui.hotkey("ctrl", "v")
        time.sleep(self._rand("min_paste_to_enter", "max_paste_to_enter"))
        if stop_check and stop_check():
            return False
        pyautogui.press("enter")
        return True

    def send_command(self, cmd, wait=True):
        """发送游戏命令;wait=True 时附加命令间随机等待。"""
        self.send_chat_text(cmd)
        if wait:
            self.wait_between_commands()
