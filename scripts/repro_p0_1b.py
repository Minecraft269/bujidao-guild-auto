# -*- coding: utf-8 -*-
"""P0-1b 最小复现/验收脚本 —— 跨轮续做重放已成功动作（R-09/R-10/R-11/R-12/R-13/R-15）

直接跑:
    python scripts/repro_p0_1b.py

每条独立断言,末尾打印 PASS/FAIL 汇总,任一失败退出码为 1。
反转验证:把工作区切到修复前的 main(4ec7365),本脚本会因为
`pending_items_from_records` / `merge_pending` / `split_async_tasks` /
`pending_items_from_async_system` / `gate_on_failures` 不存在而在对应 R-* 上
打印 FAIL(R-11 的"续做不二次确认就真踢"在旧代码上同样是 FAIL)。
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from guild_auto.application import pending as P  # noqa: E402

RESULTS = []


def check(name, fn):
    try:
        detail = fn()
        RESULTS.append((name, True, detail or ""))
    except AssertionError as e:
        RESULTS.append((name, False, str(e)))
    except Exception as e:  # 缺函数/导入失败 = 修复前的旧代码
        RESULTS.append((name, False, f"{type(e).__name__}: {e}"))


class _FakeRt:
    """最小 runtime:只提供 gate_on_failures 需要的 confirm。"""

    def __init__(self, answer):
        self.answer = answer

    def confirm(self, prompt, default=False):
        return self.answer


class _FakeSystem:
    """记录调用顺序的假 async_system,并模拟 stop() 才把任务推进补做队列。"""

    def __init__(self, tasks):
        self._tasks = tasks
        self._queue = []
        self.calls = []

    def stop(self, timeout=5.0):
        self.calls.append("stop")
        self._queue = list(self._tasks)

    def get_retry_queue(self):
        self.calls.append("get_retry_queue")
        return self._queue

    def get_stranded(self):
        self.calls.append("get_stranded")
        return []


class _Ex:
    """假执行器:记录真正发出去的动作,全部成功。"""

    hwnd = None
    detector = None

    def __init__(self):
        self.sent = []

    def execute_actions(self, player, actions):
        self.sent.extend(actions)
        return [(a, True, None) for a in actions]


def _task(player, action):
    return {"player": player, "command": f"/guild {action} {player}", "meta": {"action": action}}


def _cfg(**sw):
    return {
        "guild_name": "演示公会",
        "switches": {"execution_enabled": True, **sw},
        "kick_confirm": True,
        "kick_confirm_text": "kick-confirm",
    }


# ---------------------------------------------------------------- R-09
def r09_pending_only_unfinished_suffix():
    rec = {
        "player": "甲",
        "rank": "entry",
        "actions": ["promote", "promote"],  # 两条真实升职指令
        "executed": True,
        "error": "执行失败(promote): 超时",
        "remaining_actions": ["promote"],  # 只有第二条没做成
    }
    items = P.pending_items_from_records([rec])
    assert len(items) == 1, items
    assert items[0]["actions"] == ["promote"], f"待办含已成功动作: {items[0]['actions']}"
    # 已全部成功的记录不得进待办
    done = dict(rec, error=None, remaining_actions=[])
    assert P.pending_items_from_records([done]) == []
    return "actions=['promote'](已成功那条未落盘)"


# ---------------------------------------------------------------- R-10
def r10_dedupe_same_player_same_actions():
    old = [{"player": "甲", "actions": ["kick"], "error": "旧"}]
    new = [{"player": "甲", "actions": ["kick"], "error": "新"}]
    merged = P.merge_pending(old, new)
    assert len(merged) == 1, f"同一玩家同一动作被登记 {len(merged)} 次"
    assert merged[0]["error"] == "新"
    # promote×2 是两次真实操作,不得被合并
    two = [
        {"player": "甲", "actions": ["promote"]},
        {"player": "甲", "actions": ["promote", "promote"]},
    ]
    assert len(P.merge_pending([], two)) == 2
    return "2 条同键 → 1 条;promote 与 promote×2 未被误并"


def _run_pending_compat(path, ex, kick_text):
    """跑一次续做;旧代码没有 ask_text_fn 时退回"只问一次"的老契约。

    这样反转验证拿到的是**行为**差异(旧码直接真踢),而不是 ImportError。
    """
    common = dict(console_fn=lambda *a, **k: None, input_fn=lambda *a, **k: "y")
    try:
        return P.run_pending(
            _cfg(), None, None, None, ex, path, ask_text_fn=lambda *a, **k: kick_text, **common
        )
    except TypeError:
        return P.run_pending(_cfg(), None, None, None, ex, path, **common)


# ---------------------------------------------------------------- R-11
def _run_resume(kick_text):
    """跑一次续做,返回 (实际发送的动作列表, 剩余待办, 待办文件内容)。"""
    tmp = tempfile.mkdtemp(prefix="repro_p0_1b_")
    path = os.path.join(tmp, "pending_actions.json")
    P.save_pending(path, "演示公会", [{"player": "甲", "rank": "entry", "actions": ["kick"]}])
    ex = _Ex()
    cont, rem = _run_pending_compat(path, ex, kick_text)
    return ex.sent, rem, P.load_pending(path)


def r11_resume_kick_needs_second_confirm():
    sent_bad, rem_bad, left_bad = _run_resume("y")  # 习惯性敲 y,不是确认词
    assert sent_bad == [], f"未二次确认就发了踢出: {sent_bad}"
    assert rem_bad == [] and left_bad == [], "用户已跳过踢出,却仍留在待办(下次会补踢)"
    sent_ok, rem_ok, _ = _run_resume("kick-confirm")
    assert sent_ok == ["kick"], f"逐字确认后仍未执行: {sent_ok}"
    assert rem_ok == []
    return "错误确认词 → 发送 0 次且不留待办;正确确认词 → 发送 1 次"


# ---------------------------------------------------------------- R-12
def r12_gc_task_not_written_as_actions():
    resumable, unresumable = P.split_async_tasks(
        [
            _task("甲", "kick"),
            {"player": "甲", "command": "/gc 本周处理完毕", "meta": {"msg": "本周处理完毕"}},
        ]
    )
    assert [t["player"] for t in resumable] == ["甲"], resumable
    assert len(unresumable) == 1, "/gc 广播被当成可续做的动作"
    items, bad = P.pending_items_from_async_system(
        _FakeSystem([_task("甲", "kick"), {"player": "乙", "command": "/gc x", "meta": {}}])
    )
    assert items and items[0]["actions"] == ["kick"], items
    assert bad == 1, f"不可续做任务未被单独计数: {bad}"
    return "kick → 可续做; /gc 广播 → 不可续做(单独计数,不写坏条目)"


# ---------------------------------------------------------------- R-13
def r13_stop_before_retry_queue():
    sys_ = _FakeSystem([_task("甲", "kick")])
    items, bad = P.pending_items_from_async_system(sys_)
    assert sys_.calls[0] == "stop", f"调用顺序错误: {sys_.calls}"
    assert sys_.calls.index("stop") < sys_.calls.index("get_retry_queue"), sys_.calls
    assert len(items) == 1 and items[0]["actions"] == ["kick"], items
    assert bad == 0
    return f"调用顺序={sys_.calls}, 取到 {len(items)} 条补做任务"


# ---------------------------------------------------------------- R-15
def r15_failure_gate_blocks_by_default():
    import guild_auto.main as M

    # 用户否决(或回车/EOF,默认 False)→ 必须中止
    assert M.gate_on_failures(_FakeRt(False), "通知失败", 3, "通知有失败") is True
    # 用户明确确认继续 → 不中止(但会留 ERROR 级痕迹)
    assert M.gate_on_failures(_FakeRt(True), "通知失败", 3, "通知有失败") is False
    # 没有失败 → 不问、不中止
    assert M.gate_on_failures(_FakeRt(False), "通知失败", 0, "通知有失败") is False
    return "否决/EOF → 中止; 无失败 → 不打扰"


# ------------------------------------------------------- 跨轮场景(R-09 验收)
def _items_like_old_main(r):
    """修复前 main.py 的写法:整条 actions 原样落盘(不裁剪)。

    只在 `pending_items_from_records` 不存在(旧代码)时使用 —— 让反转验证
    看到的是"续做重放已成功动作"的行为差异,而不是一个 ImportError。
    """
    return [
        {
            "player": r["player"],
            "rank": r["rank"],
            "actions": list(r["actions"]),
            "trigger": None,
            "weekly": r.get("weekly_total"),
            "status": "failed",
            "error": r.get("error"),
        }
    ]


def cross_round_no_replay():
    """一轮部分成功 → 中断 → 续做:已成功动作发送次数必须为 0。"""
    tmp = tempfile.mkdtemp(prefix="repro_p0_1b_x_")
    path = os.path.join(tmp, "pending_actions.json")
    # 第 1 轮:promote×2,第一条成功、第二条失败 → 落待办
    round1 = [
        {
            "player": "甲",
            "rank": "entry",
            "weekly_total": 12000,
            "actions": ["promote", "promote"],
            "executed": True,
            "error": "执行失败(promote): 超时",
            "remaining_actions": ["promote"],
        }
    ]
    if hasattr(P, "pending_items_from_records"):
        writer, merge = P.pending_items_from_records, P.merge_pending
    else:  # 旧代码:整条 actions 落盘,且不合并去重
        writer = lambda recs: _items_like_old_main(recs[0])  # noqa: E731
        merge = lambda a, b: a + b  # noqa: E731
    # 第 2 轮启动时再写一次(模拟同一失败被再次登记)
    items = merge(writer(round1), writer(round1))
    P.save_pending(path, "演示公会", items)
    with open(path, encoding="utf-8") as f:
        content = f.read()

    ex = _Ex()
    _run_pending_compat(path, ex, "kick-confirm")
    assert ex.sent == ["promote"], f"续做重放了已成功动作: {ex.sent}"
    assert content.count('"promote"') == 1, content
    return f"续做发送={ex.sent}; 待办文件={content.strip()}"


def main():
    check("R-09 待办只存未完成后缀", r09_pending_only_unfinished_suffix)
    check("R-10 待办按玩家+动作去重", r10_dedupe_same_player_same_actions)
    check("R-11 续做 kick 二次确认", r11_resume_kick_needs_second_confirm)
    check("R-12 /gc 回显不写成动作", r12_gc_task_not_written_as_actions)
    check("R-13 先 stop 再取补做队列", r13_stop_before_retry_queue)
    check("R-15 流程失败门", r15_failure_gate_blocks_by_default)
    check("跨轮:已成功动作发送次数=0", cross_round_no_replay)

    width = max(len(n) for n, _, _ in RESULTS)
    for name, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
    failed = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
