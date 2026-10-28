# -*- coding: utf-8 -*-
"""
config/schema.py —— 配置结构契约(键名 / 枚举 / 校验规则)
========================================================
把"配置长什么样"从"配置怎么读写"里分离:
  * loader.py 只管 JSONC 的解析与落盘
  * schema.py 管"哪些键必须存在、取什么值才算合法"

这样改校验规则不需要动 IO 代码,反之亦然。
"""

import os

from guild_auto.config.defaults import DEFAULT_CONFIG

VALID_SCOPES = ("all", "high", "mid", "low", "entry")
VALID_NOTIFY_MODES = ("summary", "per_player")


# ---------------------------------------------------------------------------


def validate_config(cfg):
    """校验配置,返回问题列表(每条为 (级别, 消息);级别: error/warning)。"""
    issues = []

    def _is_num(v):
        """bool 是 int 子类,true/false 不应通过数值校验。"""
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    def check_num(d, key, path, minimum=0):
        val = d.get(key)
        if not _is_num(val) or val < minimum:
            issues.append(
                ("error", f"配置项 {path}.{key} 应为不小于 {minimum} 的数字,当前: {val!r}")
            )

    def check_required(section, keys, path=None):
        """必需键缺失检查:键被整个删掉时兜底(值类型错由 check_num 等负责)。"""
        path = path or section
        for k in keys:
            if k not in section:
                issues.append(("error", f"缺少必需配置项 {path}.{k}"))

    def check_bool(d, key, path=""):
        val = d.get(key)
        label = f"{path}.{key}" if path else key
        if not isinstance(val, bool):
            issues.append(("error", f"配置项 {label} 应为布尔值 true/false,当前: {val!r}"))

    def section(cfg, name):
        """取子配置段;不是 dict 时返回空 dict。

        下方所有遍历(''.values() / ''.get() / ''.items()'')都经它取段:
        用户把某段写成字符串时,校验必须**报出错误**而不是在遍历处抛
        AttributeError —— 抛异常等于校验整个失效(main 里 error 分支看不到)。"""
        val = cfg.get(name)
        return val if isinstance(val, dict) else {}

    # ── 必需键(用户手删配置项时明确报错,而不是静默回退到默认后行为诡异) ──
    for _path in ("thresholds", "commands", "notify_templates"):
        if not isinstance(cfg.get(_path), dict):
            issues.append(("error", f"配置项 {_path} 应为对象(dict),当前: {cfg.get(_path)!r}"))
        else:
            check_required(cfg[_path], DEFAULT_CONFIG[_path], _path)

    # 其余子段同样要求是对象(否则下游遍历抛异常,等于校验失效)
    for _path in (
        "delays",
        "timeouts",
        "switches",
        "debug",
        "log",
        "async",
        "rank_names",
        "state_check",
    ):
        if _path in cfg and not isinstance(cfg.get(_path), dict):
            issues.append(("error", f"配置项 {_path} 应为对象(dict),当前: {cfg.get(_path)!r}"))

    th = section(cfg, "thresholds")
    for k in DEFAULT_CONFIG["thresholds"]:
        check_num(th, k, "thresholds")

    # ── 阈值逻辑矛盾:判定是 if/elif 链(先升职后降职/踢出),边界含/不含各自固定。
    # 门槛倒置会让某个区间既不升职也不降职,该成员被静默跳过 —— 属必须拦下的配置事故。
    # 表驱动:(键A, 键B, 比较函数, 报错措辞, 为什么);两端都必须是数字才比。
    _n = lambda k: th[k] if _is_num(th.get(k)) else None
    for _a, _b, _cmp, _word, _why in (
        (
            "entry_promote_1",
            "entry_kick",
            lambda a, b: a < b,
            "小于",
            "入门成员 exp 落在两者之间时既不升职也不踢出,等于静默跳过",
        ),
        ("entry_promote_1", "entry_promote_2", lambda a, b: a > b, "大于", "升职档位应随门槛递增"),
        ("entry_promote_2", "entry_promote_3", lambda a, b: a > b, "大于", "升职档位应随门槛递增"),
        ("low_promote_1", "low_promote_2", lambda a, b: a > b, "大于", "升职档位应随门槛递增"),
    ):
        if _n(_a) is None or _n(_b) is None:
            continue  # 非数值:类型错误已由上面的 check_num 报出,不重复
        if _cmp(_n(_a), _n(_b)):
            issues.append(
                ("error", f"thresholds.{_a}({_n(_a)}) 不能{_word} thresholds.{_b}({_n(_b)}):{_why}")
            )

    d = section(cfg, "delays")
    # 三组随机区间都要查 min<=max:只查 send_interval 会漏掉另外两组同样写反的情况
    for _lo, _hi in (
        ("min_send_interval", "max_send_interval"),
        ("min_paste_to_enter", "max_paste_to_enter"),
        ("min_chat_key_delay", "max_chat_key_delay"),
    ):
        check_num(d, _lo, "delays")
        check_num(d, _hi, "delays")
        # 只在两者都确实是数值时才比较:上面 check_num 可能已记下"非数字"错误,
        # 若此处无条件比较会 TypeError 崩掉整个校验(用户把延时写成字符串即触发)。
        if _is_num(d.get(_lo)) and _is_num(d.get(_hi)) and d[_lo] > d[_hi]:
            issues.append(("error", f"delays.{_lo} 不能大于 delays.{_hi}"))

    t = section(cfg, "timeouts")
    for k in ("response_wait", "max_retries"):
        # check_num 已含"负数"判断(负值 < 0),无需再单独判 < 0
        check_num(t, k, "timeouts")

    scope = cfg.get("query_scope", "all")
    if scope not in VALID_SCOPES:
        issues.append(("error", f"query_scope 取值非法: {scope!r},应为 {VALID_SCOPES} 之一"))

    # ── 开关类型:值写成字符串 "false" 是 JSON 里最常见的坑(引号一加就永远为真),
    #    到运行时表现是"开关关不掉",所以这里必须拦住。
    sw = section(cfg, "switches")
    if not isinstance(sw, dict):
        issues.append(("error", f"配置项 switches 应为对象(dict),当前: {sw!r}"))
    else:
        for k in (
            "promote_enabled",
            "demote_enabled",
            "kick_enabled",
            "execution_enabled",
            "notify_enabled",
            "verbose_log",
        ):
            check_bool(sw, k, "switches")
        if sw.get("notify_mode") not in VALID_NOTIFY_MODES:
            issues.append(
                (
                    "error",
                    f"switches.notify_mode 取值非法: {sw.get('notify_mode')!r},"
                    f"应为 {VALID_NOTIFY_MODES} 之一",
                )
            )
    check_bool(cfg, "kick_confirm", "")  # 顶层键:路径前缀为空
    # ── 名单(阶段 5)──
    for key in ("blacklist", "whitelist", "whitelist_protected"):
        if key in cfg:
            val = cfg[key]
            if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
                issues.append(("error", f"配置项 {key} 应为字符串列表,当前: {val!r}"))
    if "match_wildcard" in cfg:
        check_bool(cfg, "match_wildcard", "")
    if "blocked_name_policy" in cfg:
        pol = cfg["blocked_name_policy"]
        if pol not in ("skip", "query_attempt"):
            issues.append(
                (
                    "error",
                    f"配置项 blocked_name_policy 取值非法: {pol!r},"
                    f"应为 ('skip', 'query_attempt') 之一",
                )
            )
    if "pause_key" in cfg and not isinstance(cfg["pause_key"], str):
        issues.append(("error", f"配置项 pause_key 应为字符串,当前: {cfg['pause_key']!r}"))

    for rank in cfg.get("skip_ranks", []):
        if rank not in section(cfg, "rank_names"):
            issues.append(("warning", f"skip_ranks 中未知等级: {rank!r}"))

    for key, tmpl in section(cfg, "notify_templates").items():
        if not isinstance(tmpl, str) or not tmpl.strip():
            issues.append(("error", f"notify_templates.{key} 不能为空"))

    for cmd in section(cfg, "commands").values():
        if not isinstance(cmd, str) or not cmd.strip():
            issues.append(("error", f"commands 中存在空命令: {cmd!r}"))

    if cfg.get("chat_key") != "auto" and not isinstance(cfg.get("chat_key"), str):
        issues.append(("error", "chat_key 应为字符串('auto' 或键名)"))

    # debug skip_stages 合法性
    skip = section(cfg, "debug").get("skip_stages", [])
    valid_stages = {"list", "member", "decide", "notify", "execute"}
    for stage in skip:
        if stage not in valid_stages:
            issues.append(
                ("error", f"debug.skip_stages 中无效阶段: {stage}，应为 {valid_stages} 之一")
            )

    # 校验 players_per_rank
    sim = section(section(cfg, "debug"), "simulation")
    ppr = sim.get("players_per_rank")
    if ppr is not None:
        if isinstance(ppr, dict):
            rank_names = section(cfg, "rank_names")
            for k, v in ppr.items():
                if k not in rank_names:
                    issues.append(
                        ("warning", f"debug.simulation.players_per_rank 中未知等级: {k!r}")
                    )
                if not isinstance(v, int) or v < 0:
                    issues.append(
                        (
                            "error",
                            f"debug.simulation.players_per_rank[{k}] 应为非负整数，当前: {v!r}",
                        )
                    )
        elif not isinstance(ppr, int) or ppr < 0:
            issues.append(
                ("error", f"debug.simulation.players_per_rank 应为整数或对象，当前: {ppr!r}")
            )

    # 贡献范围必须为长度为2的列表且 min <= max
    cr = section(section(cfg, "debug"), "simulation").get("contribution_range")
    if cr is not None:
        if not isinstance(cr, list) or len(cr) != 2:
            issues.append(
                ("error", "debug.simulation.contribution_range 应为 [min, max] 且 min <= max")
            )
        elif not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in cr):
            # 元素非数值时不再比较(否则 TypeError),上面的长度错误已足够定位问题
            issues.append(
                ("error", "debug.simulation.contribution_range 应为 [min, max] 且 min <= max")
            )
        elif cr[0] > cr[1]:
            issues.append(
                ("error", "debug.simulation.contribution_range 应为 [min, max] 且 min <= max")
            )

    # 日志级别:必须为 1~6 的整数
    lv = section(cfg, "log").get("level")
    if lv is not None:
        if not isinstance(lv, int) or isinstance(lv, bool) or lv < 1 or lv > 6:
            issues.append(("error", f"log.level 应为 1~6 的整数，当前: {lv!r}"))

    # 异步配置校验
    async_cfg = section(cfg, "async")
    if not isinstance(async_cfg.get("enabled"), bool):
        issues.append(("error", "async.enabled 应为布尔值"))
    if not isinstance(async_cfg.get("num_workers"), int) or async_cfg.get("num_workers", 0) < 1:
        issues.append(("error", "async.num_workers 应为 >=1 的整数"))
    if (
        not isinstance(async_cfg.get("max_queue_size"), int)
        or async_cfg.get("max_queue_size", 0) < 1
    ):
        issues.append(("error", "async.max_queue_size 应为 >=1 的整数"))

    # game_log 防呆:不得指向脚本自身的日志目录(读自己写的日志 → 解析器被脚本输出污染、
    # 归档互相触发,形成自读自写死循环)
    gl = str(cfg.get("game_log") or "")
    if gl:
        log_dir_cfg = str(section(cfg, "log").get("dir", "logs"))
        try:
            gl_abs = os.path.abspath(gl)
            own_logs_abs = os.path.abspath(log_dir_cfg)
            if os.path.normcase(gl_abs).startswith(
                os.path.normcase(own_logs_abs) + os.sep
            ) or os.path.normcase(gl_abs) == os.path.normcase(own_logs_abs):
                issues.append(
                    (
                        "error",
                        f"game_log 指向了脚本自身的日志目录({log_dir_cfg}),"
                        f"会导致脚本读取自己产生的日志。请改为游戏客户端的 latest.log 路径",
                    )
                )
            else:
                # 同文件精确比对兜底(处理相对/绝对路径写法不同但指向同一文件的情况):
                # 脚本运行目录下的 logs/latest.log
                script_latest = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), log_dir_cfg, "latest.log"
                )
                if os.path.normcase(gl_abs) == os.path.normcase(script_latest):
                    issues.append(
                        (
                            "error",
                            "game_log 指向了脚本自己的 logs/latest.log,"
                            "请改为游戏客户端日志路径"
                            "(如 C:\\Path\\To\\Minecraft\\logs\\latest.log)",
                        )
                    )
        except (OSError, ValueError):
            pass  # 路径解析失败不阻塞校验(后续打开日志时会自然报错)

    return issues
