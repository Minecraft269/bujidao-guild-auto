# -*- coding: utf-8 -*-
"""
test_async_system.py —— 异步后台验证系统单元测试
=================================================
对应硬性验收标准第 5、6 条:异步模式主线程不等待验证,且重试闭环不死锁。

【核心语义(必须逐条断言,写错即为业务缺陷)】
  1. 主线程 submit/submit_many 立即返回,**不读日志、不解析、不验证**
  2. 主线程在**全部工作完成后**才 wait_all(),等后台完成信号
  3. 验证成功的 → 忽略并抛弃(get_completed)
  4. 验证失败的(没有找到/没有数据) → 记录结果等待(get_failed)→ 主线程决定重试
  5. E→D→B 重试闭环**不得死锁**
  6. 队列满**不丢任务**(异步模式下丢任务=丢业务数据)
  7. stop 时未完成任务进补做队列,不丢失

【防死锁的三道保险,逐条测】
  A. wait_all 带总超时,超时返回 False 而非无限阻塞
  B. 重试轮数由 max_retries 计数器约束,不由"等后台"驱动
  C. 完成信号判定用"没有 PENDING/SENT",不是"全部 OK"
     (若等"全部 OK",一个永远失败的任务会让每轮空等到超时 —— 实测 3 轮 12s)
"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logger  # noqa: E402
from async_system import (  # noqa: E402
    FAILED,
    OK,
    PENDING,
    SENT,
    STOPPED,
    AsyncVerifySystem,
    VerifyTask,
    run_async_round,
)

for _n in ("log_debug", "log_info", "log_warning", "log_error",
           "log_critical", "log_trace", "log_exception"):
    if not hasattr(logger, _n):
        setattr(logger, _n, lambda *a, **k: None)


def _ok(player=None, delay=0.0):
    """恒成功的 verify_fn。"""
    def _v(cmd, p, meta):
        if delay:
            time.sleep(delay)
        return True, None, {"player": p}
    return _v


def _always_fail(cmd, p, meta):
    return False, "没有找到数据", None


class TestVerifyTaskStateMachine(unittest.TestCase):
    """VerifyTask 状态机流转(纯内存,无线程)。"""

    def test_initial_state_pending(self):
        t = VerifyTask(1, "/guild member 甲", "甲")
        self.assertEqual(t.state, PENDING)
        self.assertEqual(t.attempts_count, 0)
        self.assertIsNone(t.ok)

    def test_mark_sent_increments_attempts(self):
        t = VerifyTask(1, "cmd", "甲")
        t.mark_sent()
        self.assertEqual(t.state, SENT)
        self.assertEqual(t.attempts_count, 1)

    def test_mark_sent_resets_previous_result(self):
        """重试时必须清空上一轮结果,否则读到陈旧 ok/error。"""
        t = VerifyTask(1, "cmd", "甲")
        t.mark_sent()
        t.mark_verified(False, error="第一次失败")
        t.mark_sent()          # 重试
        self.assertIsNone(t.error, "重试时上轮 error 未被清空")
        self.assertIsNone(t.ok)
        self.assertEqual(t.attempts_count, 2)

    def test_mark_verified_success(self):
        t = VerifyTask(1, "cmd", "甲")
        t.mark_verified(True, data={"weekly": 100})
        self.assertEqual(t.state, OK)
        self.assertTrue(t.ok)
        self.assertEqual(t.data, {"weekly": 100})

    def test_mark_verified_failure(self):
        t = VerifyTask(1, "cmd", "甲")
        t.mark_verified(False, error="没有找到")
        self.assertEqual(t.state, FAILED)
        self.assertFalse(t.ok)
        self.assertEqual(t.error, "没有找到")

    def test_mark_timeout_is_failure(self):
        t = VerifyTask(1, "cmd", "甲")
        t.mark_timeout("超时")
        self.assertEqual(t.state, FAILED)
        self.assertEqual(t.error, "超时")

    def test_mark_stopped_is_terminal(self):
        t = VerifyTask(1, "cmd", "甲")
        t.mark_stopped("stopped before send")
        self.assertEqual(t.state, STOPPED)
        self.assertTrue(t.is_done())

    def test_ok_is_done_failed_is_not(self):
        """关键语义:FAILED 不是终态 —— 它等主线程重试,不能算"完成"。"""
        t_ok = VerifyTask(1, "c", "甲"); t_ok.mark_verified(True)
        t_fail = VerifyTask(2, "c", "乙"); t_fail.mark_verified(False, error="x")
        self.assertTrue(t_ok.is_done())
        self.assertFalse(t_fail.is_done(), "FAILED 被误判为终态,会导致完成信号提前置位")

    def test_snapshot_is_isolated_copy(self):
        """快照必须是副本,改它不影响任务本身。"""
        t = VerifyTask(1, "cmd", "甲", meta={"k": "v"})
        snap = t.snapshot()
        snap["meta"]["k"] = "篡改"
        self.assertEqual(t.meta["k"], "v", "snapshot 暴露了内部 meta 引用")
        self.assertEqual(snap["command"], "cmd")

    def test_meta_defaults_to_empty_dict(self):
        t = VerifyTask(1, "cmd", "甲")
        self.assertEqual(t.meta, {})


class TestSubmitDoesNotBlock(unittest.TestCase):
    """验收第 5 条:异步模式下主线程不等待验证。"""

    def test_submit_returns_immediately_with_slow_verify(self):
        """verify 每条耗 300ms,提交 5 条主线程应在毫秒级返回。"""
        s = AsyncVerifySystem(verify_fn=_ok(delay=0.3), poll_interval=0.02, task_timeout=5)
        s.attach_sender(lambda c, p, m: None)
        s.start()
        try:
            t0 = time.time()
            s.submit_many([(f"/guild member P{i}", f"P{i}") for i in range(5)])
            submit_ms = (time.time() - t0) * 1000
            # 串行验证需 5*300=1500ms;主线程不应等这么久
            self.assertLess(submit_ms, 500,
                            f"主线程提交耗时 {submit_ms:.0f}ms,疑似等待了验证")
        finally:
            s.stop()

    def test_sender_thread_actually_sends(self):
        """后台发送线程必须真的把指令发出去(不只是记账)。"""
        sent = []
        s = AsyncVerifySystem(verify_fn=_ok(), poll_interval=0.02)
        s.attach_sender(lambda c, p, m: sent.append((c, p)))
        s.start()
        try:
            s.submit_many([(f"/guild member P{i}", f"P{i}") for i in range(5)])
            s.wait_all(timeout=10)
            self.assertEqual(len(sent), 5, "发送线程未发出全部指令")
        finally:
            s.stop()

    def test_send_exception_marks_failed(self):
        """发送函数抛异常 → 记为失败(而不是让后台线程崩溃)。"""
        def boom(c, p, m):
            raise RuntimeError("模拟按键失败")
        s = AsyncVerifySystem(verify_fn=_ok(), poll_interval=0.02)
        s.attach_sender(boom)
        s.start()
        try:
            s.submit("/guild member 甲", "甲")
            s.wait_all(timeout=5)
            self.assertEqual(len(s.get_failed()), 1, "发送异常未记为失败")
        finally:
            s.stop()

    def test_threads_alive_after_start(self):
        s = AsyncVerifySystem(verify_fn=_ok(), poll_interval=0.02)
        s.start()
        try:
            self.assertTrue(s.is_alive())
            self.assertEqual(len(s._threads), 2, "应有发送线程 + 验证线程")
        finally:
            s.stop()


class TestWaitAllCompletionSignal(unittest.TestCase):
    """完成信号 + 超时保险。"""

    def test_wait_all_returns_true_when_all_ok(self):
        s = AsyncVerifySystem(verify_fn=_ok(), poll_interval=0.02, task_timeout=5)
        s.attach_sender(lambda c, p, m: None)
        s.start()
        try:
            s.submit_many([(f"/c{i}", f"P{i}") for i in range(3)])
            self.assertTrue(s.wait_all(timeout=10))
            self.assertEqual(len(s.get_completed()), 3)
            self.assertEqual(len(s.get_failed()), 0)
        finally:
            s.stop()

    def test_wait_all_completes_even_with_failures(self):
        """关键防死锁:有失败任务时完成信号也必须置位。

        若完成条件误设为"全部 OK",这里就会空等到超时 —— 这是真实踩过的坑
        (实测 3 轮耗时 12s,修后 0.06s)。"""
        s = AsyncVerifySystem(verify_fn=_always_fail, poll_interval=0.02, task_timeout=3)
        s.attach_sender(lambda c, p, m: None)
        s.start()
        try:
            t0 = time.time()
            s.submit_many([(f"/c{i}", f"P{i}") for i in range(3)])
            got = s.wait_all(timeout=10)
            elapsed = time.time() - t0
            self.assertTrue(got, "有失败任务时未收到完成信号")
            self.assertLess(elapsed, 5, f"等待完成信号耗时 {elapsed:.1f}s,疑似空等到超时")
            self.assertEqual(len(s.get_failed()), 3)
        finally:
            s.stop()

    def test_wait_all_times_out_without_blocking_forever(self):
        """保险 A:后台卡死时 wait_all 必须按超时返回,不能永久阻塞。"""
        # verify 永久挂起(模拟后台卡死)
        def hang(cmd, p, meta):
            time.sleep(30)
            return True, None, None
        s = AsyncVerifySystem(verify_fn=hang, poll_interval=0.02, task_timeout=1)
        s.attach_sender(lambda c, p, m: None)
        s.start()
        try:
            s.submit("/c", "甲")
            t0 = time.time()
            got = s.wait_all(timeout=1.5)
            elapsed = time.time() - t0
            self.assertFalse(got, "后台卡死却报告完成")
            self.assertLess(elapsed, 4, f"wait_all 未按超时返回,耗时 {elapsed:.1f}s")
        finally:
            s.stop(timeout=1)

    def test_empty_task_set_does_not_kill_verifier(self):
        """关键:启动瞬间无任务,验证线程不得永久退出。

        这是真实踩过的 bug —— 空任务集被判定为"全部完成",线程 return,
        之后 submit 的任务永远无人验证。"""
        s = AsyncVerifySystem(verify_fn=_ok(), poll_interval=0.02, task_timeout=3)
        s.attach_sender(lambda c, p, m: None)
        s.start()
        try:
            time.sleep(0.3)      # 空跑一段时间,验证线程会经历"无任务"状态
            s.submit("/c", "迟到的任务")
            got = s.wait_all(timeout=5)
            self.assertTrue(got, "空跑后提交的任务未被验证(验证线程已提前退出)")
            self.assertEqual(len(s.get_completed()), 1)
        finally:
            s.stop()


class TestQueueNoTaskLoss(unittest.TestCase):
    """验收异常路径:队列满。"""

    def test_bounded_queue_does_not_drop_tasks(self):
        """队列有界时提交超量任务,**不得丢**(丢任务=丢业务数据)。"""
        s = AsyncVerifySystem(verify_fn=_ok(delay=0.05), max_queue_size=2, poll_interval=0.02)
        s.attach_sender(lambda c, p, m: None)
        s.start()
        try:
            ids = [s.submit(f"/c{i}", f"P{i}") for i in range(20)]
            self.assertEqual(len(ids), 20, "提交阶段就有任务被丢弃")
            s.wait_all(timeout=15)
            self.assertEqual(len(s.get_completed()), 20, "验证阶段有任务丢失")
        finally:
            s.stop()

    def test_get_task_returns_none_for_unknown_id(self):
        s = AsyncVerifySystem(verify_fn=_ok())
        self.assertIsNone(s.get_task(99999))


class TestStopPreservesUnfinished(unittest.TestCase):
    """验收异常路径:停止时不丢任务。"""

    def test_stop_moves_unfinished_to_retry_queue(self):
        """stop 时未验证完的任务必须进补做队列。"""
        def slow(cmd, p, meta):
            time.sleep(5)
            return True, None, None
        s = AsyncVerifySystem(verify_fn=slow, poll_interval=0.02, task_timeout=30)
        s.attach_sender(lambda c, p, m: None)
        s.start()
        s.submit_many([(f"/c{i}", f"P{i}") for i in range(3)])
        time.sleep(0.2)
        s.stop(timeout=2)
        self.assertEqual(len(s.get_retry_queue()), 3, "stop 后未完成任务未进补做队列(丢失)")
        for item in s.get_retry_queue():
            self.assertEqual(item["state"], STOPPED)

    def test_stop_drains_unsent_queue(self):
        """发送队列里还没取走的任务,stop 时也要标记 stopped 而非悬挂。"""
        def block(c, p, m):
            time.sleep(3)
        s = AsyncVerifySystem(verify_fn=_ok(), poll_interval=0.02)
        s.attach_sender(block)
        s.start()
        s.submit_many([(f"/c{i}", f"P{i}") for i in range(10)])
        time.sleep(0.1)
        s.stop(timeout=2)
        # 不应抛异常,且所有任务都有明确终态
        for snap in s.get_results():
            self.assertIn(snap["state"], (OK, FAILED, STOPPED),
                          f"任务 {snap['task_id']} 停留在 {snap['state']}")

    def test_stop_without_start_is_safe(self):
        """未 start 就 stop 不应抛异常。"""
        s = AsyncVerifySystem(verify_fn=_ok())
        s.stop()

    def test_start_is_idempotent(self):
        """重复 start 不得起两套线程。"""
        s = AsyncVerifySystem(verify_fn=_ok(), poll_interval=0.02)
        s.start()
        n = len(s._threads)
        s.start()
        try:
            self.assertEqual(len(s._threads), n, "重复 start 起了额外线程")
        finally:
            s.stop()


class TestResultFiltering(unittest.TestCase):
    """结果分流与过滤。"""

    def test_get_failed_with_task_ids_filters(self):
        """按 task_ids 过滤:多轮闭环时不得把上一轮残留算进来。"""
        s = AsyncVerifySystem(verify_fn=_always_fail, poll_interval=0.02)
        s.attach_sender(lambda c, p, m: None)
        s.start()
        try:
            first = s.submit("/c1", "甲")
            s.wait_all(timeout=5)
            second = s.submit("/c2", "乙")
            s.wait_all(timeout=5)
            self.assertEqual(len(s.get_failed()), 2)
            self.assertEqual(len(s.get_failed(task_ids=[first])), 1)
            self.assertEqual(s.get_failed(task_ids=[first])[0]["player"], "甲")
            self.assertEqual(len(s.get_failed(task_ids=[second])), 1)
            self.assertEqual(s.get_failed(task_ids=[second])[0]["player"], "乙")
        finally:
            s.stop()

    def test_get_completed_with_task_ids_filters(self):
        s = AsyncVerifySystem(verify_fn=_ok(), poll_interval=0.02)
        s.attach_sender(lambda c, p, m: None)
        s.start()
        try:
            a = s.submit("/c1", "甲")
            s.wait_all(timeout=5)
            b = s.submit("/c2", "乙")
            s.wait_all(timeout=5)
            self.assertEqual(len(s.get_completed()), 2)
            self.assertEqual(len(s.get_completed(task_ids=[a])), 1)
        finally:
            s.stop()

    def test_archive_finished_removes_tasks(self):
        """归档后任务从 _tasks 移除,防止下一轮重复计数。"""
        s = AsyncVerifySystem(verify_fn=_ok(), poll_interval=0.02)
        s.attach_sender(lambda c, p, m: None)
        s.start()
        try:
            tid = s.submit("/c", "甲")
            s.wait_all(timeout=5)
            self.assertEqual(len(s.get_results()), 1)
            self.assertEqual(s.archive_finished([tid]), 1)
            self.assertEqual(len(s.get_results()), 0)
        finally:
            s.stop()

    def test_clear_retry_queue(self):
        s = AsyncVerifySystem(verify_fn=_ok())
        s._retry_queue.append({"x": 1})
        s.clear_retry_queue()
        self.assertEqual(len(s.get_retry_queue()), 0)

    def test_pending_count_returns_to_zero(self):
        s = AsyncVerifySystem(verify_fn=_ok(), poll_interval=0.02)
        s.attach_sender(lambda c, p, m: None)
        s.start()
        try:
            s.submit_many([(f"/c{i}", f"P{i}") for i in range(4)])
            s.wait_all(timeout=10)
            self.assertEqual(s.pending_count(), 0, "完成后 pending 未归零")
        finally:
            s.stop()


class TestVerifyFnException(unittest.TestCase):
    """验收异常路径:验证函数自身抛异常。"""

    def test_verify_exception_marks_failed_not_crash(self):
        def boom(cmd, p, meta):
            raise ValueError("解析炸了")
        s = AsyncVerifySystem(verify_fn=boom, poll_interval=0.02, task_timeout=3)
        s.attach_sender(lambda c, p, m: None)
        s.start()
        try:
            s.submit("/c", "甲")
            s.wait_all(timeout=5)
            failed = s.get_failed()
            self.assertEqual(len(failed), 1, "验证异常未记为失败")
            self.assertIn("verify exception", failed[0]["error"])
        finally:
            s.stop()

    def test_none_verify_fn_marks_all_ok(self):
        """verify_fn=None(降级/Debug)→ 所有任务立即 OK,系统仍能跑完。"""
        s = AsyncVerifySystem(verify_fn=None, poll_interval=0.02, task_timeout=3)
        s.attach_sender(lambda c, p, m: None)
        s.start()
        try:
            s.submit_many([(f"/c{i}", f"P{i}") for i in range(3)])
            self.assertTrue(s.wait_all(timeout=5))
            self.assertEqual(len(s.get_completed()), 3)
        finally:
            s.stop()


class TestRunAsyncRoundRetryLoop(unittest.TestCase):
    """E→D→B 重试闭环(验收第 5 条 + 防死锁保险 B)。"""

    def test_success_first_round_no_retry(self):
        s = AsyncVerifySystem(verify_fn=_ok(), poll_interval=0.02, task_timeout=3)
        t0 = time.time()
        ok, fail = run_async_round(s, [("/c 甲", "甲")], lambda c, p, m: None, max_retries=3)
        elapsed = time.time() - t0
        s.stop()
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(fail), 0)
        self.assertLess(elapsed, 5, "成功路径不应耗时过长")

    def test_retry_until_success(self):
        """第 3 次才成功 → 应重试到成功,最终 fail 为空。"""
        calls = {}
        def flaky(cmd, p, meta):
            calls[p] = calls.get(p, 0) + 1
            good = calls[p] >= 3
            return good, None if good else "没有数据", None
        s = AsyncVerifySystem(verify_fn=flaky, poll_interval=0.02, task_timeout=3)
        ok, fail = run_async_round(s, [("/c 甲", "甲")], lambda c, p, m: None, max_retries=3)
        s.stop()
        self.assertEqual(len(ok), 1, "重试后成功应计入 ok")
        self.assertEqual(len(fail), 0, f"重试成功不应留在失败里:{[f['player'] for f in fail]}")
        self.assertEqual(calls["甲"], 3, "未重试到第 3 次")

    def test_retry_exhausted_returns_final_failed(self):
        """始终失败 → 重试到 max_retries 后进 final_failed,且不死锁。"""
        s = AsyncVerifySystem(verify_fn=_always_fail, poll_interval=0.02, task_timeout=2)
        t0 = time.time()
        ok, fail = run_async_round(s, [("/c 甲", "甲")], lambda c, p, m: None, max_retries=3)
        elapsed = time.time() - t0
        s.stop()
        self.assertEqual(len(ok), 0)
        self.assertEqual(len(fail), 1, f"应恰好 1 条最终失败,实际 {[f['player'] for f in fail]}")
        self.assertLess(elapsed, 5, f"重试闭环耗时 {elapsed:.1f}s,疑似死锁")

    def test_partial_success_partial_failure(self):
        """部分成功部分失败 → ok/fail 精确分流。"""
        def mix(cmd, p, meta):
            if p == "坏玩家":
                return False, "没有数据", None
            return True, None, None
        s = AsyncVerifySystem(verify_fn=mix, poll_interval=0.02, task_timeout=2)
        ok, fail = run_async_round(
            s, [("/c 好", "好玩家"), ("/c 坏", "坏玩家")], lambda c, p, m: None, max_retries=2)
        s.stop()
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(fail), 1)
        self.assertEqual(fail[0]["player"], "坏玩家")

    def test_multi_round_reuses_same_system(self):
        """同一 system 跑多轮 → 结果不重复计数(第 2 轮才成功的任务只算一次)。"""
        calls = {}
        def two_round(cmd, p, meta):
            calls[p] = calls.get(p, 0) + 1
            good = calls[p] >= 2
            return good, None if good else "等待", None
        s = AsyncVerifySystem(verify_fn=two_round, poll_interval=0.02, task_timeout=3)
        ok, fail = run_async_round(s, [("/c 甲", "甲")], lambda c, p, m: None, max_retries=3)
        s.stop()
        self.assertEqual(len(ok), 1, "多轮后成功数应去重为 1")
        self.assertEqual(len(fail), 0, f"多轮后不应残留失败:{[f['player'] for f in fail]}")

    def test_empty_command_list(self):
        """空输入 → 空结果,不抛异常。"""
        s = AsyncVerifySystem(verify_fn=_ok())
        ok, fail = run_async_round(s, [], lambda c, p, m: None, max_retries=3)
        s.stop()
        self.assertEqual((len(ok), len(fail)), (0, 0))

    def test_single_round_max_retries_one(self):
        """max_retries=1 → 只跑一轮,失败直接计入 final_failed。"""
        s = AsyncVerifySystem(verify_fn=_always_fail, poll_interval=0.02, task_timeout=2)
        ok, fail = run_async_round(s, [("/c 甲", "甲")], lambda c, p, m: None, max_retries=1)
        s.stop()
        self.assertEqual(len(ok), 0)
        self.assertEqual(len(fail), 1)

    def test_many_players_mixed(self):
        """多玩家混合:2 成功 2 失败。"""
        def mix(cmd, p, meta):
            return (p.startswith("好"), None if p.startswith("好") else "没有数据", None)
        cmds = [(f"/c {i}", i) for i in ["好1", "坏1", "好2", "坏2"]]
        s = AsyncVerifySystem(verify_fn=mix, poll_interval=0.02, task_timeout=2)
        ok, fail = run_async_round(s, cmds, lambda c, p, m: None, max_retries=2)
        s.stop()
        self.assertEqual(len(ok), 2, f"成功数不对:{[o['player'] for o in ok]}")
        self.assertEqual(len(fail), 2, f"失败数不对:{[f['player'] for f in fail]}")
        self.assertEqual({f["player"] for f in fail}, {"坏1", "坏2"})


if __name__ == "__main__":
    unittest.main(verbosity=2)