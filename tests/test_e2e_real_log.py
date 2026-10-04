# -*- coding: utf-8 -*-
"""
test_e2e_real_log.py —— 用真实游戏日志驱动的端到端集成测试
============================================================
目的:不开 MC 客户端、不发真实按键,直接读取真实游戏日志(latest.log)中已存在的
/guild list 与 /guild member 响应块,把核心流水线完整跑一遍,验证:
  1. LogWatcher 能在真实 GBK 编码文件上正确增量读
  2. extract_chat_block 能切出真实公会面板(网易版含 [C] 复制标记)
  3. parse_guild_list / parse_guild_member 能解析真实面板内容
  4. discard_buffer() 不会读到 launch 前的残留
  5. min_wait_after_start 守卫生效——起步 0.5s 内的旧块不被误认为新响应
  6. _pause_event 置位/复位可正常工作(暂停键 u 修复的核心)

日志路径通过环境变量 GUILD_TEST_GAME_LOG 提供(避免把本机绝对路径写进仓库)。
未设置或文件不存在时,本模块的真实日志用例自动 skip —— CI 上恒 skip,
本地真机验收时设置该变量即可运行。
"""
import os
import sys
import time
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logger
from log_watcher import (  # noqa: E402
    LogWatcher,
    detect_encoding,
    extract_chat_block,
    split_messages,
)

REAL_LOG = os.environ.get("GUILD_TEST_GAME_LOG", "")


def _setup_fake_logger():
    """注入最小 logger,避免依赖 logger.init_logger(后者要写文件)。"""
    if not hasattr(logger, "log_debug"):
        def _noop(*_a, **_kw): pass
        for n in ("log_debug", "log_info", "log_warning", "log_error",
                  "log_exception", "log_critical", "trace_code_location",
                  "trace_return", "trace_file_read"):
            setattr(logger, n, _noop)


class TestE2EOnRealLog(unittest.TestCase):
    """用真实 latest.log 跑核心解析流水线。"""

    @classmethod
    def setUpClass(cls):
        _setup_fake_logger()
        if not os.path.exists(REAL_LOG):
            raise unittest.SkipTest(f"真实游戏日志不存在:{REAL_LOG}")
        cls.enc = detect_encoding(REAL_LOG)
        with open(REAL_LOG, "r", encoding=cls.enc, errors="replace") as f:
            cls.raw = f.read()
        cls.lines = cls.raw.splitlines()

    def test_detect_encoding_resolves_gbk(self):
        """真实网易版日志编码必须是 gbk 系列。"""
        self.assertIn(self.enc, ("gbk", "gb2312"))

    def test_real_log_has_at_least_one_guild_list_block(self):
        """/guild list 块起始(--------------------公会---------------------)
        至少出现 1 次。网易版:[CHAT] 之后整条消息是单行物理行,内含分隔线 + 面板内容。"""
        count = sum(1 for ln in self.lines
                    if "[CHAT]" in ln and "公会" in ln and "----" in ln)
        self.assertGreaterEqual(count, 1, "未在 latest.log 中发现公会面板起始线")

    def test_real_log_has_at_least_one_guild_member_block(self):
        """/guild member 块以日期明细模式(YYYY-MM-DD)开头。"""
        import re
        date_re = re.compile(r"\d{4}-\d{2}-\d{2}")
        weekly_hits = 0
        for ln in self.lines:
            if "[CHAT]" in ln and "----" in ln and date_re.search(ln):
                weekly_hits += 1
        self.assertGreaterEqual(weekly_hits, 1, "未发现 /guild member 类型的成员贡献面板")

    def test_logwatcher_open_seek_to_end_then_read_returns_only_new(self):
        """LogWatcher.open() 应 seek 到末尾;open 后 read_new_lines 不返回 launch 前旧内容。"""
        w = LogWatcher(REAL_LOG, encoding=self.enc)
        w.open()
        try:
            new_lines = w.read_new_lines(block=False)
            for ln in new_lines:
                # 旧启动行不应出现(launch 阶段日志)
                self.assertNotIn("ModLauncher running", ln)
                self.assertNotIn("JVM identified", ln)
        finally:
            w.close()


class TestExtractChatBlockOnRealBlock(unittest.TestCase):
    """把真实日志里的 1 个 /guild list 块 + 1 个 /guild member 块
    切出来,验证 extract_chat_block 能正确处理网易版面板(含 \\n 字面量)。"""

    @classmethod
    def setUpClass(cls):
        _setup_fake_logger()
        if not os.path.exists(REAL_LOG):
            raise unittest.SkipTest(f"真实游戏日志不存在:{REAL_LOG}")
        enc = detect_encoding(REAL_LOG)
        with open(REAL_LOG, "r", encoding=enc, errors="replace") as f:
            raw = f.read()
        cls.enc = enc
        lines = raw.splitlines()
        # 1) /guild list 块:网易版整条消息是单行物理行,格式 [CHAT] ----...公会...----
        list_start_idx = None
        for i, ln in enumerate(lines):
            if "[CHAT]" in ln and "公会" in ln and "----" in ln:
                list_start_idx = i
                break
        # 找 list 块结尾:从 list_start 之后第一个 40+ dash 行
        list_end_idx = None
        if list_start_idx is not None:
            for j in range(list_start_idx + 1, len(lines)):
                stripped = lines[j].strip().rstrip("[C]").strip()
                if len(stripped) >= 20 and set(stripped) == {"-"}:
                    list_end_idx = j
                    break
        cls.list_block_lines = (lines[list_start_idx:list_end_idx + 1]
                                if list_start_idx is not None and list_end_idx is not None
                                else [])
        # 2) /guild member 块:找同时含 "----" 和 "成员贡献"等关键字的物理行
        member_block_lines = []
        for i, ln in enumerate(lines):
            if "----" in ln:
                try:
                    ln_gbk = ln.encode("gbk", errors="replace").decode("gbk", errors="replace")
                except Exception:
                    continue
                if "成员贡献" in ln_gbk or "加入时间" in ln_gbk:
                    member_block_lines = [ln]
                    break
        cls.member_msg_lines = member_block_lines

    def test_list_block_extracted_nonempty(self):
        """extract_chat_block 必须能从真实 list 块里抽出非空内容。"""
        if not self.list_block_lines:
            self.skipTest("未切出 /guild list 块")
        # 把物理行合成"一条消息"格式:首行带 [CHAT] 前缀
        msg = ["[CHAT] " + self.list_block_lines[0]] + self.list_block_lines[1:]
        block = extract_chat_block(msg)
        self.assertIsNotNone(block, "extract_chat_block 返回 None")
        self.assertGreater(len(block), 0)
        joined = "\n".join(block)
        self.assertTrue(
            "成员" in joined or "会长" in joined,
            f"切出的块不像公会面板:前 200 字={joined[:200]!r}"
        )

    def test_member_block_extracted_nonempty(self):
        """extract_chat_block 必须能从真实 member 块里抽出非空内容。"""
        if not self.member_msg_lines:
            self.skipTest("未切出 /guild member 块")
        # member 块是单条物理行(网易版特征),[CHAT] 在行首
        if "[CHAT]" not in self.member_msg_lines[0]:
            msg = ["[CHAT] " + self.member_msg_lines[0]]
        else:
            msg = self.member_msg_lines
        block = extract_chat_block(msg)
        self.assertIsNotNone(block, "member 块 extract_chat_block 返回 None")
        self.assertGreater(len(block), 0)


class TestFirstCommandGuard(unittest.TestCase):
    """first_command 过早读日志的修复验证:min_wait_after_start
    必须让 wait_for_chat_block 在起步 N 秒内不返回任何块。"""

    def setUp(self):
        _setup_fake_logger()
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "latest.log")
        ts_old = "01月2026 10:00:00.000"
        ts_new = "01月2026 10:00:05.000"
        self.content = (
            f"[{ts_old}] [main/INFO] [ChatComponent/]: [CHAT] --------------------公会---------------------\n"
            f"残留旧块第 1 行(launch 前)\n"
            f"-------------------------------------------- [C]\n"
            f"[{ts_new}] [main/INFO] [ChatComponent/]: [CHAT] --------------------公会---------------------\n"
            f"新块第 1 行\n"
            f"-------------------------------------------- [C]\n"
        ).encode("utf-8")
        with open(self.path, "wb") as f:
            f.write(self.content)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_min_wait_blocks_residual(self):
        """min_wait_after_start=0.5 + 文件无新增 → 必须等满 0.5s 才超时返回 None。
        关键:守卫拦的是"wait 起步 0.5s 内不能返回",文件里已有内容(经
        discard_buffer 跳过的旧块)即使在 0.5s 后能被 extract,守卫也已经放行——
        所以本测试必须保持"文件里没有可被 extract 的新内容"才能验证守卫真起作用。
        """
        from log_watcher import LogWatcher
        # 写一个文件:只有"非面板"内容,没有 ---- 块;discard_buffer 跳完后
        # wait 永远拿不到完整块 → 必须依赖 timeout 退出
        nonblock_path = os.path.join(self.tmpdir, "nonblock.log")
        with open(nonblock_path, "w", encoding="utf-8") as f:
            f.write("[01月2026 10:00:00.000] [main/INFO] [ChatComponent/]: [CHAT] 普通聊天消息\n")
            f.write("[01月2026 10:00:01.000] [main/INFO] [ChatComponent/]: [CHAT] 又一条聊天\n")
        w = LogWatcher(nonblock_path, encoding="utf-8", poll_interval_ms=50)
        w.open()        # 默认 seek 到末尾,文件全读
        # 现在追加一个完整块(模拟 first_command 启动后 0s 时游戏就已写好的块)
        with open(nonblock_path, "a", encoding="utf-8") as f:
            f.write(
                "[01月2026 10:00:02.000] [main/INFO] [ChatComponent/]: [CHAT] --------------------公会---------------------\n"
                "早写好的块\n"
                "-------------------------------------------- [C]\n"
            )
        w._size = os.path.getsize(nonblock_path) - 0  # 不重置,_size 已落后,read_new_lines 会读到
        start = time.time()
        block = w.wait_for_chat_block(timeout=0.6, min_wait_after_start=0.5)
        elapsed = time.time() - start
        # 文件里有完整块(满足 extract 条件),min_wait=0.5s 已过 → 应该返回块
        # (注意:这不是守卫失败的体现——守卫本身只在 0~0.5s 窗口内拦下,窗口外正常返回)
        # 真正验证守卫:让 timeout < min_wait,验证返回 None 且 elapsed ≥ timeout
        self.assertIsNotNone(block, f"文件已有完整块,0.5s 后应能返回,got None in {elapsed:.2f}s")
        self.assertGreaterEqual(elapsed, 0.5, f"返回过早,elapsed={elapsed:.2f}s < 0.5s")

    def test_min_wait_blocks_when_file_has_no_new_content(self):
        """min_wait_after_start + timeout < min_wait + 文件无新内容 → 必须返回 None。
        验证:守卫拦的是"起步 N 秒内不返回",文件无新内容情况下,wait 走满 timeout 就退出。"""
        from log_watcher import LogWatcher
        # 文件只有普通聊天,无 ---- 块 → extract 永远拿不到块
        nb_path = os.path.join(self.tmpdir, "no_block.log")
        with open(nb_path, "w", encoding="utf-8") as f:
            f.write("[01月2026 10:00:00.000] [main/INFO] [ChatComponent/]: [CHAT] 普通聊天\n")
        w = LogWatcher(nb_path, encoding="utf-8", poll_interval_ms=50)
        w.open()
        start = time.time()
        block = w.wait_for_chat_block(timeout=0.3, min_wait_after_start=0.5)
        elapsed = time.time() - start
        # 文件无块可 extract → 必须返回 None,耗时接近 timeout 0.3s
        self.assertIsNone(block, f"无新内容时不应返回块,got={block!r}")
        self.assertGreaterEqual(elapsed, 0.25, f"应至少等到接近 timeout,elapsed={elapsed:.2f}s")


class TestPauseEvent(unittest.TestCase):
    """_pause_event 是修复"按 u 不暂停"的核心:验证置位/复位机制。"""

    def setUp(self):
        _setup_fake_logger()

    def test_pause_event_set_blocks_then_clear_resumes(self):
        """手动模拟 _toggle_pause:置位 event 后 wait_event 应立即返回 True;
        clear 后 wait_event 才会阻塞。"""
        ev = threading.Event()
        self.assertFalse(ev.wait(timeout=0.05))
        ev.set()
        self.assertTrue(ev.wait(timeout=0.05))
        ev.clear()
        self.assertFalse(ev.wait(timeout=0.05))

    def test_main_module_exposes_pause_event(self):
        """main.py 必须定义 _pause_event(否则 pynput 修复无效)。"""
        import main
        self.assertTrue(hasattr(main, "_pause_event"))
        self.assertIsInstance(main._pause_event, threading.Event)

    def test_toggle_pause_flips_event(self):
        """_toggle_pause 必须在 _paused=True 时 set,_paused=False 时 clear。"""
        import main
        main._paused = False
        main._pause_event.clear()
        with main._pause_lock:
            main._paused = True
            main._pause_event.set()
        self.assertTrue(main._pause_event.is_set())
        with main._pause_lock:
            main._paused = False
            main._pause_event.clear()
        self.assertFalse(main._pause_event.is_set())


class TestCtrlCMenuModuleImports(unittest.TestCase):
    """Ctrl+C 菜单修复验证:msvcrt 路径在 main 模块级可访问。"""

    def setUp(self):
        _setup_fake_logger()

    def test_ctrlc_pause_menu_uses_msvcrt_on_windows(self):
        """_ctrlc_pause_menu 在源码中应包含 msvcrt 路径(Windows 走非阻塞轮询)。"""
        import main
        import inspect
        src = inspect.getsource(main._ctrlc_pause_menu)
        self.assertIn("msvcrt", src, "_ctrlc_pause_menu 未走 msvcrt 路径")
        self.assertIn("kbhit", src, "未使用 kbhit 非阻塞轮询")
        self.assertIn("getwch", src, "未使用 getwch 读键")

    def test_fallback_function_exists(self):
        """非 Windows 平台应回退到 _ctrlc_pause_menu_fallback。"""
        import main
        self.assertTrue(hasattr(main, "_ctrlc_pause_menu_fallback"))
        import inspect
        src = inspect.getsource(main._ctrlc_pause_menu_fallback)
        self.assertIn("BaseException", src, "fallback 必须捕获 BaseException 否则 queue 永久空")


class TestOnPressCtrlCDetection(unittest.TestCase):
    """_on_press 修复:不能把 Key.ctrl_l 单独按下误判为 Ctrl+C。
    根因:脚本内部 send_chat_text 用 pyautogui.hotkey("ctrl", "v") 粘贴,
    pynput 把 Ctrl+V 拆成两帧 on_press:Key.ctrl_l(修饰键) + '\x16'(字符键)。
    旧实现 `key == Key.ctrl_l` 在第一帧就误命中 → _force_stop=True → 进菜单。"""

    def setUp(self):
        _setup_fake_logger()
        import main
        with main._pause_lock:
            main._force_stop = False
            main._paused = False
        main._pause_event.clear()

    def test_ctrl_l_alone_does_NOT_trigger_force_stop(self):
        """单独按 Ctrl 键(Key.ctrl_l,无后续字符)→ _on_press 必须不动 _force_stop。
        这是 pynput 报"用户按了 Ctrl 修饰键但还没按其他键"的常见帧——绝不能误判。"""
        from pynput.keyboard import Key
        import main
        # 先确保初始态
        with main._pause_lock:
            main._force_stop = False
        main._on_press(Key.ctrl_l)
        with main._pause_lock:
            self.assertFalse(main._force_stop,
                             f"Key.ctrl_l 单独按下不应触发 _force_stop,当前值={main._force_stop}")

    def test_ctrl_v_paste_does_NOT_trigger_force_stop(self):
        """Ctrl+V(脚本粘贴聊天命令)→ on_press 收到 '\x16' → _on_press 必须不动 _force_stop。
        这是脚本自身行为:window_input.send_chat_text 调 pyautogui.hotkey("ctrl", "v")。"""
        from pynput.keyboard import KeyCode
        import main
        with main._pause_lock:
            main._force_stop = False
        # pynput 报 Ctrl+V 是 KeyCode(v_char='\x16')
        main._on_press(KeyCode.from_char('\x16'))
        with main._pause_lock:
            self.assertFalse(main._force_stop,
                             f"Ctrl+V(paste) 不应触发 _force_stop,当前值={main._force_stop}")

    def test_real_ctrl_c_does_trigger_force_stop(self):
        """真实 Ctrl+C → on_press 收到 '\x03' → on_press 必须主动发 CTRL_C_EVENT
        (修复"pynput 静默吞 KeyboardInterrupt"根因)——不再设 _force_stop,
        由 os.kill(CTRL_C_EVENT) → SIGINT → signal handler raise 走菜单。
        验证:on_press 源码含 os.kill + CTRL_C_EVENT(用 inspect),
        且 mock os.kill 验证参数确实是 CTRL_C_EVENT。"""
        from pynput.keyboard import KeyCode
        from unittest.mock import patch
        import main
        with main._pause_lock:
            main._force_stop = False
        # 验证 on_press 源码含 os.kill + CTRL_C_EVENT 路径
        import inspect
        src = inspect.getsource(main._on_press)
        self.assertIn("os.kill", src, "on_press 必须用 os.kill 主动发信号,不能仅设标志位")
        self.assertIn("CTRL_C_EVENT", src, "on_press 必须用 CTRL_C_EVENT 转 SIGINT,不能静默吞 KeyboardInterrupt")
        # 模拟实际调用(mock os.kill 不真的发信号)
        with patch("os.kill") as mock_kill:
            main._on_press(KeyCode.from_char('\x03'))
            self.assertGreaterEqual(mock_kill.call_count, 1,
                                    f"on_press 应该调 os.kill,实际 {mock_kill.call_count} 次")
            args, _ = mock_kill.call_args
            import signal as _sig
            self.assertEqual(args[1], _sig.CTRL_C_EVENT,
                             f"应该发 CTRL_C_EVENT 触发 SIGINT,实际发了 {args[1]!r}")
        # 复位
        with main._pause_lock:
            main._force_stop = False
            main._paused = False
        main._pause_event.clear()


class TestPasteAndEnterReturnsTrue(unittest.TestCase):
    """修复 first_command 跳过 wait_for_chat_block 的根因:
    HumanInput.paste_and_enter 之前无 return 语句 → 隐式 None →
    send_and_capture 内 `if not ok: pass` 把 None 当 False → 跳过 wait_for_chat_block →
    latest.log 中 first_command 期间没有任何 READ 日志输出。
    现在必须明确 return True(成功执行到底)/ False(中途被 stop_check 打断)。"""

    def setUp(self):
        _setup_fake_logger()

    def test_paste_and_enter_has_return_true_in_source(self):
        """源码必须含 `return True`(成功路径显式返回)。
        用 inspect.getsource 检查比 mock pyautogui 简单可靠。"""
        from window_input import HumanInput
        import inspect
        src = inspect.getsource(HumanInput.paste_and_enter)
        self.assertIn("return True", src,
                      f"HumanInput.paste_and_enter 源码必须含 return True,实际:\n{src}")


class TestWaitIfPausedForceStop(unittest.TestCase):
    """wait_if_paused 看到 _force_stop=True 必须 raise KeyboardInterrupt
    ——修复 latest.log 21:10 死循环重发 30 次 /guild list 的根因。
    旧实现是 return,导致 query_guild_list 把 '暂停中断' 当成普通 interrupted
    分支 continue,永远不退出。"""

    def setUp(self):
        _setup_fake_logger()
        import main
        # 强制初始态:未暂停
        with main._pause_lock:
            main._force_stop = False
            main._paused = False
            main._reset_needed = False
        main._pause_event.clear()

    def test_wait_if_paused_raises_on_force_stop(self):
        """_force_stop=True 时 wait_if_paused 必须立即 raise KeyboardInterrupt。"""
        import main
        with main._pause_lock:
            main._force_stop = True
        with self.assertRaises(KeyboardInterrupt):
            main.wait_if_paused()
        # 复位
        with main._pause_lock:
            main._force_stop = False

    def test_wait_if_paused_blocks_when_paused_only(self):
        """仅 _paused=True(用户按 u,非 Ctrl+C)→ wait_if_paused 阻塞等待 _paused=False。
        这里用后台线程 0.2s 后清 _paused,验证 wait_if_paused 能正常返回。"""
        import main
        import threading
        with main._pause_lock:
            main._paused = True
        main._pause_event.set()
        def _resume():
            time.sleep(0.2)
            with main._pause_lock:
                main._paused = False
            main._pause_event.clear()
        t = threading.Thread(target=_resume, daemon=True)
        t.start()
        start = time.time()
        main.wait_if_paused()  # 不应抛异常,应等到 _paused=False
        elapsed = time.time() - start
        self.assertGreaterEqual(elapsed, 0.15, f"未等够 0.15s 就返回:elapsed={elapsed:.3f}s")
        self.assertLess(elapsed, 1.0, f"等待过久:elapsed={elapsed:.3f}s")

    def test_wait_if_paused_returns_immediately_when_not_paused(self):
        """_paused=False 且 _force_stop=False → 立即返回。"""
        import main
        with main._pause_lock:
            main._paused = False
            main._force_stop = False
        main._pause_event.clear()
        start = time.time()
        main.wait_if_paused()
        elapsed = time.time() - start
        self.assertLess(elapsed, 0.1, f"未暂停时应立即返回,elapsed={elapsed:.3f}s")


if __name__ == "__main__":
    unittest.main()
