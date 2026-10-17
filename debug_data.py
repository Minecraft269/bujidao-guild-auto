# -*- coding: utf-8 -*-
"""
debug_data.py —— Debug 模式的模拟数据生成
=========================================
从 main.py 拆出。这些函数只在 debug.enabled 且 debug.skip_stages 命中时使用,
作用是"跳过真实查询,直接给主流程喂同构数据",好让通知/执行/报告阶段能在
没有游戏在跑的情况下被完整走一遍。

关键约束:产出结构必须与真实解析结果**同构**,否则 debug 模式通过不代表真实路径通过。
  * _generate_debug_guild  → 与 parsers.parse_guild_list 的返回值同键
  * _generate_debug_records → 与 parsers.parse_guild_member 的返回值同键,
    外加 rank / actions / trigger(判定阶段填)
"""
import random
import string


def random_name(prefix="Test_"):
    """生成 Test_ 开头,后接 3~6 个随机字符(含中文)。

    玩家名走合成字符集,绝不含真实玩家名 —— 测试与报告里都是可公开的假名。
    """
    chars = (string.ascii_letters + string.digits +
             "的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年"
             "动同工也能下过子说产种面而方后多定行学法所民得经十三之进着等部度家电力里如水化"
             "高自二理起小物现实加量都两体制机当使点从业本去把性好应开它合还因由其些然前外天"
             "政四日那社义事平形相全表间样与关各重新线内数正心反你明看原又么利比或但质气第向"
             "道命此变条只没结解问意建月公无系军很情者最立代想已通并提直题党程展五果料象员革"
             "位入常文总次品式活设及管特件长求老头基资边流路级少图山统接知较将组见计别她手角"
             "期根论运农指几九区强放决西被干做必战先回则任取据处队南给色光门即保治北造百规热"
             "领七海口东导器压志世金增争济阶油思术极交受联什认六共权收证改清己美再采转更单风"
             "切打白教速花带安场身车例真务具万每目至达走积示议声报斗完类八离华名确才科张信马"
             "节话米整空元况今集温传土许步群广石记需段研界拉林律叫且究观越织装影算低持音众书"
             "布复容儿须际商非验连断深难近矿千周委素技备半办青省列习响约支般史感劳便团往酸历"
             "市克何除消构府称太准精值号率族维划选标写存候毛亲快效斯院查江型眼王按格养易置派"
             "层片始却专状育厂京识适属圆包火住调满县局照参红细引听该铁价严龙飞")
    name_len = random.randint(3, 6)
    suffix = ''.join(random.choice(chars) for _ in range(name_len))
    return f"{prefix}{suffix}"


def _generate_debug_guild(cfg):
    """生成模拟公会数据,支持 players_per_rank 为整数或对象。"""
    sim = cfg.get("debug", {}).get("simulation", {})
    guild_name = sim.get("guild_name", "Debug公会")
    rank_names = cfg.get("rank_names", {})
    ppr = sim.get("players_per_rank", 3)

    # 确定每个等级的玩家数量
    if isinstance(ppr, dict):
        per_rank = {rk: ppr.get(rk, 1) for rk in rank_names.keys()}  # 缺失的等级默认 1
    else:
        per_rank = {rk: ppr for rk in rank_names.keys()}

    groups = {}
    for rank_key, count in per_rank.items():
        groups[rank_key] = [random_name() for _ in range(count)]
    return {
        "guild_name": guild_name,
        "groups": groups,
        "total": sum(len(v) for v in groups.values()),
        "online": 0,
    }


def _generate_debug_records(cfg, guild, targets, decide_skipped=False):
    """生成模拟成员记录,周贡献在 contribution_range 内随机。

    decide_skipped=True 时跳过判定阶段,直接随机分配动作
    (对应 debug.skip_stages 含 'decide')。
    """
    sim = cfg.get("debug", {}).get("simulation", {})
    cr = sim.get("contribution_range", [0, 50000])
    min_c, max_c = cr[0], cr[1]
    records = []
    for rank, pid in targets:
        weekly = random.randint(min_c, max_c)
        record = {
            "player": pid,
            "rank": rank,
            "weekly_total": weekly,
            "joined_at": "2026-01-01 00:00:00",
            "last_online": "2026-08-17 00:00:00",
            "daily": [],
        }
        if decide_skipped:
            # 随机分配动作(可以配置比例,这里简单平均)
            action_types = ["promote", "demote", "kick", None]  # None 表示无操作
            chosen = random.choice(action_types)
            if chosen == "promote":
                record["actions"] = ["promote"] * random.randint(1, 3)
            elif chosen == "demote":
                record["actions"] = ["demote"]
            elif chosen == "kick":
                record["actions"] = ["kick"]
            else:
                record["actions"] = []
            record["trigger"] = "debug_skip_decide"
        else:
            record["actions"] = []
            record["trigger"] = None
        records.append(record)
    return records
