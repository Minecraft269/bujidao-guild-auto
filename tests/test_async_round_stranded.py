# -*- coding: utf-8 -*-
"""
test_async_round_stranded.py —— run_async_round 的"超时后在途任务"兜底
====================================================================
run_async_round 是异步模式的 B→D→E 闭环。这里锁定它最容易被忽略、
后果最严重的一条:

    wait_all 超时后仍有 SENT/PENDING 任务在途时,
    这些任务必须**保留在系统内并计入最终失败清单**,
    不能被归档抹掉。

为什么这条是红线:
    归档等于从系统里删除。任务被删掉后主线程再也取不到结果 ——
    没有补做、没有告警,用户看到的是"这条指令好像发过了但没生效",
    而这恰恰是最难复现、最难查的一类丢指令。

配套断言:
  * 正常的失败重试路径不受影响(仍然只返回真正验证失败的那些)
  * 最终失败清单按 (command, player) 去重 —— 重试会产生多个 task_id,
    业务事实只有一条
"""

import time
import unittest

from guild_auto.concurrency.async_system import AsyncVerifySystem, run_async_round


class TestStrandedTasksAreNotLost(unittest.TestCase):
    def test_stranded_task_appears_in_final_failed(self):
        """后台挂死 → wait_all 超时 → 在途任务必须出现在最终失败里。"""

        def hang(cmd, player, meta):
            time.sleep(30)
            return True, None, None

        s = AsyncVerifySystem(verify_fn=hang, poll_interval=0.02, task_timeout=2)
        try:
            ok, failed = run_async_round(
                s,
                [("/c1", "P1"), ("/c2", "P2")],
                send_fn=lambda c, p, m: None,
                max_retries=1,
            )
            self.assertEqual(ok, [], "挂死的任务不该被算成成功")
            players = {f["player"] for f in failed}
            self.assertEqual(players, {"P1", "P2"}, f"在途任务被抹掉了 —— 指令静默丢失: {failed}")
        finally:
            s.stop(timeout=1)

    def test_stranded_task_stays_inside_system(self):
        """任务不能只是"返回里有一条",系统内部也必须还留着它。"""

        def hang(cmd, player, meta):
            time.sleep(30)
            return True, None, None

        s = AsyncVerifySystem(verify_fn=hang, poll_interval=0.02, task_timeout=2)
        try:
            run_async_round(s, [("/c1", "P1")], send_fn=lambda c, p, m: None, max_retries=1)
            self.assertEqual(len(s.get_stranded()), 1, "在途任务被归档抹掉了,系统里已查不到")
        finally:
            s.stop(timeout=1)

    def test_stranded_does_not_duplicate_existing_failure(self):
        """同一条既在 final_failed 又在 stranded 里时,不得重复计入。

        去重键是 (command, player):重试会为同一业务指令生成多个 task_id,
        但业务事实只有一条。
        """

        def hang(cmd, player, meta):
            time.sleep(30)
            return True, None, None

        s = AsyncVerifySystem(verify_fn=hang, poll_interval=0.02, task_timeout=2)
        try:
            _ok, failed = run_async_round(
                s, [("/c1", "P1")], send_fn=lambda c, p, m: None, max_retries=1
            )
            keys = [(f["command"], f["player"]) for f in failed]
            self.assertEqual(len(keys), len(set(keys)), f"同一业务指令被重复计入失败: {keys}")
        finally:
            s.stop(timeout=1)


class TestNormalFailureStillWorks(unittest.TestCase):
    """对照组:没有超时的情况下,失败清单必须精确(不能因为兜底逻辑而虚增)。"""

    def test_only_genuinely_failed_are_returned(self):
        def flaky(cmd, player, meta):
            return (player == "P2"), (None if player == "P2" else "验证失败"), None

        s = AsyncVerifySystem(verify_fn=flaky, poll_interval=0.02, task_timeout=2)
        try:
            ok, failed = run_async_round(
                s,
                [("/c1", "P1"), ("/c2", "P2")],
                send_fn=lambda c, p, m: None,
                max_retries=1,
            )
            self.assertEqual([o["player"] for o in ok], ["P2"])
            self.assertEqual([f["player"] for f in failed], ["P1"])
            self.assertEqual(s.get_stranded(), [], "正常收尾时不该有在途任务")
        finally:
            s.stop(timeout=1)

    def test_success_results_deduped_by_business_key(self):
        """成功结果按 (command, player) 去重:重试成功不该被算成 2 次成功。"""

        def succeed(cmd, player, meta):
            return True, None, None

        s = AsyncVerifySystem(verify_fn=succeed, poll_interval=0.02, task_timeout=2)
        try:
            ok, failed = run_async_round(
                s, [("/c1", "P1")], send_fn=lambda c, p, m: None, max_retries=3
            )
            self.assertEqual(len(ok), 1, f"成功被重复计数: {ok}")
            self.assertEqual(failed, [])
        finally:
            s.stop(timeout=1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
