# -*- coding: utf-8 -*-
"""
vision/verifier.py —— 界面操作的结果验证
=========================================
三种操作的回显不同(`docs/UI识别模式详解.md` §四),因此验证策略也不同:

  升职/降职  「成功设置<玩家名>的职位为<等级>!」—— 含玩家名,可按名 + 等级匹配
  踢出      「踢出成功」—— **不含玩家名**,单凭它无法归因到具体玩家
  权限不足  「你只能修改低于自己职位的公会成员职位!」—— 直接判失败并告警

踢出的交叉验证(可组合,见 config 的 vision.kick_verify):
  * grid        踢完回到公会界面重新全扫,确认该成员格位消失
  * count       踢前/踢后「人数: [N/M]」对账,N 必须减少 1
  * time_window 只接受操作起点之后、且窗口内**恰好一条**「踢出成功」回显

三种都不成立时返回「执行结果待确认」,绝不把相邻的「踢出成功」认作本次操作。
聊天回显读取函数由调用方注入(可接 log_watcher),本模块零 I/O。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅用于类型注解
    from collections.abc import Callable, Sequence

RANK_ECHO_PATTERN = "成功设置{player}的职位为{rank}!"
KICK_ECHO_TEXT = "踢出成功"
PERMISSION_DENIED_TEXT = "你只能修改低于自己职位的公会成员职位"
UNCONFIRMED = "执行结果待确认"

KICK_METHODS = ("grid", "count", "time_window", "any")


@dataclass(frozen=True)
class ChatLine:
    """一行聊天回显。时间窗验证需要时间戳(单调时钟或日志时间皆可)。

    直接传 str 也可:但那样就没有时间戳,时间窗验证会判为"无数据"。
    """

    text: str
    timestamp: float


@dataclass(frozen=True)
class VerifyResult:
    """验证结论。alert=True 表示要告警(如权限不足)。"""

    ok: bool
    reason: str
    method: str = ""
    alert: bool = False


def _text(line: ChatLine | str) -> str:
    return line.text if isinstance(line, ChatLine) else str(line)


def _timestamp(line: ChatLine | str) -> float | None:
    return line.timestamp if isinstance(line, ChatLine) else None


def _permission_denied(lines: Sequence[ChatLine | str]) -> VerifyResult | None:
    for line in lines:
        if PERMISSION_DENIED_TEXT in _text(line):
            return VerifyResult(
                ok=False,
                reason=f"权限不足(聊天回显包含「{PERMISSION_DENIED_TEXT}」),操作未生效",
                method="permission",
                alert=True,
            )
    return None


def verify_rank_change(
    *,
    player: str,
    expected_rank: str,
    chat_reader: Callable[[], Sequence[ChatLine | str]],
) -> VerifyResult:
    """升职/降职:按「玩家名 + 新等级」匹配聊天回显。

    回显含玩家名,所以可以精确归因;匹配不到就是失败(不猜"应该成功了")。
    """
    lines = tuple(chat_reader() or ())
    denied = _permission_denied(lines)
    if denied is not None:
        return denied

    expected = RANK_ECHO_PATTERN.format(player=player, rank=expected_rank)
    if any(expected in _text(line) for line in lines):
        return VerifyResult(ok=True, reason=f"聊天回显匹配:{expected}", method="echo")
    return VerifyResult(
        ok=False,
        reason=f"未在聊天回显中找到「{expected}」,无法确认 {player} 的职位变更",
        method="echo",
    )


def _grid_check(member_gone: bool | None) -> tuple[bool | None, str]:
    if member_gone is None:
        return None, "格位复核=无数据"
    return bool(member_gone), f"格位复核={'已消失' if member_gone else '仍在'}"


def _count_check(count_before: int | None, count_after: int | None) -> tuple[bool | None, str]:
    if count_before is None or count_after is None:
        return None, "人数对账=无数据"
    expected = count_before - 1
    ok = count_after == expected
    return ok, f"人数对账={count_before}→{count_after}(期望 {expected})"


def _time_window_check(
    lines: Sequence[ChatLine | str],
    operation_ts: float | None,
) -> tuple[bool | None, str]:
    if operation_ts is None:
        return None, "时间窗=无数据(需要操作起始时间)"
    hits = 0
    for line in lines:
        if KICK_ECHO_TEXT not in _text(line):
            continue
        ts = _timestamp(line)
        if ts is not None and ts >= operation_ts:
            hits += 1
    if hits == 1:
        return True, "时间窗=操作后恰有 1 条踢出成功回显"
    if hits == 0:
        return False, "时间窗=操作后没有踢出成功回显"
    return False, f"时间窗=操作后有 {hits} 条踢出成功回显,无法归因到本次操作"


def verify_kick(
    *,
    method: str,
    chat_reader: Callable[[], Sequence[ChatLine | str]] | None = None,
    operation_ts: float | None = None,
    member_gone: bool | None = None,
    count_before: int | None = None,
    count_after: int | None = None,
) -> VerifyResult:
    """踢出验证:必须靠交叉验证,不得单凭「踢出成功」归因。

    method 取自配置 vision.kick_verify,只评估指定的一种;`any` 评估全部可用证据,
    任一成立即确认。全部不成立(或证据缺失)返回「执行结果待确认」。
    """
    if method not in KICK_METHODS:
        raise ValueError(f"kick_verify 取值非法: {method!r},应为 {KICK_METHODS} 之一")

    # 回显只读一次:读取函数可能接实时日志钩子,重复调用会漏掉窗口内的行
    lines: tuple[ChatLine | str, ...] = tuple(chat_reader() or ()) if chat_reader else ()
    if lines:
        denied = _permission_denied(lines)
        if denied is not None:
            return denied

    grid = _grid_check(member_gone)
    count = _count_check(count_before, count_after)
    window = _time_window_check(lines, operation_ts) if chat_reader else (None, "时间窗=无数据")
    evidence = {"grid": grid, "count": count, "time_window": window}

    if method == "any":
        evaluated = [(name, value) for name, (value, _detail) in evidence.items()]
        details = "；".join(detail for _value, detail in evidence.values())
        passing = [name for name, value in evaluated if value is True]
        if passing:
            return VerifyResult(
                ok=True,
                reason=f"交叉验证成立({', '.join(passing)}):{details}",
                method=method,
            )
        return VerifyResult(
            ok=False,
            reason=f"{UNCONFIRMED}:{details}",
            method=method,
        )

    value, detail = evidence[method]
    if value is True:
        return VerifyResult(ok=True, reason=detail, method=method)
    if value is False:
        return VerifyResult(ok=False, reason=f"{UNCONFIRMED}:{detail}", method=method)
    return VerifyResult(ok=False, reason=f"{UNCONFIRMED}:{detail}", method=method)
