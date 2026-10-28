# -*- coding: utf-8 -*-
"""
domain/rules.py —— 自定义规则引擎（纯函数，零 IO）
=================================================
核心约束：**内置阈值规则与自定义规则共用同一接口** —— 内置规则被表达成
一条名为 `builtin_threshold` 的规则，由 `builtin_default_rule()` 生成，
其求值委托给 `decisions.decide_actions`（业务规则零改动）。

规则结构：
    {
      "id": "r1",                  # 唯一，用于报告追溯
      "name": "低贡献踢出",         # 可读名
      "enabled": true,
      "priority": 100,             # 越大越先
      "stop": true,                # 命中后是否停止后续匹配
      "conditions": [ {"field": "weekly_total", "op": "<", "value": 1000} ],
      "actions":    [ {"type": "kick", "reason": "自定义原因"} ]
    }

条件算子（field, op, value）：
    weekly_total   < <= > >= ==        数值
    rank           == != in not_in     等级键
    online         == !=               true/false
    player         == != matches        matches 支持 * ?（大小写不敏感）
    joined_at      before after        日期字符串（YYYY-MM-DD[ HH:MM:SS]）
    last_online    before after        同上

动作：promote(times) / demote(times) / kick(reason) / notify / ignore

优先级策略（cfg["rules_strategy"]）：
    first_match       按列表顺序,首个命中即用（默认）
    highest_priority  命中规则里 priority 最大者
    all               应用所有命中的规则（动作按顺序拼接）

求值结果 (actions, hit_rule_ids, traces) 是纯数据，可追溯（每步条件是否满足）。
"""
from __future__ import annotations

import fnmatch
from datetime import datetime

BUILTIN_RULE_ID = "builtin_threshold"
STRATEGIES = ("first_match", "highest_priority", "all")
ACTIONS = ("promote", "demote", "kick", "notify", "ignore")
_DATE_FIELDS = ("joined_at", "last_online")
_NUM_FIELDS = ("weekly_total",)
_ENUM_FIELDS = ("rank",)
_BOOL_FIELDS = ("online",)
_NAME_FIELDS = ("player",)


def _parse_date(text):
    """解析 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS；失败返回 None（条件判否）。"""
    if text is None:
        return None
    s = str(text).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _cmp(op, left, right):
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    return None


def check_condition(cond, member):
    """单条条件是否满足。返回 (ok, detail)。

    detail 用于报告/日志追溯（命中依据），例如 "weekly_total=800 < 1000"。
    无法求值（字段缺失/日期不可解析/算子非法）一律判否并写明原因 ——
    绝不把"读不到"当成 0 或真。
    """
    if not isinstance(cond, dict):
        return False, "条件格式非法(非对象)"
    field = cond.get("field")
    op = cond.get("op")
    value = cond.get("value")

    if field in _NUM_FIELDS:
        raw = member.get(field)
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            return False, f"{field} 不可读"
        res = _cmp(op, raw, value)
        if res is None:
            return False, f"{field} 算子非法: {op}"
        return res, f"{field}={raw} {op} {value}"

    if field in _ENUM_FIELDS:
        raw = member.get(field)
        if raw is None:
            return False, f"{field} 不可读"
        if op in ("==", "!="):
            return _cmp(op, raw, value), f"{field}={raw!r} {op} {value!r}"
        if op in ("in", "not_in"):
            if not isinstance(value, (list, tuple)):
                return False, f"{field} 的 {op} 需要列表"
            inside = raw in value
            return (inside if op == "in" else not inside), f"{field}={raw!r} {op} {value}"
        return False, f"{field} 算子非法: {op}"

    if field in _BOOL_FIELDS:
        raw = member.get(field)
        if not isinstance(raw, bool):
            return False, f"{field} 不可读"
        if op not in ("==", "!="):
            return False, f"{field} 算子非法: {op}"
        return _cmp(op, raw, bool(value)), f"{field}={raw} {op} {value}"

    if field in _NAME_FIELDS:
        raw = member.get(field)
        if not raw:
            return False, f"{field} 不可读"
        if op == "matches":
            res = fnmatch.fnmatchcase(str(raw).casefold(), str(value).casefold())
            return res, f"{field}={raw!r} 匹配 {value!r}"
        if op in ("==", "!="):
            return _cmp(op, str(raw), str(value)), f"{field}={raw!r} {op} {value!r}"
        return False, f"{field} 算子非法: {op}"

    if field in _DATE_FIELDS:
        left = _parse_date(member.get(field))
        right = _parse_date(value)
        if left is None:
            return False, f"{field} 不可读"
        if right is None:
            return False, f"{field} 阈值日期非法: {value!r}"
        if op == "before":
            return left < right, f"{field}={left.date()} before {right.date()}"
        if op == "after":
            return left > right, f"{field}={left.date()} after {right.date()}"
        return False, f"{field} 算子非法: {op}"

    return False, f"未知字段: {field}"


def rule_matches(rule, member):
    """规则是否命中。返回 (ok, trace_list)。条件之间是 AND；空条件恒真。"""
    trace = []
    for i, cond in enumerate(rule.get("conditions") or []):
        ok, detail = check_condition(cond, member)
        trace.append({"index": i, "ok": ok, "detail": detail})
        if not ok:
            return False, trace
    return True, trace


def _actions_of(rule):
    """把规则里的动作规格转成引擎的扁平动作列表。"""
    out = []
    for act in rule.get("actions") or []:
        if not isinstance(act, dict):
            continue
        kind = act.get("type")
        if kind == "promote":
            times = int(act.get("times", 1) or 1)
            out.extend(["promote"] * max(1, times))
        elif kind == "demote":
            times = int(act.get("times", 1) or 1)
            out.extend(["demote"] * max(1, times))
        elif kind == "kick":
            out.append("kick")
        elif kind == "notify":
            out.append("notify")
        elif kind == "ignore":
            out.append("ignore")
    return out


def builtin_default_rule(cfg):
    """把内置阈值规则表达成"一条规则"，与自定义规则共用同一接口。

    求值委托 decisions.decide_actions —— 阈值语义零改动。
    这样"默认规则"不是另一套代码路径，而只是规则表里的一个成员。
    """
    return {
        "id": BUILTIN_RULE_ID,
        "name": "内置阈值规则",
        "enabled": True,
        "priority": 0,
        "stop": True,
        "builtin": True,
        "conditions": [],
        "actions": [],
        "_thresholds": cfg["thresholds"],
    }


def _eval_builtin(rule, member):
    """内置规则求值：委托 decide_actions（唯一真源）。"""
    from guild_auto.domain.decisions import decide_actions

    return decide_actions(
        member.get("rank"), member.get("weekly_total"), rule["_thresholds"]
    )


def evaluate(rule, member):
    """对单条规则求值。返回 (actions, trigger, trace)。"""
    if rule.get("builtin"):
        actions, trigger = _eval_builtin(rule, member)
        return actions, trigger, [{"index": -1, "ok": True, "detail": f"内置阈值命中 {trigger}"}]
    ok, trace = rule_matches(rule, member)
    if not ok:
        return [], None, trace
    return _actions_of(rule), rule.get("id"), trace


def evaluate_rules(rules, member, strategy="first_match"):
    """按策略对整张规则表求值。

    返回 (actions, hit_rule_ids, traces)：
      actions        扁平动作列表（供执行阶段使用）
      hit_rule_ids   命中的规则 id 列表（报告可追溯）
      traces         [{rule_id, matched, trigger, conditions:[...]}]
    """
    enabled = [r for r in rules if r.get("enabled", True)]
    traces = []
    hits = []

    def _record(rule, actions, trigger, trace):
        traces.append(
            {
                "rule_id": rule.get("id"),
                "matched": bool(actions) or bool(trigger),
                "trigger": trigger,
                "conditions": trace,
            }
        )
        return actions

    if strategy == "all":
        actions = []
        for rule in enabled:
            acts, trigger, trace = evaluate(rule, member)
            matched = bool(acts) or bool(trigger)
            if matched:
                hits.append(rule.get("id"))
            actions.extend(_record(rule, acts, trigger, trace))
            if matched and rule.get("stop"):
                break
        return actions, hits, traces

    if strategy == "highest_priority":
        best = None
        for rule in enabled:
            acts, trigger, trace = evaluate(rule, member)
            matched = bool(acts) or bool(trigger)
            _record(rule, acts, trigger, trace)
            if not matched:
                continue
            pri = rule.get("priority", 0)
            if best is None or pri > best[0]:
                best = (pri, rule, acts)
        if best is None:
            return [], [], traces
        return best[2], [best[1].get("id")], traces

    # first_match（默认）
    for rule in enabled:
        acts, trigger, trace = evaluate(rule, member)
        matched = bool(acts) or bool(trigger)
        _record(rule, acts, trigger, trace)
        if matched:
            return acts, [rule.get("id")], traces
        # 未命中不触发 stop —— stop 的语义是"命中后不再匹配后续规则"
    return [], [], traces


def validate_rules(rules):
    """启动期校验：语法错误必须**报错而不是静默兜底**。

    返回 [{"rule_id":..., "field":..., "message":...}]；空列表表示合法。
    """
    errors = []
    if rules is None:
        return errors
    if not isinstance(rules, list):
        return [{"rule_id": "", "field": "rules", "message": "rules 必须是列表"}]

    seen = set()
    for idx, rule in enumerate(rules):
        if not isinstance(rule, dict):
            errors.append({"rule_id": f"[{idx}]", "field": "", "message": "规则必须是对象"})
            continue
        rid = rule.get("id")
        where = rid or f"[{idx}]"
        if not rid:
            errors.append({"rule_id": where, "field": "id", "message": "缺少 id"})
        elif rid in seen:
            errors.append({"rule_id": rid, "field": "id", "message": "id 重复"})
        else:
            seen.add(rid)

        if "priority" in rule and not isinstance(rule["priority"], int):
            errors.append(
                {"rule_id": where, "field": "priority", "message": "priority 必须是整数"}
            )
        for key in ("stop", "enabled"):
            if key in rule and not isinstance(rule[key], bool):
                errors.append(
                    {"rule_id": where, "field": key, "message": f"{key} 必须是布尔值"}
                )

        conds = rule.get("conditions")
        if conds is not None and not isinstance(conds, list):
            errors.append(
                {"rule_id": where, "field": "conditions", "message": "conditions 必须是列表"}
            )
        for ci, cond in enumerate(conds or []):
            if not isinstance(cond, dict):
                errors.append(
                    {"rule_id": where, "field": f"conditions[{ci}]", "message": "条件必须是对象"}
                )
                continue
            field = cond.get("field")
            op = cond.get("op")
            allowed = {
                "weekly_total": ("<", "<=", ">", ">=", "=="),
                "rank": ("==", "!=", "in", "not_in"),
                "online": ("==", "!="),
                "player": ("==", "!=", "matches"),
                "joined_at": ("before", "after"),
                "last_online": ("before", "after"),
            }
            if field not in allowed:
                errors.append(
                    {
                        "rule_id": where,
                        "field": f"conditions[{ci}].field",
                        "message": f"未知字段 {field!r}，可用: {sorted(allowed)}",
                    }
                )
            elif op not in allowed[field]:
                errors.append(
                    {
                        "rule_id": where,
                        "field": f"conditions[{ci}].op",
                        "message": f"{field} 不支持算子 {op!r}，可用: {allowed[field]}",
                    }
                )
            elif "value" not in cond:
                errors.append(
                    {
                        "rule_id": where,
                        "field": f"conditions[{ci}].value",
                        "message": "缺少 value",
                    }
                )

        acts = rule.get("actions")
        if not isinstance(acts, list) or not acts:
            errors.append(
                {"rule_id": where, "field": "actions", "message": "actions 必须是非空列表"}
            )
            continue
        for ai, act in enumerate(acts):
            if not isinstance(act, dict):
                errors.append(
                    {"rule_id": where, "field": f"actions[{ai}]", "message": "动作必须是对象"}
                )
                continue
            kind = act.get("type")
            if kind not in ACTIONS:
                errors.append(
                    {
                        "rule_id": where,
                        "field": f"actions[{ai}].type",
                        "message": f"未知动作 {kind!r}，可用: {ACTIONS}",
                    }
                )
            if kind in ("promote", "demote"):
                times = act.get("times", 1)
                if not isinstance(times, int) or times < 1:
                    errors.append(
                        {
                            "rule_id": where,
                            "field": f"actions[{ai}].times",
                            "message": "times 必须是 >=1 的整数",
                        }
                    )
    return errors
