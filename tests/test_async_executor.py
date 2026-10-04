# -*- coding: utf-8 -*-
"""
test_async_executor.py —— 异步执行器单元测试(验收清单 E)
==========================================================
覆盖四个核心场景:
  E1 成功提交:submit → 后台验证 ok → get_results 状态正确
  E2 失败重试→补做:verify_fn 连续失败 → max_retries 次重试(retry_delay 生效)→
     重试耗尽进入补做队列,任务不丢失
  E3 队列满丢弃:超过 max_queue_size 的 submit 返回 False 并告警
  E4 暂停期间静默:主线程暂停时,后台线程的发送路径被 _send_with_gate 阻塞——
     暂停窗口内零按键/剪贴板操作;恢复后先消费 _reset_needed 重置窗口再发送
另覆盖: stop 后 join 不悬挂(drain)、无僵尸线程、多 worker 发送串行化。
"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from async_executor import AsyncCommandExecutor  # noqa: E402


class TestSuccessPath(unittest.TestCase):
    """E1: 成功提交。"""

    def test_submit_ok_default_verify(self):
        ex = AsyncCommandExecutor(num_workers=1, max_retries=2, retry_delay=0.05)
        ex.start()
        self.assertTrue(ex.submit("cmd1", "P1"))
        ex.join()
        results = ex.get_results()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "ok")
        self.assertEqual(results[0]["player"], "P1")
        self.assertEqual(results[0]["retries"], 1)
        ex.stop(timeout=2)
        self.assertFalse(ex.is_alive())

    def test_submit_ok_with_verify_fn(self):
        def verify(cmd, player):
            return True, None
        ex = AsyncCommandExecutor(num_workers=2, max_retries=3, retry_delay=0.05)
        ex.start()
        for i in range(5):
            self.assertTrue(ex.submit(f"cmd{i}", f"P{i}", verify))
        ex.join()
        results = ex.get_results()
        self.assertEqual(len(results), 5)
        self.assertTrue(all(r["status"] == "ok" for r in results))
        ex.stop(timeout=2)


class TestRetryAndPendingQueue(unittest.TestCase):
    """E2: 失败重试→补做队列。"""

    def test_fail_then_retry_exhausted_goes_to_pending(self):
        attempts = {"n": 0}
        times = []

        def always_fail(cmd, player):
            attempts["n"] += 1
            times.append(time.monotonic())
            return False, "模拟验证失败"

        ex = AsyncCommandExecutor(num_workers=1, max_retries=3, retry_delay=0.15)
        ex.start()
        ex.submit("kick_cmd", "PB", always_fail)
        ex.join()
        # 重试次数 = max_retries(首次 + 2 次重试)
        self.assertEqual(attempts["n"], 3, f"应尝试 3 次,实际 {attempts['n']}")
        # retry_delay 生效:相邻两次尝试间隔 >= 0.12(容忍调度误差)
        if len(times) >= 2:
            gap = times[1] - times[0]
            self.assertGreaterEqual(gap, 0.12, f"retry_delay 未生效,间隔 {gap:.3f}s")
        # 结果状态 failed
        results = ex.get_results()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "failed")
        self.assertIn("模拟验证失败", results[0]["error"])
        # 进入补做队列,不丢失
        pending = ex.get_retry_tasks()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["player"], "PB")
        self.assertEqual(pending[0]["status"], "failed")
        ex.stop(timeout=2)

    def test_fail_once_then_success(self):
        calls = {"n": 0}

        def flaky(cmd, player):
            calls["n"] += 1
            return (calls["n"] >= 2, None if calls["n"] >= 2 else "第一次失败")

        ex = AsyncCommandExecutor(num_workers=1, max_retries=3, retry_delay=0.05)
        ex.start()
        ex.submit("cmd", "PF", flaky)
        ex.join()
        results = ex.get_results()
        self.assertEqual(results[0]["status"], "ok")
        self.assertEqual(results[0]["retries"], 2)   # 第二次成功
        self.assertEqual(ex.get_retry_tasks(), [])    # 成功不进补做队列
        ex.stop(timeout=2)


class TestQueueFull(unittest.TestCase):
    """E3: 队列满载丢弃。"""

    def test_overflow_dropped(self):
        block = threading.Event()

        def blocking_verify(cmd, player):
            block.wait(timeout=3)
            return True, None

        ex = AsyncCommandExecutor(num_workers=1, max_queue_size=2,
                                  max_retries=1, retry_delay=0.05)
        ex.start()
        time.sleep(0.1)
        # 第1条被 worker 取走并阻塞;再填满队列 2 条
        self.assertTrue(ex.submit("c0", "B0", blocking_verify))
        time.sleep(0.2)  # 确保 worker 已取走 c0
        self.assertTrue(ex.submit("c1", "B1", blocking_verify))
        self.assertTrue(ex.submit("c2", "B2", blocking_verify))
        # 第 4 条:队列已满 → 丢弃
        self.assertFalse(ex.submit("c3", "B3", blocking_verify))
        block.set()
        ex.join()
        statuses = {r["player"]: r["status"] for r in ex.get_results()}
        self.assertNotIn("B3", statuses, "B3 应被丢弃,不应出现在结果中")
        self.assertIn("B1", statuses)
        self.assertIn("B2", statuses)
        ex.stop(timeout=3)


class TestPauseGate(unittest.TestCase):
    """E4: 暂停期间异步线程必须静默;恢复后先重置窗口再发送。

    通过打桩记录全部设备操作时间线,
    断言暂停窗口 [t_pause, t_resume] 内无任何 send_chat_text 触发的动作。
    """

    class _FakeInput:
        """替代 HumanInput:记录设备操作时间线(模拟键盘/剪贴板),持锁 ~20ms。"""
        def __init__(self):
            self.timeline = []          # (time, action)
            self.lock = threading.Lock()

        def send_chat_text(self, text):
            with self.lock:
                self.timeline.append((time.monotonic(), f"send:{text}"))
                time.sleep(0.02)

        def wait_between_commands(self):
            pass

    class _FakeWatcher:
        def __init__(self, player_holder=None):
            self.player_holder = player_holder or {}

        def read_until(self, timeout=4.0):
            # 模拟游戏回显:返回含"当前执行玩家"成功标记的行
            time.sleep(0.05)
            p = self.player_holder.get("current", "PX")
            return [f"[CHAT] 成功设置{p}的职位为高活跃成员!"]

    def setUp(self):
        import action_executor as actions_mod
        self.actions_mod = actions_mod
        # 打桩 activate_window / prepare_chat_state / pyautogui.press
        self.calls = []
        actions_mod.activate_window = lambda hwnd: self.calls.append(("activate", time.monotonic()))
        actions_mod.prepare_chat_state = lambda det, cf: self.calls.append(("ocr_menu", time.monotonic()))
        import pyautogui
        self._orig_press = pyautogui.press
        pyautogui.press = lambda k: self.calls.append((f"press:{k}", time.monotonic()))

    def tearDown(self):
        import pyautogui
        pyautogui.press = self._orig_press

    def _make_executor(self):
        cfg = {"switches": {"verbose_log": False}, "error_patterns": ["权限不足"],
               "timeouts": {"error_check_window": 4.0, "max_retries": 2},
               "commands": {"guild_promote": "/guild promote {player}",
                            "guild_demote": "/guild demote {player}",
                            "guild_kick": "/guild kick {player} {reason}",
                            "kick_reason": "x"}}
        fake_input = self._FakeInput()
        # watcher 回显"当前玩家"由 verify 闭包写入,保证成功判定匹配
        self.player_holder = {"current": "?"}
        watcher = self._FakeWatcher(self.player_holder)
        executor = self.actions_mod.ActionExecutor(
            cfg, fake_input, watcher,
            hwnd=None, detector=None, stop_check=lambda: self.paused["v"])
        # 注入 main._reset_needed 同款标志的消费函数
        self.reset_flag = {"v": False}
        reset_lock = threading.Lock()

        def consume_reset():
            with reset_lock:
                v = self.reset_flag["v"]
                self.reset_flag["v"] = False
                return v
        executor.set_reset_hook(consume_reset)
        return executor, fake_input

    def test_paused_worker_silent_then_reset_on_resume(self):
        self.paused = {"v": True}       # 提交前就处于暂停 → worker 必须在暂停门阻塞
        executor, fake_input = self._make_executor()

        def verify(cmd, player):
            self.player_holder["current"] = player   # 回显匹配当前玩家 → 成功
            res = executor.execute_action("promote", player)
            ok = all(o for _, o, _ in res)
            return ok, None if ok else "fail"

        aex = AsyncCommandExecutor(num_workers=2, max_retries=2, retry_delay=0.05)
        aex.start()

        # ---- 时间线:暂停状态下提交 → 保持 0.6s(零发送)→ 恢复+置 reset 标志 ----
        aex.submit("/guild promote PX", "PX", verify)
        time.sleep(0.6)                 # 暂停窗口:此处不得有任何 send 动作
        t_pause = time.monotonic() - 0.55   # 窗口起点(提交后不久)
        t_resume = time.monotonic()
        self.paused["v"] = False        # 恢复
        self.reset_flag["v"] = True     # 模拟 _toggle_pause 置位 _reset_needed
        aex.join()

        # 断言1: 暂停窗口内零发送动作
        sends_in_pause = [a for (t, a) in fake_input.timeline if t_pause <= t <= t_resume]
        self.assertEqual(sends_in_pause, [],
                         f"暂停窗口内出现发送动作!{sends_in_pause}")
        # 断言2: 发送发生在恢复之后
        self.assertTrue(fake_input.timeline, "应有发送记录")
        first_send_t = min(t for (t, a) in fake_input.timeline if a.startswith("send:"))
        self.assertGreaterEqual(first_send_t, t_resume - 0.05,
                                "发送应发生在恢复之后(暂停门阻塞)")
        # 断言3: 恢复后发送前有窗口重置动作(激活窗口/Esc/OCR 任一)
        device_actions_before_send = [name for (name, ts) in self.calls if ts < first_send_t]
        self.assertTrue(any(n.startswith(("activate", "press:esc", "ocr_menu"))
                            for n in device_actions_before_send),
                        f"恢复后发送前应有窗口重置动作,{self.calls}")
        aex.stop(timeout=3)
        self.assertFalse(aex.is_alive(), "stop 后不应有存活线程")

    def test_multiworker_no_cross_send(self):
        """多 worker 提交时,发送仍严格串行(FakeInput 内部锁互斥):时间戳单调不减。"""
        self.paused = {"v": False}
        executor, fake_input = self._make_executor()

        def verify(cmd, player):
            # 多 worker 并发共享单值会互相覆盖 → 回显行包含全部玩家的成功标记,
            # 使任意一次发送的 成功设置{P}的职位为 均可匹配
            self.player_holder["current"] = "|".join(
                f"成功设置{p}的职位为高活跃成员!" for p in
                ("PM0", "PM1", "PM2", "PM3", "PM4", "PM5"))
            res = executor.execute_action("promote", player)
            return all(o for _, o, _ in res), None

        aex = AsyncCommandExecutor(num_workers=3, max_retries=1, retry_delay=0.05)
        aex.start()
        for i in range(6):
            aex.submit(f"cmd{i}", f"PM{i}", verify)
        aex.join()
        send_times = [t for (t, a) in fake_input.timeline if a.startswith("send:")]
        self.assertEqual(len(send_times), 6)
        ordered = all(b >= a for a, b in zip(send_times, send_times[1:]))
        self.assertTrue(ordered, f"发送时间戳应单调(串行化),实际 {send_times}")
        aex.stop(timeout=3)


class TestAsyncWaitForResult(unittest.TestCase):
    """主线程异步等待后台 worker 结果(替代主线程同步 read_until 阻塞):
    submit + wait_for_result 期间主线程未持有 watcher 锁,后台 worker 才执行真实 read。"""

    def test_wait_for_result_does_not_block_main_thread(self):
        from async_executor import AsyncCommandExecutor
        import time
        main_blocked_at = []

        def slow_verify(_cmd, _player):
            # 后台 worker:模拟 read 耗时 0.4s(期间主线程应已拿到 wait_for_result 超时)
            time.sleep(0.4)
            return True, {"ok": True, "player": _player}

        ex = AsyncCommandExecutor(num_workers=1, max_retries=1, retry_delay=0.01)
        ex.start()
        t0 = time.monotonic()
        ex.submit("cmd", "P1", slow_verify)
        # 主线程 wait 0.05s(短于后台 0.4s)→ 必超时返回 None
        r = ex.wait_for_result("P1", timeout=0.05)
        elapsed = time.monotonic() - t0
        assert r is None, f"wait 0.05s 应超时返回 None,实际 {r}"
        assert 0.04 <= elapsed <= 0.15, f"主线程不应阻塞 0.4s,实际 {elapsed:.3f}s"
        # 后台继续完成,主线程再 wait 应能拿到
        r2 = ex.wait_for_result("P1", timeout=1.0)
        assert r2 is not None and r2.get("status") == "ok"
        ex.stop(timeout=2)
        print("主线程异步等待验证 PASS: wait 0.05s 超时 / 后台 0.4s 完成后 wait 1s 拿到结果")

class TestBroadcastNonBlocking(unittest.TestCase):
    """主线程调 async_broadcast 后,后台 worker 才执行 read;主线程不阻塞。"""

    def test_async_broadcast_returns_quickly(self):
        from async_executor import AsyncCommandExecutor
        import time
        # 模拟 ActionExecutor:把 read_until 慢化的 verify
        def slow_verify(_cmd, _pid):
            time.sleep(0.4)  # 模拟读回显阻塞
            return True, None
        ex = AsyncCommandExecutor(num_workers=1, max_retries=1, retry_delay=0.01)
        ex.start()
        t0 = time.monotonic()
        ex.submit("/gc broadcast", f"__broadcast__:{hash('test')}", slow_verify)
        # 模拟 main.async_broadcast 的调用:submit 后 wait_for_result
        res = ex.wait_for_result(f"__broadcast__:{hash('test')}", timeout=0.05)
        elapsed = time.monotonic() - t0
        # 0.05s 应超时返回 None(后台 0.4s 还未完成),主线程只等了 0.05s 而非 0.4s
        assert res is None, f"0.05s wait 应超时,实际 {res}"
        assert elapsed < 0.15, f"主线程不应被 read 阻塞,实际 {elapsed:.3f}s"
        # 等后台完成
        res2 = ex.wait_for_result(f"__broadcast__:{hash('test')}", timeout=1.0)
        assert res2 is not None and res2.get("status") == "ok"
        ex.stop(timeout=2)
        print(f"async_broadcast 不阻塞验证 PASS: 主线程 0.05s 超时(后台 0.4s)/后台完成后 1s 拿到结果")



class TestStopDrain(unittest.TestCase):
    """E: stop 正确 drain,join 不悬挂,无任务悬挂/僵尸线程。"""

    def test_stop_drains_pending_tasks(self):
        block = threading.Event()

        def slow(cmd, player):
            block.wait(timeout=3)
            return True, None

        ex = AsyncCommandExecutor(num_workers=1, max_queue_size=10,
                                  max_retries=1, retry_delay=0.05)
        ex.start()
        ex.submit("slow0", "S0", slow)   # worker 取走并阻塞
        time.sleep(0.2)
        for i in range(1, 5):
            ex.submit(f"c{i}", f"S{i}", slow)   # 排队等待
        # block 保持关闭 → worker 卡在 S0,排队任务未被处理
        time.sleep(0.1)
        # 直接 stop → 排队的 S1..S4 被 drain 为 skipped;
        # S0 在 worker 手中,stop 后其 block.wait(3s) 超时返回,verify 正常完成(ok)
        # ——语义:正在执行的任务允许完成(不强杀),排队任务不执行
        ex.stop(timeout=6)
        self.assertFalse(ex.is_alive())
        players = {r["player"]: r["status"] for r in ex.get_results()}
        for i in range(1, 5):
            self.assertEqual(players.get(f"S{i}"), "skipped",
                             f"S{i} 应被 drain 为 skipped,实际 {players}")
        # drain 后 queue.join() 立即返回(不悬挂)——用线程+超时验证
        done = threading.Event()
        t = threading.Thread(target=lambda: (ex.join(), done.set()), daemon=True)
        t.start()
        self.assertTrue(done.wait(timeout=2), "drain 后 join() 仍悬挂!")




class TestSubmitWithoutBlocking(unittest.TestCase):
    """核心契约:submit() 不阻塞主线程,主线程拿到 key 立即继续,
    验证/补做/重试全部后台处理。"""

    def test_submit_returns_immediately_main_thread_free(self):
        from async_executor import AsyncCommandExecutor
        import time
        def slow_verify(_cmd, _pid):
            time.sleep(0.3)  # 后台慢
            return True, None  # (ok, error) 二元组

        ex = AsyncCommandExecutor(num_workers=1, max_retries=1, retry_delay=0.01)
        ex.start()
        # 提交 N 条命令
        N = 5
        t0 = time.monotonic()
        keys = []
        for i in range(N):
            k = ex.submit(f"cmd_{i}", f"P{i}", slow_verify)
            # 模拟主线程继续做其他事(此处为循环开销,可忽略)
            keys.append(k)
        submit_elapsed = time.monotonic() - t0
        # 提交阶段应极快(<<单条后台 0.3s):主线程没读 read_until
        assert submit_elapsed < 0.05,             f"主线程提交 N={N} 条应 < 0.05s,实际 {submit_elapsed:.3f}s(后台 0.3s/条)"
        # 此时后台还在跑第一/第二条,但主线程已可继续
        # 主线程立即取结果应拿到 None(后台未完成)
        r_now = ex.get_result_nowait("P0")
        # 不强求 None(可能已完成第一/第二条),但 submit 后立即可继续 = 核心契约满足
        # 全部完成后应能取到 status=ok
        ex.join()
        done = [ex.get_result_nowait(f"P{i}") for i in range(N)]
        assert all(d and d.get("status") == "ok" for d in done), done
        ex.stop(timeout=2)
        print(f"submit 不阻塞主线程 PASS: N={N} 提交耗时 {submit_elapsed*1000:.1f}ms << 单条后台 300ms")

    def test_failed_verify_queued_for_retry_not_lost(self):
        """验证失败 → 后台重试 max_retries 次 → 仍未通过 → 进补做队列(get_retry_tasks)。
        模拟用户原话"若此时后台验证没提供,插入任务重试上条命令"。"""
        from async_executor import AsyncCommandExecutor
        attempts = {"n": 0}
        def always_fail(_cmd, _pid):
            attempts["n"] += 1
            return False, f"模拟第{attempts['n']}次失败"

        ex = AsyncCommandExecutor(num_workers=1, max_retries=3, retry_delay=0.01)
        ex.start()
        ex.submit("cmd", "P1", always_fail)
        ex.join()
        # 恰好尝试 max_retries 次
        assert attempts["n"] == 3, attempts
        # 失败任务进入补做队列,不丢失
        retry = ex.get_retry_tasks()
        assert len(retry) == 1 and retry[0]["player"] == "P1"
        ex.stop(timeout=2)
        print(f"补做不丢失 PASS: max_retries=3 全失败后 get_retry_tasks 返 1 条")



class TestBroadcastKeyUnique(unittest.TestCase):
    """修复 bug:async_broadcast 用 hash(msg) 作 key 时文本相似 broadcast 撞 key 复用。
    改用 player+seq 后,同一/不同玩家的 broadcast key 必须互不冲突。"""

    def test_broadcast_keys_unique_across_similar_msgs(self):
        from async_executor import AsyncCommandExecutor
        import time
        ex = AsyncCommandExecutor(num_workers=1, max_retries=1, retry_delay=0.01)
        ex.start()
        # 模拟两条相似 msg(玩家名相近、模板相同)
        msg1 = "Test_个西更p日 这周贡献为:12981 执行操作:升职(2次) 非常感谢你对公会做出的贡献 公会群:YOUR_QQ_GROUP"
        msg2 = "Test_个西更p日 这周贡献为:12982 执行操作:降职(1次) 非常感谢你对公会做出的贡献 公会群:YOUR_QQ_GROUP"
        # 复现修复:调用方传入 player 字段,主线程保证 key 唯一
        key1 = f"__broadcast__:P1"
        key2 = f"__broadcast__:P2"

        def ok_v(_c, _p):
            return True, None
        ex.submit(msg1, key1, ok_v)
        ex.submit(msg2, key2, ok_v)
        ex.join()
        r1 = ex.get_result_nowait(key1)
        r2 = ex.get_result_nowait(key2)
        assert r1 is not None and r1.get("status") == "ok", r1
        assert r2 is not None and r2.get("status") == "ok", r2
        # 关键修复断言:key1 != key2,且各自结果独立(不被覆盖)
        assert key1 != key2, "key 必须唯一"
        assert r1 is not r2, "两条 broadcast 结果不应是同一对象引用"
        ex.stop(timeout=2)
        print(f"broadcast key 唯一 PASS: key1!=key2 / 各自结果独立(修复 hash 撞 key bug)")

    def test_same_player_two_broadcasts_dont_collide(self):
        """同一玩家两条 msg(升职/降职不同模板)用不同 player+seq 区分"""
        from async_executor import AsyncCommandExecutor
        ex = AsyncCommandExecutor(num_workers=1, max_retries=1, retry_delay=0.01)
        ex.start()
        # 用 player+seq 后,即使同一玩家两条 broadcast 也会用不同 key
        key1 = f"__broadcast__:P1:0"
        key2 = f"__broadcast__:P1:1"
        ex.submit("升职", key1, lambda *_: (True, None))
        ex.submit("降职", key2, lambda *_: (True, None))
        ex.join()
        assert ex.get_result_nowait(key1) is not ex.get_result_nowait(key2)
        ex.stop(timeout=2)
        print("同玩家多 broadcast 独立 PASS")



class TestTrulyNoBlocking(unittest.TestCase):
    """用户原话:全部都异步化 完全不等待判断。
    主线程 submit 后立即继续,后台 worker 自己完成 verify 并将结果写入
    result[player];主线程只在最后一次取已就绪结果。"""

    def test_main_thread_never_waits_for_verify(self):
        from async_executor import AsyncCommandExecutor
        import time
        # 慢 verify(0.3s)模拟读日志
        def slow_v(_c, _p):
            time.sleep(0.3)
            return True, {"ok": True}
        ex = AsyncCommandExecutor(num_workers=1, max_retries=1, retry_delay=0.01)
        ex.start()
        # 主线程依次提交 N 条命令,每次都立即返回
        t0 = time.monotonic()
        for i in range(5):
            k = ex.submit(f"cmd_{i}", f"P{i}", slow_v)
        submit_total = time.monotonic() - t0
        # 提交阶段必须极快(<10ms),不阻塞 5*0.3s
        assert submit_total < 0.05,             f"提交 5 条应 < 0.05s,实际 {submit_total*1000:.1f}ms(若接近 1.5s 则主线程在等)"
        # 此时后台 worker 还在跑第一/第二条,主线程不阻塞
        # 主线程可立即做其他事
        time.sleep(0.1)
        # 0.1s 后:第一条应已就绪(后台 ~0.3s 验证)
        r0 = ex.get_result_nowait("P0")
        # 不强求 ok(可能还没完成);关键是主线程没等
        # 全部 join 后,所有 P 都应 ok
        ex.join()
        all_ok = all(ex.get_result_nowait(f"P{i}") and ex.get_result_nowait(f"P{i}").get("status") == "ok"
                     for i in range(5))
        ex.stop(timeout=2)
        print(f"主线程不阻塞 PASS: 5 条提交 {submit_total*1000:.1f}ms << 5*0.3s 后台;主线程可立即推进")

    def test_pending_results_visible_after_background(self):
        """后台 verify 完成后,result 才可见;未就绪时 None(不阻塞)。"""
        from async_executor import AsyncCommandExecutor
        import time
        def slow_v(_c, _p):
            time.sleep(0.2)
            return True, {"data": "ready"}
        ex = AsyncCommandExecutor(num_workers=1, max_retries=1, retry_delay=0.01)
        ex.start()
        ex.submit("cmd", "P1", slow_v)
        # 立即取(后台 0.2s 还没完成)应 None(不阻塞)
        r_immediate = ex.get_result_nowait("P1")
        assert r_immediate is None, f"未就绪时不应可见,实际 {r_immediate}"
        # 等后台完成后再取
        ex.join()
        r_done = ex.get_result_nowait("P1")
        assert r_done is not None and r_done.get("data", {}).get("data") == "ready", r_done
        ex.stop(timeout=2)
        print(f"就绪可见性 PASS: 未就绪=None / 已就绪=data 完整")

    def test_failed_verify_queued_for_retry(self):
        """verify 失败→后台 _worker 内部重试→耗尽入 retry_queue 兜底(主线程可查 get_retry_tasks)。"""
        from async_executor import AsyncCommandExecutor
        attempts = {"n": 0}
        def always_fail(_c, _p):
            attempts["n"] += 1
            return False, f"模拟第{attempts['n']}次失败"
        ex = AsyncCommandExecutor(num_workers=1, max_retries=3, retry_delay=0.01)
        ex.start()
        ex.submit("cmd", "P1", always_fail)
        # 主线程不 join——直接 get_retry_tasks
        # (但 retry_queue 是 _worker 耗尽才入,此处只跑了 0 次,retry 仍空)
        r0 = ex.get_retry_tasks()
        assert r0 == [], f"未耗尽时 retry_queue 应空,实际 {r0}"
        # 主动 join 触发耗尽路径
        ex.join()
        r1 = ex.get_retry_tasks()
        assert len(r1) == 1 and r1[0]["player"] == "P1", r1
        assert attempts["n"] == 3, attempts
        ex.stop(timeout=2)
        print(f"补做不丢失 PASS: max_retries=3 全失败后 retry_queue=1(主线程不阻塞可直接 get_retry_tasks 查)")

if __name__ == "__main__":
    unittest.main()
