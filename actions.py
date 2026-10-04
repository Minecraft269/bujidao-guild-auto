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

import logger

# 等级链:索引越小等级越低(用于计算 升职N次/降职1次 后的新等级)
# 业务规则 §1:入门成员 → 低活跃 → 中等活跃 → 高活跃
RANK_ORDER = ["entry", "low", "mid", "high"]


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
        if weekly <= t.get("high_demote", 30000):
            res = (["demote"], "high_demote")
            logger.trace_return("decide_actions", res)
            return res
    elif rank == "mid":
        # 先升职(>= 30000),后降职(< 5000);中间区间无操作
        if weekly >= t.get("mid_promote", 30000):
            res = (["promote"], "mid_promote")
            logger.trace_return("decide_actions", res)
            return res
        if weekly < t.get("mid_demote", 5000):
            res = (["demote"], "mid_demote")
            logger.trace_return("decide_actions", res)
            return res
    elif rank == "low":
        # 降序阈值:30000 → ×2,10000 → ×1,最后才是 < 3500 降职
        if weekly >= t.get("low_promote_2", 30000):
            res = (["promote", "promote"], "low_promote_2")
            logger.trace_return("decide_actions", res)
            return res
        if weekly >= t.get("low_promote_1", 10000):
            res = (["promote"], "low_promote_1")
            logger.trace_return("decide_actions", res)
            return res
        if weekly < t.get("low_demote", 3500):
            res = (["demote"], "low_demote")
            logger.trace_return("decide_actions", res)
            return res
    elif rank == "entry":
        # 降序阈值:30000 → ×3,10000 → ×2,3500 → ×1,最后才是 < 3500 踢出。
        # entry_promote_1(>= 3500)与 entry_kick(< 3500)在 3500 处分段无缝衔接,
        # 3500 本身归升职一侧(踢出不含边界)。
        if weekly >= t.get("entry_promote_3", 30000):
            res = (["promote", "promote", "promote"], "entry_promote_3")
            logger.trace_return("decide_actions", res)
            return res
        if weekly >= t.get("entry_promote_2", 10000):
            res = (["promote", "promote"], "entry_promote_2")
            logger.trace_return("decide_actions", res)
            return res
        if weekly >= t.get("entry_promote_1", 3500):
            res = (["promote"], "entry_promote_1")
            logger.trace_return("decide_actions", res)
            return res
        if weekly < t.get("entry_kick", 3500):
            res = (["kick"], "entry_kick")
            logger.trace_return("decide_actions", res)
            return res
    # 未命中任何分支(含未知 rank)→ 不做任何操作
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
    """
    logger.trace_code_location("build_notification.start", f"player={player} weekly={weekly} actions={actions}")
    templates = cfg.get("notify_templates", {})
    guild_qq = cfg.get("guild_qq", "")
    if not actions:
        tmpl = templates.get(
            "none",
            "{player} 这周贡献为:{weekly} 执行操作:无 非常感谢你对公会做出的贡献 公会群:{guild_qq}",
        )
        res = tmpl.format(player=player, weekly=weekly, guild_qq=guild_qq)
        logger.trace_return("build_notification", res)
        return res
    kind = actions[0]
    if kind == "kick":
        tmpl = templates.get(
            "kick",
            "{player} 这周贡献为:{weekly} 执行操作:踢出 未完成这周最低标准 "
            "若有异议请去群里寻找执行管理员 公会群:{guild_qq}",
        )
    elif kind == "demote":
        tmpl = templates.get(
            "demote",
            "{player} 这周贡献为:{weekly} 执行操作:降职 "
            "若有异议请去群里找执行此操作的管理员 公会群:{guild_qq}",
        )
    else:
        tmpl = templates.get(
            "promote",
            "{player} 这周贡献为:{weekly} 执行操作:升职({count}次) "
            "非常感谢你对公会做出的贡献 公会群:{guild_qq}",
        )
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
    """
    cmds = cfg.get("commands", {})
    if action == "promote":
        return cmds.get("guild_promote", "/guild promote {player}").format(player=player)
    if action == "demote":
        return cmds.get("guild_demote", "/guild demote {player}").format(player=player)
    if action == "kick":
        reason = cmds.get("kick_reason", "未完成一周最低标准")
        return cmds.get("guild_kick", "/guild kick {player} {reason}").format(
            player=player, reason=reason
        )
    raise ValueError(f"未知动作: {action}")
