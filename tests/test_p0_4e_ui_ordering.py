# -*- coding: utf-8 -*-
"""P0-4e 的源码级不变量：`app.js` 与 `ipc.rs` 不能被改回「等 RPC」的写法。

这两处都在 DOM/管道胶水层，跑不了纯逻辑单测；真正的行为证据来自
`scripts/repro_p0_4e.py`（首次 RPC 9ms、模态延迟 4ms、核退出 abort 1ms）。
这里只钉几条最容易被人"顺手改回去"的顺序，改回去就红。
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _code(path: Path) -> str:
    """去掉 `//` 行注释后再比 —— 注释里会写「以前是 await rpc("pause")」这类说明。"""
    src = path.read_text(encoding="utf-8")
    return "\n".join(line.split("//", 1)[0] for line in src.splitlines())


APP_JS = _code(REPO / "gui" / "src" / "app.js")
IPC_RS = _code(REPO / "gui" / "src-tauri" / "src" / "ipc.rs")


def test_close_modal_does_not_wait_for_pause_rpc():
    assert 'await rpc("pause"' not in APP_JS, (
        "askClosePolicy 又变成先等 pause 应答才建模态了 —— 核无响应时第一次点 × 会没反应"
    )
    assert 'rpc("pause", {})' in APP_JS, "pause 仍要在后台发出，只是不再 await 它"


def test_start_button_does_not_wait_for_start_rpc_before_feedback():
    start_flow = APP_JS.split('$("btn-start").onclick', 1)[1][:900]
    assert 'await rpc("start"' in start_flow, "start 仍要发"
    # 反馈必须排在 start 之前：先写 ctl-hint 再 await 倒数
    assert start_flow.index("ctl-hint") < start_flow.index('await rpc("start"'), (
        "「开始」的点击反馈必须早于 start RPC"
    )


def test_countdown_guards_on_core_gone():
    countdown = APP_JS.split("async function startupCountdown", 1)[1].split(
        "function abortCoreDependentUi", 1
    )[0]
    assert "S.coreGone" in countdown, "倒数循环必须每格检查核是否还在"


def test_ipc_closed_aborts_core_dependent_ui():
    handler = APP_JS.split('api.event.listen("ipc-closed"', 1)[1][:800]
    assert "S.coreGone = true" in handler
    assert "abortCoreDependentUi(" in handler


def test_reader_loop_never_blocks():
    """P0-4e 根因：阻塞读会占住文件对象，把后续 WriteFile 全部排队。"""
    assert "SetNamedPipeHandleState" not in IPC_RS, "不要再靠 PIPE_NOWAIT 兜底"
    assert "read_line(" not in IPC_RS, "不得再调会挂起的 read_line"
    assert "pipe_avail(" in IPC_RS, "改为按 Peek 报的字节数读"
