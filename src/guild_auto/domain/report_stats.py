# -*- coding: utf-8 -*-
"""
report_stats.py —— 运行分析报告的统计纯函数(零 IO)
==================================================
第三份报告(运行分析报告)第 4 节需要的统计量全部在这里算好:
分位数、贡献汇总、等级迁移矩阵、操作计数。

为什么单独成模块:
  * 纯函数、不读文件不碰配置,可以脱离整条运行流程单测(覆盖率门禁要求);
  * 报告渲染只负责"排版",数值口径集中在这里,改口径只改一处。

**容错口径**(报告是流程最后一步,不能因为一条脏数据就崩):
  * weekly_total 缺失 / 非数值 → 按 0 计(与贡献报告 "缺失即 0" 的现有口径一致);
    bool 是 int 子类,必须排除,否则 True 会被当成 1 分贡献;
  * 空列表 / 单元素 → 返回 0 / None 之类的确定值,不抛异常。
"""


def _num(value):
    """把字段规整成数字:缺失 / 非数值按 0 计(口径见模块 docstring)。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return value


def percentiles(values):
    """返回 P25/P50/P75/P90(整数)。

    口径:线性插值,与 numpy.percentile 默认一致 —— 位置 pos=(n-1)*p,
    取相邻两个值按小数部分插值,最后四舍五入到整数(报告只展示整数)。
    空序列 → 四个分位都是 None(报告据此打印占位符,而不是误导性的 0)。
    """
    vals = sorted(_num(v) for v in values or [])

    def _pct(p):
        if not vals:
            return None
        pos = (len(vals) - 1) * p
        lo = int(pos)
        hi = min(lo + 1, len(vals) - 1)
        frac = pos - lo
        return round(vals[lo] + (vals[hi] - vals[lo]) * frac)

    return {"p25": _pct(0.25), "p50": _pct(0.5), "p75": _pct(0.75), "p90": _pct(0.9)}


def contribution_stats(records):
    """贡献汇总:合计 / 均值 / 中位数 / 最高 / 最低(最高最低含玩家名)。

    均值与中位数四舍五入到整数;并列最高/最低取**首个**出现的玩家,
    保证同一输入每次生成同样的报告(便于逐字节回归比对)。
    """
    pairs = [(_num(r.get("weekly_total")), str(r.get("player") or "")) for r in records or []]
    if not pairs:
        return {
            "count": 0,
            "total": 0,
            "mean": 0,
            "median": 0,
            "max_value": 0,
            "max_player": "",
            "min_value": 0,
            "min_player": "",
        }

    values = [v for v, _ in pairs]
    ordered = sorted(values)
    count = len(values)
    mid = ordered[count // 2] if count % 2 else (ordered[count // 2 - 1] + ordered[count // 2]) / 2
    max_value = max(values)
    min_value = min(values)
    return {
        "count": count,
        "total": sum(values),
        "mean": round(sum(values) / count),
        "median": round(mid),
        "max_value": max_value,
        "max_player": next(p for v, p in pairs if v == max_value),
        "min_value": min_value,
        "min_player": next(p for v, p in pairs if v == min_value),
    }


def rank_transition_matrix(records, rank_order):
    """等级迁移矩阵:行 = 原等级,列 = 新等级(含 "kicked" 与 "unchanged")。

    new_rank 缺失视为"不变"(未执行 / dry-run / 无操作);未知 new_rank 也归入
    "不变" —— 矩阵只认 rank_order 里的等级,凭空多一列会让报告排版错位。

    返回 {原等级: {新等级或 "kicked"/"unchanged": 人数}}。
    每行人数之和 = 该原等级的记录数(records 里没有的等级行全 0)。
    """
    cols = list(rank_order) + ["kicked", "unchanged"]
    matrix = {rank: {c: 0 for c in cols} for rank in rank_order}
    for r in records or []:
        old = r.get("rank")
        if old not in matrix:
            continue  # 不在 rank_order 的等级(如跳过职位)不属于迁移矩阵
        new = r.get("new_rank") or old
        col = "unchanged" if new == old else new
        if col not in cols:
            col = "unchanged"
        matrix[old][col] += 1
    return matrix


def action_counts(records):
    """操作计数:升 / 降 / 踢 / 无(以 actions[0] 为准,与 count_actions 同口径)。

    actions 为空或首个动作不是三类已知动作(如 notify/ignore)→ 计入 "none",
    保证四项之和恒等于 len(records)。
    """
    out = {"promote": 0, "demote": 0, "kick": 0, "none": 0}
    for r in records or []:
        acts = r.get("actions") or []
        kind = acts[0] if acts else None
        if kind in ("promote", "demote", "kick"):
            out[kind] += 1
        else:
            out["none"] += 1
    return out
