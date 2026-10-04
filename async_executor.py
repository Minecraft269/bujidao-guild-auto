# -*- coding: utf-8 -*-
"""
async_executor.py — 异步命令执行框架
======================================
为 guild_auto_v2 提供异步查询/执行验证能力。

核心思路(与验收清单 E 对应):
- 主线程快速提交全部任务,后台工作线程逐一消费执行(发送经 ActionExecutor._send_with_gate
  串行化:暂停门阻塞 + 恢复重置,不存在绕过暂停的发送路径);
- 验证失败在 max_retries 内自动重试(间隔 retry_delay),耗尽后进入补做队列
  (get_retry_tasks → main finally 写入 pending_actions.json,不丢失);
- 队列满载时丢弃新任务并告警(submit 返回 False,调用方可感知);
- stop() 先 drain 未处理任务(标记 skipped 并 task_done)再停线程,
  保证 join() 不因残留未完成任务而永久挂起;无僵尸线程(daemon + join)。

线程安全: queue.Queue / threading.Lock 保护结果与补做队列。
"""

import queue
import threading
import time

import logger

# 复用项目分级日志(logger 未初始化时 _log 为空操作)
_log = logger


class AsyncCommandExecutor:
    """异步命令执行器。

    使用方式:
        executor = AsyncCommandExecutor(num_workers=1)
        executor.start()
        executor.submit(command_str, player, verify_fn)
        ...
        executor.join()          # 等待队列排空并回收结果
        executor.stop(timeout=5) # 程序退出前停止线程池
    """

    def __init__(self, num_workers=1, max_queue_size=100, max_retries=3, retry_delay=1.0):
        self._queue = queue.Queue(maxsize=max_queue_size)
        self._results = []
        self._results_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._workers = []
        self._num_workers = max(1, int(num_workers))
        self._started = False
        # 验证重试配置
        self._max_retries = max(1, int(max_retries))
        self._retry_delay = float(retry_delay)
        self._retry_lock = threading.Lock()
        self._retry_queue = []  # 失败任务的补做队列(若缺失在插入任务)
        # 按 player 维度的"完成事件"+ 结果:后台 worker 完成任务后置位,
        # 主线程用 wait_for_result(player, timeout) 异步等待(不阻塞后台 worker)
        self._player_results = {}           # {player: result_dict}
        self._player_events = {}            # {player: threading.Event}
        self._player_results_lock = threading.Lock()

    def _get_player_event(self, player):
        """获取(或创建)指定 player 的完成事件——保证并发 submit 同一 player 复用同一事件。"""
        ev = self._player_events.get(player)
        if ev is None:
            with self._player_results_lock:
                ev = self._player_events.get(player)
                if ev is None:
                    ev = threading.Event()
                    self._player_events[player] = ev
        return ev

    def wait_for_result(self, player, timeout=None):
        """主线程按 player 异步等待后台任务完成。返回结果 dict 或 None(超时)。"""
        ev = self._get_player_event(player)
        # 即使已置位(任务早完成)wait 也立即返回 True
        ev.wait(timeout=timeout)
        with self._player_results_lock:
            return self._player_results.get(player)

    def get_result_nowait(self, player):
        """非阻塞获取已完成结果(主线程轮询用)。"""
        with self._player_results_lock:
            return self._player_results.get(player)

    # ---- 后台工作线程 ----
    def _worker(self):
        """后台工作线程:从队列获取命令并执行验证。
        verify_fn 由调用方提供(内部经 ActionExecutor._send_with_gate 发送:
        锁内暂停门→恢复重置→发送→读回显判定);失败重试至多 max_retries 次,
        耗尽进入补做队列。stop 置位后取出的任务标记 skipped 不再执行。"""
        while not self._stop_event.is_set():
            try:
                cmd, player, verify_fn = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if self._stop_event.is_set():
                # stop 已置位但任务刚取出:不再执行,标记 skipped 并退出
                with self._results_lock:
                    self._results.append({"command": cmd, "player": player,
                                          "status": "skipped", "error": "executor stopped"})
                self._queue.task_done()
                break

            # 阶段流水线:worker 内部仅 1 次 attempt,失败立即入 retry_queue;
            # 主线程在阶段切换前调 run_retry_queue() 统一重做,
            # max_retries 仅作为"单条任务最大尝试次数"(通常=1)防止无限重试。
            retries_left = max(1, self._max_retries)
            result = None
            error = None
            attempt = 0

            while retries_left > 0 and not self._stop_event.is_set():
                attempt += 1
                try:
                    _log.log_debug(f"(异步)开始验证 player={player} cmd={cmd[:60]} "
                                   f"attempt={attempt}/{max(1, self._max_retries)}")
                    if verify_fn is not None:
                        ok, error = verify_fn(cmd, player)
                    else:
                        ok, error = True, None
                    if ok:
                        result = {"command": cmd, "player": player,
                                  "status": "ok",
                                  "retries": attempt,
                                  "data": error}
                        _log.log_debug(f"(异步)验证成功 player={player} retries={result['retries']}")
                        break
                    _log.log_warning(f"(异步)验证失败 {player}: {error},剩余 attempt {retries_left - 1}")
                except Exception as e:
                    _log.log_error(f"(异步)验证异常 {player}: {e!r}")
                    error = repr(e)
                retries_left -= 1
                if retries_left > 0 and not self._stop_event.is_set():
                    time.sleep(self._retry_delay)

            if result is None:
                status = "skipped" if self._stop_event.is_set() else "failed"
                err_txt = str(error or ("executor stopped" if status == "skipped" else "重试耗尽"))
                result = {"command": cmd, "player": player, "status": status,
                          "error": err_txt, "retries": attempt}
                if status == "failed":
                    # 失败的任务入 retry_queue(主线程阶段切换前统一重做)
                    with self._retry_lock:
                        self._retry_queue.append(result)

            with self._results_lock:
                self._results.append(result)
            # 通知主线程:本 player 的结果已就绪
            with self._player_results_lock:
                self._player_results[player] = result
            self._get_player_event(player).set()
            self._queue.task_done()

    # ---- 生命周期 ----
    def start(self):
        """启动后台工作线程(幂等)。"""
        if self._started:
            return
        self._stop_event.clear()
        for i in range(self._num_workers):
            t = threading.Thread(target=self._worker, name=f"async-exec-{i}", daemon=True)
            t.start()
            self._workers.append(t)
        self._started = True
        _log.log_info(f"(异步)执行器已启动 workers={self._num_workers} "
                      f"max_queue={self._queue.maxsize} max_retries={self._max_retries}")

    def submit(self, command_str, player, verify_fn=None):
        """提交命令到异步队列(非阻塞)。返回 False 表示队列已满被丢弃(调用方可感知)。"""
        try:
            self._queue.put((command_str, player, verify_fn), block=False)
            return True
        except queue.Full:
            _log.log_warning(f"(异步)队列已满({self._queue.maxsize}),丢弃任务: {player} {command_str[:50]}")
            return False

    def get_results(self):
        """获取所有已完成的验证结果(线程安全,返回副本)。"""
        with self._results_lock:
            return list(self._results)

    def join(self, timeout_per_worker=2.0):
        """等待所有队列任务完成(阻塞直至排空)。

        _queue.join 等 queue 中所有 put 都被 task_done 配对;
        此外按短超时 join 各个 worker,确保 worker 当前 task 结束。
        (daemon worker join 必须带 timeout,否则 main 退出时可能死锁;
        此处带短超时仅作"催熟当前 task",verify 重试循环主循环驱动。)"""
        self._queue.join()
        for t in self._workers:
            t.join(timeout=timeout_per_worker)

    def stop(self, timeout=None):
        """停止所有工作线程:先 drain 队列中未处理的任务(task_done 归还),
        再置位停止事件并 join 各线程,避免任务悬挂与 join 计数错乱。"""
        self._drain_pending()
        self._stop_event.set()
        for t in self._workers:
            t.join(timeout=timeout)

    def _drain_pending(self):
        """把队列中尚未被 worker 取走的任务取出并按 skipped 记录+task_done,
        使后续 _queue.join() 不会永久挂起、任务不悬挂。"""
        drained = 0
        while True:
            try:
                cmd, player, _fn = self._queue.get_nowait()
            except queue.Empty:
                break
            with self._results_lock:
                self._results.append({"command": cmd, "player": player,
                                      "status": "skipped", "error": "drained on stop"})
            self._queue.task_done()
            drained += 1
        if drained:
            _log.log_warning(f"(异步)停止前排空 {drained} 条未处理任务(skipped)")

    def is_alive(self):
        """是否有存活的工作线程(用于退出时僵尸线程检查)。"""
        return any(t.is_alive() for t in self._workers)

    # ---- 补做队列 ----
    def run_retry_queue(self):
        """主线程在阶段切换前调用:把 retry_queue 中所有失败任务重新 submit(原 cmd+player+同 verify 闭包不可重建,
        仅重做携带 verify_fn 的;verify 必须是无副作用的,例如无读 read_until 残留状态)。
        本方法无法重建原 verify_fn,因此仅支持"重新提交已重试过的任务"语义:
        对每个 retry task,本次 submit 使用一个"已失败重做"标记闭包,
        主线程在阶段切换前自行调用 execute_actions 重做命令本身。
        更简单做法:主线程从 retry_queue 取 player,自己再次走同步 execute_actions,
        本方法只清空队列并返回需重做的 player 列表。"""
        with self._retry_lock:
            tasks = list(self._retry_queue)
            self._retry_queue.clear()
        return tasks

    def requeue(self, result):
        """主线程阶段切换前显式重入 retry_queue(替代清空):
        result = {"command", "player", "status", "error", ...} 同 _worker 写入格式。"""
        with self._retry_lock:
            self._retry_queue.append(result)

    def get_retry_tasks(self):
        """获取需要补做的失败任务列表(若缺失在插入任务)。返回副本。"""
        with self._retry_lock:
            return list(self._retry_queue)

    def clear_retry_tasks(self):
        """清空补做队列。"""
        with self._retry_lock:
            self._retry_queue.clear()
