# -*- coding: utf-8 -*-
"""
async_system.py —— 异步后台验证系统
==================================
本模块是【异步(后台)模式】的中枢,与 async_executor.py 的语义**完全相反**:

    async_executor.py (旧实现,保留供默认模式/兼容) : 主线程发一条 → 后台验一条 → 主线程 join 等结果
    async_system.py    (本模块,异步模式专用)      : 主线程**只发不验**,后台系统负责读日志+解析+验证

【核心语义(流程规格的硬性要求)】
  * 主线程 submit/submit_many 只负责"把指令投递出去",**不读日志、不解析、不验证**
  * 后台验证线程持续轮询,调用注入的 verify_fn 把每个响应块匹配回对应任务
  * 验证成功的 → 忽略并抛弃(主线程不关心)
  * 验证失败的(没有找到 / 没有数据) → 记录结果等待 → 主线程可取回并重试
  * 主线程只有在**全部工作完成后**才 wait_all(),等待后台返回完成信号
  * 主线程在发送过程中**绝不 join / 绝不等待验证**

【防死锁设计(规格明确要求"此处必须防死锁")】
  E→D→B 循环是"重试→再发一轮→再验证"。若后台验证线程与主线程互相等待对方就会死锁。
  本模块用三点保证不死锁:
    1. 后台验证线程**永不阻塞**在主线程上:循环以"单次 verify(带超时) + 无 SENT 任务时 sleep"驱动,
       超时即视为该任务未收到响应并记账继续走;
    2. 主线程只在**确认不再提交新任务**后才 wait_all(),而 wait_all() 自身带总超时,
       超时返回 False 而不是无限阻塞;
    3. 重试轮次由**计数器** max_retries 约束,不由"等后台说完了"驱动,
       因此即使后台卡死,主线程也会在有限轮次后退出循环而不是无限等下去。
"""

import queue
import threading
import time

import logger

# ---------------------------------------------------------------------------
# 任务状态机
# ---------------------------------------------------------------------------
# 状态流转:
#   PENDING --(发送线程发出)--> SENT --(后台验证成功)--> OK      (主线程抛弃)
#                                    --(后台验证失败)--> FAILED  (记录,等主线程重试)
#   FAILED --(主线程重试再发)--> SENT ...
#   任意   --(stop)--------> STOPPED (进补做队列,不丢失)
PENDING = "pending"
SENT = "sent"
OK = "ok"
FAILED = "failed"
STOPPED = "stopped"

# 终态:到这两态后主线程不再等待。FAILED **不是**终态——它等主线程重试,
# 所以此时不能置完成信号(否则主线程会误以为可以收工而漏掉重试)。
TERMINAL_STATES = (OK, STOPPED)


class VerifyTask:
    """一条"已发送、待验证"的指令任务。

    线程契约:
      * 主线程填 (task_id, command, player, meta) 并 submit(),**不碰验证结果**
      * 发送线程 mark_sent()
      * 后台验证线程 mark_verified() / mark_timeout()
      * 锁粒度只到单个任务,绝不跨任务持锁(避免死锁)
    """

    __slots__ = ("task_id", "command", "player", "meta", "_state", "attempts",
                 "ok", "error", "data", "_lock")

    def __init__(self, task_id, command, player, meta=None):
        self.task_id = task_id
        self.command = command
        self.player = player
        self.meta = meta or {}
        self._state = PENDING
        self.attempts = 0          # 已发送次数(每次重试 +1)
        self.ok = None             # None=未验证; True/False=后台判定
        self.error = None
        self.data = None           # 解析结果 dict(仅验证成功时)
        self._lock = threading.Lock()

    @property
    def state(self):
        with self._lock:
            return self._state

    @property
    def attempts_count(self):
        with self._lock:
            return self.attempts

    def mark_sent(self):
        """发送线程:已把指令发出去,等待后台验证。重试时清空上轮结果。"""
        with self._lock:
            self._state = SENT
            self.attempts += 1
            self.ok = None
            self.error = None

    def mark_verified(self, ok, error=None, data=None):
        """后台:写入验证结果。"""
        with self._lock:
            self.ok = bool(ok)
            self.error = error
            self.data = data
            self._state = OK if ok else FAILED

    def mark_timeout(self, reason):
        """后台:等待超时(没找到响应块)→ 记为失败。"""
        self.mark_verified(False, error=reason)

    def mark_stopped(self, reason="stopped"):
        with self._lock:
            self._state = STOPPED
            self.ok = False
            self.error = reason

    def snapshot(self):
        """线程安全快照(供主线程/报告读取)。"""
        with self._lock:
            return {
                "task_id": self.task_id,
                "command": self.command,
                "player": self.player,
                "meta": dict(self.meta),
                "state": self._state,
                "attempts": self.attempts,
                "ok": self.ok,
                "error": self.error,
                "data": self.data,
            }

    def is_done(self):
        """是否已到终态(OK 或 STOPPED)。FAILED 不算终态,它等主线程重试。"""
        with self._lock:
            return self._state in TERMINAL_STATES

    def __repr__(self):
        return (f"<VerifyTask id={self.task_id} player={self.player!r} "
                f"state={self.state} attempts={self.attempts_count}>")


class AsyncVerifySystem:
    """异步后台验证系统(主线程只发不验)。

    使用方式:
        system = AsyncVerifySystem(verify_fn=my_verify, task_timeout=15.0)
        system.attach_sender(send_fn)      # 注入实际发送函数
        system.start()
        # 主线程:只发不验
        system.submit_many([(cmd, player), ...])
        # ...主线程继续做别的事(发下一阶段指令/推进流程)...
        # 主线程全部工作完成后,才等待后台完成信号
        system.wait_all(timeout=120)
        ok  = system.get_completed()     # 成功的:主线程忽略并抛弃
        bad = system.get_failed()        # 失败的:记录结果等待 → 重试

    verify_fn(cmd, player, meta) -> (ok, error, data) 由调用方注入
        (通常是"读日志 → extract_chat_block → parse_guild_member"的实现)。
        传 None 表示不做验证(降级/Debug/单测用),此时所有任务立即置 OK。
    """

    def __init__(self, verify_fn=None, poll_interval=0.3, max_queue_size=0,
                 task_timeout=15.0, name="async-verify"):
        self._verify_fn = verify_fn
        self._poll = poll_interval
        self._task_timeout = float(task_timeout)
        self._name = name
        # 发送队列:主线程投递,发送线程消费。
        # maxsize=0(无界)——异步模式下队列满**不该丢任务**(丢任务=丢业务数据,
        # 规格要求验证失败的必须"记录结果等待",不能静默丢弃)。
        self._send_q = queue.Queue(maxsize=max_queue_size)
        # 全部已提交任务 task_id -> VerifyTask
        self._tasks = {}
        self._tasks_lock = threading.Lock()
        self._next_id = 0
        self._id_lock = threading.Lock()
        self._send_fn = None
        self._threads = []
        self._stop_event = threading.Event()
        self._started = False
        # 完成信号:后台全部任务到终态后置位,主线程 wait_all() 等它
        self._all_done = threading.Event()
        self._pending_count = 0
        self._count_lock = threading.Lock()
        # 补做队列:stop 时未到终态的任务快照(不丢失)
        self._retry_queue = []
        self._retry_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self):
        """启动发送线程 + 验证线程(幂等)。"""
        if self._started:
            return
        self._stop_event.clear()
        self._all_done.clear()
        t_send = threading.Thread(target=self._sender_loop,
                                  name=f"{self._name}-send", daemon=True)
        t_verify = threading.Thread(target=self._verifier_loop,
                                    name=f"{self._name}-verify", daemon=True)
        t_send.start()
        t_verify.start()
        self._threads = [t_send, t_verify]
        self._started = True
        logger.log_info(f"(异步系统)已启动 verify_fn={'注入' if self._verify_fn else 'None(不验证)'}"
                        f" task_timeout={self._task_timeout}s poll={self._poll}s")

    def stop(self, timeout=5.0):
        """停止后台系统:未到终态的任务进补做队列,置位事件,join 线程。

        先把队列里还没发出去的任务标 STOPPED,再把已提交但未验证的任务标 STOPPED,
        保证 wait_all() 不会因残留任务永久挂起、任务不丢失。"""
        self._stop_event.set()
        stopped = 0
        # 1) 排空发送队列中未取走的任务
        while True:
            try:
                task_id = self._send_q.get_nowait()
            except queue.Empty:
                break
            with self._tasks_lock:
                task = self._tasks.get(task_id)
            if task is not None and not task.is_done():
                task.mark_stopped("stopped before send")
                stopped += 1
            self._send_q.task_done()
        # 2) 已提交但未验证的任务 → 补做队列
        with self._tasks_lock:
            unfinished = [t for t in self._tasks.values() if not t.is_done()]
        for t in unfinished:
            t.mark_stopped("stopped before verify")
            with self._retry_lock:
                self._retry_queue.append(t.snapshot())
        stopped += len(unfinished)
        with self._count_lock:
            self._pending_count = 0
        self._all_done.set()
        for t in self._threads:
            t.join(timeout=timeout)
        if stopped:
            logger.log_warning(f"(异步系统)停止前把 {stopped} 条未完成任务标记为 stopped 并入补做队列")
        logger.log_info("(异步系统)已停止")

    def is_alive(self):
        return any(t.is_alive() for t in self._threads)

    # ------------------------------------------------------------------
    # 主线程 API:只发不验
    # ------------------------------------------------------------------
    def submit(self, command, player, meta=None):
        """主线程投递一条指令。**立即返回,不等验证**(异步模式核心)。返回 task_id。"""
        with self._id_lock:
            self._next_id += 1
            task_id = self._next_id
        task = VerifyTask(task_id, command, player, meta)
        with self._tasks_lock:
            self._tasks[task_id] = task
        with self._count_lock:
            self._pending_count += 1
        self._all_done.clear()
        self._send_q.put(task_id)   # 主线程唯一动作;实际按键在 sender 线程
        logger.log_trace(f"TRACE_QUEUE async_system.submit | task_id={task_id} "
                         f"player={player!r} depth={self._send_q.qsize()}")
        return task_id

    def submit_many(self, commands):
        """批量投递。commands: [(cmd, player)] 或 [(cmd, player, meta)]。
        投递完立刻返回,主线程随后可继续推进,完全不等验证。"""
        ids = []
        for item in commands:
            cmd, player = item[0], item[1]
            meta = item[2] if len(item) > 2 else None
            ids.append(self.submit(cmd, player, meta))
        logger.log_info(f"(异步系统)批量投递 {len(ids)} 条指令(主线程不等验证)")
        return ids

    # ------------------------------------------------------------------
    # 主线程 API:取结果 / 等完成
    # ------------------------------------------------------------------
    def get_task(self, task_id):
        with self._tasks_lock:
            return self._tasks.get(task_id)

    def get_results(self):
        with self._tasks_lock:
            return [t.snapshot() for t in self._tasks.values()]

    def get_failed(self, task_ids=None):
        """验证失败(没有找到/没有数据)的任务快照 —— 主线程据此决定重试。
        这是规格里"验证失败的:记录结果等待"的落点。

        task_ids 给了就只返回这些任务的结果(多轮闭环时避免把上一轮残留算进来)。"""
        ids = None if task_ids is None else set(task_ids)
        return [s for s in self.get_results()
                if s["state"] == FAILED and (ids is None or s["task_id"] in ids)]

    def get_completed(self, task_ids=None):
        """验证成功的任务快照(主线程"忽略并抛弃"的对象)。

        task_ids 给了就只返回这些任务的结果 —— 多轮闭环(规格 E→D→B)时,
        上一轮的 FAILED 任务仍留在 _tasks 里,不按轮次过滤会重复计数。
        """
        ids = None if task_ids is None else set(task_ids)
        return [s for s in self.get_results()
                if s["state"] == OK and (ids is None or s["task_id"] in ids)]

    def get_retry_queue(self):
        with self._retry_lock:
            return list(self._retry_queue)

    def archive_finished(self, task_ids):
        """把指定任务从 _tasks 中移出(结果已被主线程取走)。

        用于闭环收尾:避免下一轮把上一轮的残留结果再算一遍。返回移出的数量。"""
        ids = set(task_ids)
        with self._tasks_lock:
            victims = [tid for tid in ids if tid in self._tasks]
            for tid in victims:
                del self._tasks[tid]
        if victims:
            logger.log_trace(f"TRACE_QUEUE archive_finished | 移出 {len(victims)} 条已完成任务")
            # 归档后若已无在途任务,主动置完成信号,让此刻正在 wait_all() 的
            # 主线程立即返回,不必空等到超时(否则多轮闭环每轮都多等一个 timeout)。
            with self._tasks_lock:
                idle = not self._tasks and self._send_q.empty()
            if idle:
                self._all_done.set()
        return len(victims)

    def clear_retry_queue(self):
        with self._retry_lock:
            self._retry_queue.clear()

    def pending_count(self):
        with self._count_lock:
            return self._pending_count

    def wait_all(self, timeout=None):
        """主线程**全部工作完成后**才调用:等待后台返回完成信号。

        带总超时(默认 task_timeout*2);超时返回 False 表示后台未全部完成,
        但绝不无限阻塞——这是防死锁的第 2 道保险。"""
        if timeout is None:
            timeout = self._task_timeout * 2
        got = self._all_done.wait(timeout=timeout)
        if got:
            logger.log_info("(异步系统)收到后台完成信号,全部任务已到终态")
        else:
            logger.log_warning(f"(异步系统)等待完成信号超时({timeout:.1f}s),"
                               f"仍有 {self.pending_count()} 条未验证")
        return got

    # ------------------------------------------------------------------
    # 后台:发送线程(只发不验)
    # ------------------------------------------------------------------
    def _sender_loop(self):
        """后台发送线程:取任务 → 调注入的 send_fn 发送 → mark_sent → 立刻取下一条。
        本线程**不验证**;验证完全交给 _verifier_loop。"""
        while not self._stop_event.is_set():
            try:
                task_id = self._send_q.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                with self._tasks_lock:
                    task = self._tasks.get(task_id)
                if task is not None:
                    self._send_one(task)
            except Exception as e:  # 发送线程异常不能拖垮系统
                logger.log_error(f"(异步系统)发送线程异常: {e!r}")
            finally:
                self._send_q.task_done()

    def _send_one(self, task):
        """发送单条指令并标记已发送。"""
        send_fn = self._send_fn
        if send_fn is None:
            # 未注入发送函数(如单测):只记账标记,不真发
            task.mark_sent()
            return
        task.mark_sent()
        logger.log_trace(f"TRACE_VERIFY send | task_id={task.task_id} "
                         f"player={task.player!r} cmd={task.command[:60]!r}")
        try:
            send_fn(task.command, task.player, task.meta)
        except Exception as e:  # 发送异常 → 记失败,由主线程决定重试
            logger.log_error(f"(异步系统)发送异常 player={task.player}: {e!r}")
            task.mark_verified(False, error=f"send failed: {e!r}")

    def attach_sender(self, send_fn):
        """注入实际发送函数 send_fn(cmd, player, meta)。"""
        self._send_fn = send_fn

    # ------------------------------------------------------------------
    # 后台:验证线程(读日志 + 解析 + 判定)
    # ------------------------------------------------------------------
    def _verifier_loop(self):
        """后台验证线程:对每个 SENT 任务调 verify_fn 验证。

        防死锁要点:
          * 单次 verify 由注入方控制超时(内部轮询日志),本循环不额外阻塞
          * 没有 SENT 任务时 sleep(poll) 让出 CPU,不空转
          * 全部任务到终态 → 置完成信号并退出循环(线程自然结束)
        """
        while not self._stop_event.is_set():
            with self._tasks_lock:
                candidates = [t for t in self._tasks.values() if t.state == SENT]
            if not candidates:
                if self._all_tasks_terminal():
                    # 本轮全部到终态 → 置完成信号让主线程 wait_all() 返回。
                    # **不能 return**:规格的 E→D→B 重试循环会在同一系统上再次
                    # submit_many(新一轮),线程必须能继续工作。
                    self._all_done.set()
                    time.sleep(self._poll)
                    continue
                time.sleep(self._poll)
                continue
            for task in candidates:
                if self._stop_event.is_set():
                    break
                if task.state != SENT:   # 已被其他路径改状态(如发送失败)则跳过
                    continue
                logger.log_trace(f"TRACE_VERIFY begin | task_id={task.task_id} "
                                  f"player={task.player!r} attempt={task.attempts_count}")
                try:
                    if self._verify_fn is None:
                        # 无验证函数(降级/Debug/单测):立即置 OK
                        task.mark_verified(True, data=None)
                    else:
                        ok, error, data = self._verify_fn(task.command, task.player, task.meta)
                        task.mark_verified(ok, error, data)
                except Exception as e:
                    task.mark_verified(False, error=f"verify exception: {e!r}")
                    logger.log_error(f"(异步系统)验证异常 player={task.player}: {e!r}")
                with self._count_lock:
                    if self._pending_count > 0:
                        self._pending_count -= 1
                if task.state == OK:
                    logger.log_trace(f"TRACE_VERIFY ok | task_id={task.task_id} "
                                     f"player={task.player!r}")
                else:
                    logger.log_warning(f"(异步系统)验证失败 player={task.player}: {task.error}")
        # stop 时兜底置完成信号,避免主线程 wait_all 挂起
        self._all_done.set()

    def _all_tasks_terminal(self):
        """本轮是否已全部"验证完毕"。

        语义:任务处于 OK / FAILED / STOPPED 都算验证完毕 —— 只有 SENT(已发出但
        后台还没验它)和 PENDING(还没发)才算未完。FAILED 是**已完成的验证结果**
        (没找到/没数据),主线程据此决定重试,它不属于"还在等"。

        因此这里用"没有待验证/待发送的任务"作为完成条件,而不是"全部 OK"——
        否则一个永远失败的任务会让 wait_all() 每次都空等到超时(实测 3 轮耗时 12s)。

        另外:空任务集返回 True 会让验证线程在"启动瞬间尚无任务"时直接退出,
        导致之后 submit 的任务永远无人验证。因此必须同时要求**已提交过任务**。
        """
        with self._tasks_lock:
            if not self._tasks:
                return False          # 一个任务都没有 → 还不能判定"本轮完成"
            return not any(t.state in (PENDING, SENT) for t in self._tasks.values()) \
                and self._send_q.empty()


def run_async_round(system, commands, send_fn, max_retries=3):
    """异步模式的"一轮发送+验证+重试"闭环(对应规格 B→D→E→D→B)。

    规格语义:
      B: 主线程逐条发送(只发不验)→ 全部发送完毕后等待 → D
      D: 后台读日志并验证 → 成功的抛弃,失败的记录等待 → E
      E: 若有失败且需重试则重试(仅发一次,后台继续验证)→ 回到 B

    返回 (成功结果列表, 最终仍失败的任务列表)。

    防死锁保证:
      * 重试轮数由 max_retries 计数器约束,不由"等后台"驱动
      * 每轮 wait_all 都带超时,超时后进入下一轮或收尾,绝不无限等
      * 重试耗尽的任务直接进 final_failed 返回,**不再重试**(避免 E→D→B 死循环)
    """
    results_ok = []
    final_failed = []
    remaining = list(commands)
    max_rounds = max(1, int(max_retries))
    all_round_ids = set()   # 本次闭环提交过的全部 task_id(用于收尾时只统计本次的任务)

    for round_no in range(1, max_rounds + 1):
        if not remaining:
            break
        logger.log_info(f"(异步系统)第 {round_no}/{max_rounds} 轮:投递 {len(remaining)} 条指令(只发不验)")
        system.attach_sender(send_fn)
        system.start()   # 幂等
        # 本轮 task_id 集合:用于只统计本轮结果,避免多轮共用 system 时重复计数
        round_ids = set(system.submit_many(remaining))
        all_round_ids |= round_ids

        # 主线程在"全部发送完毕后"才等待后台完成信号
        system.wait_all(timeout=None)

        # 本轮结果分流:成功的抛弃,失败的记录
        results_ok.extend(system.get_completed(task_ids=round_ids))

        failed = system.get_failed(task_ids=round_ids)
        if not failed:
            remaining = []
            break

        # 还有轮次可重试 → 全部转入下一轮(本轮 FAILED 不算"最终失败",
        # 它只是"这条要重试"的信号);最后一轮才把未通过的计入 final_failed。
        if round_no < max_rounds:
            remaining = [(s["command"], s["player"]) for s in failed]
            logger.log_warning(f"(异步系统)第 {round_no} 轮有 {len(remaining)} 条失败需重试")
        else:
            final_failed.extend(failed)
            remaining = []
            logger.log_warning(f"(异步系统)第 {round_no} 轮(末轮)仍有 {len(failed)} 条失败,重试耗尽")

    # 收尾兜底:本闭环内仍处于 FAILED(理论上末轮已全部收进 final_failed,
    # 这里防的是边界情况如 max_rounds=1)。按 task_id 去重,同一条失败不重复计入。
    seen_ids = {s["task_id"] for s in final_failed}
    for s in system.get_failed(task_ids=all_round_ids):
        if s["task_id"] not in seen_ids:
            final_failed.append(s)
            seen_ids.add(s["task_id"])

    # 归档本闭环全部任务:结果已被主线程取走,移出 _tasks 防止下一轮重复计数,
    # 同时让 _all_tasks_terminal() 能正确判定"本 system 暂无在途任务"。
    system.archive_finished(all_round_ids)

    # 收尾:某条指令若在后续轮次验证成功过,就不该同时留在"最终失败"里 ——
    # 重试会为同一 (command, player) 产生多个 task_id,前几轮的 FAILED 只是
    # "要重试"的信号。业务事实只有一个:这条指令最终过没过。
    ok_keys = {(s["command"], s["player"]) for s in results_ok}
    dedup_fail, seen_fail = [], set()
    for s in final_failed:
        key = (s["command"], s["player"])
        if key in ok_keys:          # 后续轮次成功了 → 不算最终失败
            continue
        if key in seen_fail:        # 同一条失败不重复计入
            continue
        seen_fail.add(key)
        dedup_fail.append(s)
    final_failed = dedup_fail

    # 成功结果同样按业务键去重:让"成功数"等于"成功的指令数"而非"成功的尝试数"。
    deduped, seen_ok = [], set()
    for s in results_ok:
        key = (s["command"], s["player"])
        if key in seen_ok:
            continue
        seen_ok.add(key)
        deduped.append(s)
    results_ok = deduped

    logger.log_info(f"(异步系统)闭环结束:成功 {len(results_ok)} 条,最终失败 {len(final_failed)} 条")
    return results_ok, final_failed