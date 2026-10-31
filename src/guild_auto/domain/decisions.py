# -*- coding: utf-8 -*-
"""
actions.py —— 决策引擎 + 通知文案 + 命令格式(纯逻辑)
=================================================
本模块只做三件纯逻辑的事,全部可脱离游戏窗口独立单元测试:

  * decide_actions      —— 阈值矩阵判定 升职/降职/踢出(业务规则 §1)
  * build_notification  —— 渲染通知文案(业务规则 §3)
  * build_command       —— 生成游戏内指令文本(业务规则 §2)
  * new_rank_after      —— 动作执行后的新等级(报告用)

**刻意不 import pyautogui / 不做任何键盘、剪贴板、窗口、OCR 交互。**
发送与执行属于硬件层,已独立在 action_executor.py(见该模块);
本模块只产出「该做什么」与「该发什么字」,不碰设备。
"""

import fnmatch
from guild_auto.infrastructure import logger

# 等级链:索引越小等级越低(用于计算 升职N次/降职1次 后的新等级)
# 业务规则 §1:入门成员 → 低活跃 → 中等活跃 → 高活跃
RANK_ORDER = ["entry", "low", "mid", "high"]

_SNIP_MAX = 80


def _snip(value, limit=_SNIP_MAX):
    """值 → 单行短片段(超长截断),用于日志里带玩家名/条件明细。"""
    if value is None:
        return "None"
    s = value if isinstance(value, str) else repr(value)
    s = s.replace("\r", " ").replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + f"...(+{len(s) - limit}字符)"


def _log_decision(rank, weekly, trigger, threshold_value, comparison, actions):
    """单条阈值决策的 DEBUG 埋点(第 5 级可见输入值与结论)。

    只新增日志,不参与任何判定;每条玩家决策一行,与上层 decide_for_player
    的 debug 行合并,不额外刷屏。
    """
    logger.log_debug(
        f"[阈值决策] rank={rank} weekly={weekly} trigger={trigger} "
        f"threshold_value={threshold_value} comparison={comparison} actions={actions}"
    )


def decide_actions(rank, weekly, thresholds):
    """按业务规则 §1 的阈值矩阵判定动作,是整个脚本的决策真源。

    判定顺序:每个等级内先查升职分支,再查降职/踢出分支(if/elif 链,原脚本顺序)。
    先命中先返回,因此高阈值分支必须排在低阈值分支之前。

    边界语义(逐字继承原脚本,改动即为业务事故):
      - 含边界(<= / >=):high_demote、mid_promote、low_promote_1/2、
        entry_promote_1/2/3
      - 不含边界(严格 <):mid_demote、low_demote、entry_kick
      - 30000 处 high 与 mid **同值反向**:high 是 `exp <= 30000` 降职,
        mid 是 `exp >= 30000` 升职。同一个 30000 在两个等级上走向相反,
        这是原脚本的真实行为,必须原样保留,不得「修正」成一致。
      - 降职类(high_demote/mid_demote/low_demote/entry_kick)的触发区间在
        边界**内侧**;升职类的触发区间在边界**外侧**。

    阈值从 thresholds 传入(全部可配置),默认值与业务规则 §1 完全一致;
    thresholds 缺键时退回该键的默认值,因此只传部分键也能跑。
    返回值对同一 (rank, weekly, thresholds) 是纯函数式的、无副作用的。

    参数:
      rank:       "entry" / "low" / "mid" / "high";未知等级 → 无动作
      weekly:     周贡献(公会经验,已去千分位逗号的整数)
      thresholds: 阈值字典,键名见 DEFAULT_CONFIG["thresholds"]

    返回 (actions, trigger_key):
      actions:    动作名列表,如 ["promote","promote"] / ["demote"] / ["kick"] / []
      trigger_key:命中的阈值配置键名(供报告标注);无动作时为 None
    """
    logger.trace_code_location("decide_actions.start", f"rank={rank} weekly={weekly}")
    t = thresholds
    if rank == "high":
        # 高活跃:无升职分支,<= 30000 直接降职(含边界)
        limit = t.get("high_demote", 30000)
        if weekly <= limit:
            res = (["demote"], "high_demote")
            _log_decision(rank, weekly, "high_demote", limit, f"weekly<={limit}", res[0])
            logger.trace_return("decide_actions", res)
            return res
    elif rank == "mid":
        # 先升职(>= 30000),后降职(< 5000);中间区间无操作
        limit = t.get("mid_promote", 30000)
        if weekly >= limit:
            res = (["promote"], "mid_promote")
            _log_decision(rank, weekly, "mid_promote", limit, f"weekly>={limit}", res[0])
            logger.trace_return("decide_actions", res)
            return res
        limit = t.get("mid_demote", 5000)
        if weekly < limit:
            res = (["demote"], "mid_demote")
            _log_decision(rank, weekly, "mid_demote", limit, f"weekly<{limit}", res[0])
            logger.trace_return("decide_actions", res)
            return res
    elif rank == "low":
        # 降序阈值:30000 → ×2,10000 → ×1,最后才是 < 3500 降职
        limit = t.get("low_promote_2", 30000)
        if weekly >= limit:
            res = (["promote", "promote"], "low_promote_2")
            _log_decision(rank, weekly, "low_promote_2", limit, f"weekly>={limit}", res[0])
            logger.trace_return("decide_actions", res)
            return res
        limit = t.get("low_promote_1", 10000)
        if weekly >= limit:
            res = (["promote"], "low_promote_1")
            _log_decision(rank, weekly, "low_promote_1", limit, f"weekly>={limit}", res[0])
            logger.trace_return("decide_actions", res)
            return res
        limit = t.get("low_demote", 3500)
        if weekly < limit:
            res = (["demote"], "low_demote")
            _log_decision(rank, weekly, "low_demote", limit, f"weekly<{limit}", res[0])
            logger.trace_return("decide_actions", res)
            return res
    elif rank == "entry":
        # 降序阈值:30000 → ×3,10000 → ×2,3500 → ×1,最后才是 < 3500 踢出。
        # entry_promote_1(>= 3500)与 entry_kick(< 3500)在 3500 处分段无缝衔接,
        # 3500 本身归升职一侧(踢出不含边界)。
        limit = t.get("entry_promote_3", 30000)
        if weekly >= limit:
            res = (["promote", "promote", "promote"], "entry_promote_3")
            _log_decision(rank, weekly, "entry_promote_3", limit, f"weekly>={limit}", res[0])
            logger.trace_return("decide_actions", res)
            return res
        limit = t.get("entry_promote_2", 10000)
        if weekly >= limit:
            res = (["promote", "promote"], "entry_promote_2")
            _log_decision(rank, weekly, "entry_promote_2", limit, f"weekly>={limit}", res[0])
            logger.trace_return("decide_actions", res)
            return res
        limit = t.get("entry_promote_1", 3500)
        if weekly >= limit:
            res = (["promote"], "entry_promote_1")
            _log_decision(rank, weekly, "entry_promote_1", limit, f"weekly>={limit}", res[0])
            logger.trace_return("decide_actions", res)
            return res
        limit = t.get("entry_kick", 3500)
        if weekly < limit:
            res = (["kick"], "entry_kick")
            _log_decision(rank, weekly, "entry_kick", limit, f"weekly<{limit}", res[0])
            logger.trace_return("decide_actions", res)
            return res
    # 未命中任何分支(含未知 rank)→ 不做任何操作
    _log_decision(rank, weekly, None, None, "无匹配分支", [])
    logger.trace_return("decide_actions", ([], None))
    return [], None


def new_rank_after(rank, actions):
    """动作执行后的新等级(报告用):升职 N 级 / 降职 1 级 / 踢出 → "kicked"。

    等级链 RANK_ORDER = entry → low → mid → high,封顶封底:
      - 升职到链顶(high)后不再往上,停在 "high";
      - 降职到链底(entry)后不再往下,停在 "entry"(业务上不会出现,
        entry 只会被踢出,但保守处理避免越界 IndexError)。
    actions 为空 → 返回原等级。
    只认 promote/demote/kick 三种已知动作;未知动作名(配置写错等)保守返回
    原等级,绝不落进 promote 分支被当成升职 —— 那会让报告误报新等级、掩盖异常。
    """
    logger.trace_code_location("new_rank_after.start", f"rank={rank} actions={actions}")
    if not actions:
        logger.trace_return("new_rank_after", rank)
        return rank
    if actions[0] == "kick":
        res = "kicked"
        logger.trace_return("new_rank_after", res)
        return res
    if actions[0] == "demote":
        if rank in RANK_ORDER:
            idx = RANK_ORDER.index(rank)
            res = RANK_ORDER[idx - 1] if idx > 0 else rank
            logger.trace_return("new_rank_after", res)
            return res
        logger.trace_return("new_rank_after", rank)
        return rank
    # promote ×N
    if actions[0] == "promote":
        if rank in RANK_ORDER:
            idx = RANK_ORDER.index(rank)
            res = RANK_ORDER[min(idx + len(actions), len(RANK_ORDER) - 1)]
            logger.trace_return("new_rank_after", res)
            return res
        logger.trace_return("new_rank_after", rank)
        return rank
    logger.trace_return("new_rank_after", rank)
    return rank


def build_notification(player, weekly, actions, cfg, count=None):
    """渲染通知文案(业务规则 §3,四类文案逐字继承原脚本)。

    占位符:{player} {weekly} {count} {guild_qq}
    模板与群号全部取自 cfg(配置化,零硬编码);cfg 缺键时回落到内联默认值,
    这些默认值与 config.DEFAULT_CONFIG 完全一致。

    文案选择的边界:
      - actions 为空          → none 模板(无 count 占位符,渲染时不传 count)
      - actions[0] == "promote" → promote 模板,count = len(actions) 即升职次数
      - actions[0] == "demote" → demote 模板(无 count)
      - actions[0] == "kick"   → kick 模板(无 count)
    count 参数可显式覆盖升职次数(默认取 actions 长度);无操作/降职/踢出
    三类的模板不含 {count},显式传入 count 对它们无影响。

    参数:
      player: 玩家 ID
      weekly: 周贡献(公会经验,整数)
      actions: decide_actions 的返回值
      cfg:     配置字典(读 notify_templates / guild_qq)
    返回:可直接作为 /gc 广播正文的中文字符串。

    **零硬编码**:模板只认 cfg["notify_templates"],代码不提供兜底文案。
    缺键时抛 KeyError 让问题在启动阶段就暴露 —— 静默用硬编码兜底会在
    用户改过配置后悄悄覆盖他的设置,使"文案不得改动"失去唯一真源
    (见 CLAUDE.md 第 1 条)。
    """
    logger.trace_code_location(
        "build_notification.start", f"player={player} weekly={weekly} actions={actions}"
    )
    templates = cfg.get("notify_templates", {})
    guild_qq = cfg.get("guild_qq", "")

    def _tpl(key):
        try:
            return templates[key]
        except KeyError:
            raise KeyError(
                f"配置缺少 notify_templates.{key} —— 业务文案必须由配置提供,代码不提供兜底串"
            ) from None

    if not actions:
        tmpl = _tpl("none")
        res = tmpl.format(player=player, weekly=weekly, guild_qq=guild_qq)
        logger.trace_return("build_notification", res)
        return res
    kind = actions[0]
    if kind == "kick":
        tmpl = _tpl("kick")
    elif kind == "demote":
        tmpl = _tpl("demote")
    else:
        tmpl = _tpl("promote")
    if count is None:
        count = len(actions)
    res = tmpl.format(player=player, weekly=weekly, count=count, guild_qq=guild_qq)
    logger.trace_return("build_notification", res)
    return res


def build_command(cfg, action, player):
    """生成游戏内指令文本(业务规则 §2,命令格式逐字继承原脚本)。

    仅覆盖三类**破坏性**动作;格式全部取自 cfg["commands"],零硬编码:
      promote → commands.guild_promote  = "/guild promote {player}"
                 (升职 N 级 = 该命令重复发送 N 次,重复由调用方做)
      demote  → commands.guild_demote   = "/guild demote {player}"
      kick    → commands.guild_kick     = "/guild kick {player} {reason}"
                 reason 取 commands.kick_reason = "未完成一周最低标准"(踢出必带原因)

    查询命令(/guild list、/guild member)刻意**不经本函数** —— 调用方(main)
    自行按 cfg["commands"] 拼接。未知 action 一律 raise ValueError 而非"猜"
    出一条指令:猜错会把一条查询发成破坏性操作。宁可中断也不误发。

    玩家 ID 原样嵌入,不做 strip/转义 —— 真实样本存在含空格的玩家名。

    **零硬编码**:命令格式与 kick_reason 全部取自 cfg["commands"],
    缺键抛 KeyError(见 CLAUDE.md 第 1 条)。
    """
    cmds = cfg.get("commands", {})

    def _cmd(key):
        try:
            return cmds[key]
        except KeyError:
            raise KeyError(f"配置缺少 commands.{key} —— 命令格式必须由配置提供") from None

    if action == "promote":
        return _cmd("guild_promote").format(player=player)
    if action == "demote":
        return _cmd("guild_demote").format(player=player)
    if action == "kick":
        return _cmd("guild_kick").format(player=player, reason=_cmd("kick_reason"))
    raise ValueError(f"未知动作: {action}")


def count_actions(records):
    """统计各动作人数(供 summary 广播文案用)。

    返回 {"promote": n, "demote": n, "kick": n}。
    以 actions[0] 为准 —— 一个玩家的动作列表第一个元素即其主要操作
    (promote ×N 表示升 N 级,仍算"被升职 1 人")。
    """
    counts = {"promote": 0, "demote": 0, "kick": 0}
    for r in records:
        acts = r.get("actions") or []
        if not acts:
            continue
        kind = acts[0]
        if kind in counts:
            counts[kind] += 1
    return counts


def build_summary_message(records, cfg):
    """渲染 summary 模式的总广播文案(继承清单 §3)。

    业务规则:文案模板**只能来自配置** cfg["notify_templates"]["summary"],
    代码里不放兜底串 —— 硬编码文案会在配置被修改后悄悄覆盖用户设置,
    且让"文案不得改动"这条红线失去唯一真源。缺配置则抛 KeyError
    让问题在启动阶段就暴露。

    数值由 count_actions 实统计得出,不能用 records 长度代替
    (那会把"无操作"玩家也算进升降踢人数)。
    """
    c = count_actions(records)
    templates = cfg.get("notify_templates", {})
    if "summary" not in templates:
        raise KeyError(
            "配置缺少 notify_templates.summary —— 业务文案必须由配置提供,"
            "代码不提供兜底串(硬编码文案会覆盖用户修改)"
        )
    tmpl = templates["summary"]
    return tmpl.format(
        promote=c["promote"], demote=c["demote"], kick=c["kick"], admin=cfg.get("admin") or "管理员"
    )


# ---------------------------------------------------------------------------
# 名单匹配与优先级短路(阶段 5)
# ---------------------------------------------------------------------------
# 优先级(短路,先命中先返回):
#   功能开关(promote/demote/kick enabled)
#     > blacklist            → 完全不处理
#     > 屏蔽词策略            → 名字不可读,不发送查询
#     > whitelist(非空)       → 只处理名单内的人
#     > whitelist_protected   → 免降职/踢出(升职仍生效)
#     > 阈值判定
# 功能开关在执行阶段生效(此处不重复判断),因此本函数返回的第 4 步之后
# 仍可能被执行阶段按开关拦下 —— 这与原脚本行为一致。

BLOCK_BLACKLIST = "blacklist"
BLOCK_NOT_WHITELISTED = "not_in_whitelist"


def name_matches(name, patterns, wildcard=True):
    """名字是否命中名单。**大小写不敏感**;wildcard=True 时支持 * 与 ?。

    用 fnmatch 的语义(casefold 后比较),空模式列表恒 False。
    注意:这里只做名字比较,不做任何 IO,便于纯函数单测。
    """
    if not patterns:
        return False
    target = str(name).casefold()
    for pat in patterns:
        p = str(pat).casefold()
        if wildcard:
            if fnmatch.fnmatchcase(target, p):
                return True
        elif target == p:
            return True
    return False


def name_list_block_reason(player, cfg):
    """名单层短路:返回拦截原因常量,或 None 表示放行。

    只看 blacklist 与 whitelist(屏蔽词由查询阶段处理,因为它跟"能不能
    发送指令"有关,与"该不该动他"是两个维度)。
    """
    wildcard = bool(cfg.get("match_wildcard", True))
    if name_matches(player, cfg.get("blacklist") or [], wildcard):
        return BLOCK_BLACKLIST
    wl = cfg.get("whitelist") or []
    if wl and not name_matches(player, wl, wildcard):
        return BLOCK_NOT_WHITELISTED
    return None


def is_protected(player, cfg):
    """是否在保护名单(免降职/踢出,升职仍生效)。"""
    return name_matches(
        player, cfg.get("whitelist_protected") or [], bool(cfg.get("match_wildcard", True))
    )


def decide_for_player(
    player, rank, weekly, cfg, member_joined_at=None, member_last_online=None, member_online=None
):
    """单个玩家的完整判定:名单短路优先于阈值矩阵。

    返回 (actions, trigger):
      * 被 blacklist / whitelist 拦下 → ([], 原因常量),报告里据此说明
      * 命中保护名单 → 只保留升职动作(降职/踢出被剥夺)
      * 其余 → 交给 decide_actions 走原阈值矩阵(业务规则零改动)
    """
    blocked = name_list_block_reason(player, cfg)
    if blocked is not None:
        logger.log_debug(
            f"[决策] 名单短路 | player={_snip(player)} rank={rank} weekly={weekly} "
            f"blocked_reason={blocked} trigger={blocked} actions_in=[] actions_out=[]"
        )
        return [], blocked

    # 规则引擎（阶段 4）：开启时，内置阈值规则作为"一条默认规则"参与同一接口。
    # 关闭时保持原有纯阈值路径 —— 行为零变化。
    if cfg.get("rules_enabled"):
        from guild_auto.domain.rules import builtin_default_rule, evaluate_rules

        member = {
            "player": player,
            "rank": rank,
            "weekly_total": weekly,
            "joined_at": member_joined_at,
            "last_online": member_last_online,
            "online": member_online,
        }
        # 自定义规则在前、内置阈值作兜底：first_match 下自定义优先；
        # highest_priority 下由 priority 决定（内置默认 0）。
        table = list(cfg.get("rules") or []) + [builtin_default_rule(cfg)]
        actions, hits, traces = evaluate_rules(
            table, member, cfg.get("rules_strategy", "first_match")
        )
        trigger = f"rule:{hits[0]}" if hits else None
        cond_trace = "; ".join(
            f"{rec.get('rule_id')}[{c.get('index')}]={c.get('detail')}"
            for rec in traces
            for c in rec.get("conditions") or []
        )
        if actions and is_protected(player, cfg):
            # 保护名单只免「踢出」:升降职照常(plan.txt 验收 16)。
            # 早先连 demote 一起剥夺,等于把保护名单变成了"冻结名单"。
            kept = [a for a in actions if a in ("promote", "demote")]
            if kept:
                logger.log_debug(
                    f"[决策] 保护名单保留升降职 | player={_snip(player)} rank={rank} "
                    f"weekly={weekly} rule_id={hits} actions_in={actions} "
                    f"actions_out={kept} trigger={trigger} conditions={_snip(cond_trace)}"
                )
                return kept, trigger
            logger.log_warning(
                f"[决策] 保护名单剥夺全部动作(含踢出) | player={_snip(player)} rank={rank} "
                f"weekly={weekly} rule_id={hits} actions_in={actions} actions_out=[] "
                f"trigger={trigger} kept_actions=[] conditions={_snip(cond_trace)}"
            )
            return [], "whitelist_protected"
        logger.log_debug(
            f"[决策] 规则引擎结论 | player={_snip(player)} rank={rank} weekly={weekly} "
            f"rule_id={hits} actions_in={actions} actions_out={actions} "
            f"trigger={trigger} conditions={_snip(cond_trace)}"
        )
        return actions, trigger

    actions, trigger = decide_actions(rank, weekly, cfg["thresholds"])

    if actions and is_protected(player, cfg):
        # 保护名单:只剥夺 kick,降职照常
        kept = [a for a in actions if a in ("promote", "demote")]
        if kept:
            logger.log_debug(
                f"[决策] 保护名单保留升降职 | player={_snip(player)} trigger={trigger} "
                f"actions_in={actions} actions_out={kept}"
            )
            return kept, trigger
        logger.log_warning(
            f"[决策] 保护名单剥夺全部动作(含踢出) | player={_snip(player)} rank={rank} "
            f"weekly={weekly} trigger={trigger} actions_in={actions} actions_out=[] "
            f"kept_actions=[]"
        )
        return [], "whitelist_protected"
    logger.log_debug(
        f"[决策] 阈值结论 | player={_snip(player)} rank={rank} weekly={weekly} "
        f"trigger={trigger} actions_in={actions} actions_out={actions}"
    )
    return actions, trigger
