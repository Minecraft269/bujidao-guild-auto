# -*- coding: utf-8 -*-
"""
config.py —— 配置模块
=======================
职责:默认配置定义、config.json 自动生成、加载/深合并、校验、命令行覆盖。
所有业务参数(阈值/文案/命令格式/延时/开关)均在此定义默认值,除配置文件外零硬编码。

每个配置键的中文注释说明见《配置说明.md》(与 config.json 同级)。
"""
import json
import os
import sys

# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------
# 注释约定:每行中文说明键的作用与取值范围;所有业务规则继承自原脚本(见 docs/业务规则继承清单.md)
DEFAULT_CONFIG = {
    # ── 人员信息 ────────────────────────────────────────────────────────────
    # 管理员名:当前执行操作的人,用于报告与通知标注(原脚本公告尾部"若有异议联系群管理员<ID>")
    # 留空=启动时自动从游戏日志的 "--username, <ID>" 启动参数中识别;填写后以填写值为准
    "admin": "",
    # 公会 QQ 群号:通知文案尾部"公会群:<号>"(原脚本硬编码 YOUR_QQ_GROUP)
    "guild_qq": "YOUR_QQ_GROUP",

    # ── 游戏文件路径 ────────────────────────────────────────────────────────
    # 游戏日志路径:网易版客户端 logs/latest.log,日志钩子增量读取
    "game_log": r"C:\Path\To\Minecraft\logs\latest.log",
    # 游戏配置文件路径:Minecraft options.txt,用于 auto 模式读取聊天键(key_key.chat)
    "game_options": r"C:\Path\To\Minecraft\heypixel\options.txt",
    # 日志编码:auto=自动检测(依次尝试 utf-8/gbk/gb2312);可指定 "utf-8"/"gbk"/"gb2312"
    "log_encoding": "auto",

    # ── 窗口与按键 ──────────────────────────────────────────────────────────
    # 游戏窗口标题关键词(任一命中即视为游戏窗口;大小写不敏感;首个命中优先)
    "window_keywords": ["Minecraft", "布吉岛", "HeyPixel", "heypixel"],
    # 聊天栏打开按键:auto=读 options.txt 的 key_key.chat 自动映射;也可直接写 pyautogui 键名
    # (如 "t"/"enter"/"space"),或中文别名(如 "回车"/"空格"/"esc")
    "chat_key": "auto",
    # 暂停键:运行中随时暂停/继续(pynput 键名,如 "u"/"p";键盘全局监听)
    "pause_key": "u",
    # 公会名:留空=运行时从 /guild list 输出自动识别(推荐);也可手动指定以校验
    "guild_name": "",

    # ── 查询范围 ────────────────────────────────────────────────────────────
    # 逐人查询范围:"all"=全员(会长/管理员除外);或仅查指定等级:
    # "high"(高活跃)/"mid"(中等活跃)/"low"(低活跃)/"entry"(入门)
    "query_scope": "all",
    # 踢出确认:true=执行踢出前在控制台列出清单,输入 kick-confirm 才执行(踢出不可逆,建议保持 true)
    "kick_confirm": True,
    # 跳过职位:默认跳过 会长/副会长/管理员(不查询不操作,与原脚本 skip_groups 一致);
    # 如需查询某职位,将其从列表移除即可
    "skip_ranks": ["leader", "vice_leader", "admin"],

    # ── 升/降/踢 阈值(原样继承自原脚本,勿改动数值) ────────────────────────
    # 取值:非负整数,单位=公会经验周贡献。语义与边界(含/不含)见 docs/业务规则继承清单.md §1
    "thresholds": {
        # 高活跃:周贡献 <= high_demote → 降职(原 run_auto.py,含边界)
        "high_demote": 30000,
        # 中等活跃:周贡献 >= mid_promote → 升职(原 run_mid.py,含边界)
        "mid_promote": 30000,
        # 中等活跃:周贡献 <  mid_demote → 降职(原 run_mid.py,不含边界)
        "mid_demote": 5000,
        # 低活跃:周贡献 >= low_promote_2 → 升职×2;>= low_promote_1 → 升职×1
        "low_promote_2": 30000,
        "low_promote_1": 10000,
        # 低活跃:周贡献 <  low_demote → 降职(不含边界)
        "low_demote": 3500,
        # 入门:周贡献 >= entry_promote_3 → 升职×3;>= entry_promote_2 → 升职×2;>= entry_promote_1 → 升职×1
        "entry_promote_3": 30000,
        "entry_promote_2": 10000,
        "entry_promote_1": 3500,
        # 入门:周贡献 <  entry_kick → 踢出(不含边界)
        "entry_kick": 3500,
    },

    # ── 模拟人工延迟(秒,随机范围) ──────────────────────────────────────────
    "delays": {
        # 命令间最小/最大间隔:相邻两条游戏命令的随机等待(防频率检测)
        "min_send_interval": 1.5,
        "max_send_interval": 3.0,
        # 粘贴后到按回车的最小/最大延迟
        "min_paste_to_enter": 0.15,
        "max_paste_to_enter": 0.4,
        # 按聊天键后的最小/最大延迟(等输入框打开)
        "min_chat_key_delay": 0.3,
        "max_chat_key_delay": 0.6,
    },

    # ── 超时与重试(秒) ─────────────────────────────────────────────────────
    "timeouts": {
        # 发送命令后等待日志出现响应块的最长秒数(服务器慢可调大,如 20)
        "response_wait": 12.0,
        # 试探式发送的首次等待(秒):直接粘贴+回车后若聊天栏未开则无响应,
        # 用此短超时快速判断并重试(重试时聊天栏已打开,发送成功)。建议 2~5
        "try_wait": 3.0,
        # 试探式发送的最大尝试次数(每次=粘贴+回车;聊天栏未开/菜单场景需 2~3 次)
        "try_times": 3,
        # 单个玩家查询失败后的最大重试次数(原脚本 MAX_RETRIES=10,重构默认 3)
        "max_retries": 3,
        # 日志轮询间隔(毫秒):读 latest.log 新行的频率
        "poll_interval_ms": 300,
        # 执行命令后错误回显检测窗口(秒):慢服务器可调大(如 6)
        "error_check_window": 4.0,
    },

    # ── 功能开关 ────────────────────────────────────────────────────────────
    "switches": {
        # 升职开关:false=即使满足条件也不执行 promote
        "promote_enabled": True,
        # 降职开关:false=不执行 demote
        "demote_enabled": True,
        # 踢出开关:false=不执行 kick(建议谨慎,踢出不可逆)
        "kick_enabled": True,
        # 执行开关(dry-run):false=仅查询+通知+输出报告,不执行任何升/降/踢
        "execution_enabled": True,
        # 通知开关:false=不发送 /gc 广播
        "notify_enabled": True,
        # 通知模式:"summary"=查询完成后发一条汇总广播(默认);"per_player"=逐人发送(含无操作玩家)
        "notify_mode": "summary",
        # 详细日志开关:true=控制台打印每条查询/解析细节
        "verbose_log": True,
    },

    # ── 界面状态检测(截图 + Windows OCR) ────────────────────────────────────
    "state_check": {
        # 检测开关:false=跳过检测,始终按聊天键发送。需 winsdk 与系统中文 OCR 语言包
        "enabled": True,
        # 菜单界面关键词:识别到任一即认为游戏处于菜单界面,发送前自动按 Esc 关闭
        # (OCR 输出可能逐字带空格,匹配时会自动去除空白)
        "menu_keywords": ["回到游戏", "断开连接", "断开链接", "保存并退出", "暂停菜单"],
        # 聊天栏状态处理:
        #   "auto"=用下方 chat_bar_keywords 检测(留空=不检测,视为未打开,按聊天键发送);
        #   "open"=手动告知聊天栏已打开,直接粘贴发送(不按聊天键,防止误发输入框残留内容);
        #   "closed"=始终按聊天键打开再发送
        "chat_bar_state": "auto",
        # 聊天栏输入框特征文字(不同客户端可能不同;命中任一即认为聊天栏已打开)。
        # 多数网易版客户端输入框无提示文字,默认留空即可
        "chat_bar_keywords": [],
        # OCR 结果缓存秒数:发送前用缓存结果判断,避免每条命令都截图识别(截图耗时约 0.5~1 秒)
        "cache_seconds": 3.0,
    },

    # ── 命令格式(原样继承原脚本) ───────────────────────────────────────────
    "commands": {
        "guild_list": "/guild list",
        "guild_member": "/guild member {player}",
        "guild_promote": "/guild promote {player}",
        "guild_demote": "/guild demote {player}",
        "guild_kick": "/guild kick {player} {reason}",
        # 踢出原因(原脚本固定文案)
        "kick_reason": "未完成一周最低标准",
        # 公会聊天广播前缀
        "gc_prefix": "/gc",
    },

    # ── 通知文案模板(原样继承原脚本,{player}/{weekly}/{count}/{guild_qq} 为占位符) ──
    "notify_templates": {
        # 无操作
        "none": "{player} 这周贡献为:{weekly} 执行操作:无 非常感谢你对公会做出的贡献 公会群:{guild_qq}",
        # 升职(count=次数)
        "promote": "{player} 这周贡献为:{weekly} 执行操作:升职({count}次) 非常感谢你对公会做出的贡献 公会群:{guild_qq}",
        # 降职
        "demote": "{player} 这周贡献为:{weekly} 执行操作:降职 若有异议请去群里找执行此操作的管理员 公会群:{guild_qq}",
        # 踢出
        "kick": "{player} 这周贡献为:{weekly} 执行操作:踢出 未完成这周最低标准 若有异议请去群里寻找执行管理员 公会群:{guild_qq}",
        # 汇总广播(summary 模式,查询完成后、执行前发送)
        "summary": "本周公会清人结果:升职 {promote} 人,降职 {demote} 人,踢出 {kick} 人,执行人:{admin},详情见群文件",
    },

    # ── 等级显示名(与游戏 /guild list 分组标题对应) ────────────────────────
    # 注:副会长(vice_leader)按原脚本设计默认跳过(见 skip_ranks);如需处理可从 skip_ranks 移除
    "rank_names": {
        "leader": "会长",
        "vice_leader": "副会长",
        "admin": "管理员",
        "high": "高活跃成员",
        "mid": "中等活跃成员",
        "low": "低活跃成员",
        "entry": "入门成员",
    },

    # ── 输出 ────────────────────────────────────────────────────────────────
    # 报告输出目录(相对脚本运行目录;自动创建)
    "report_dir": "reports",
    # 公告尾部联系人文案(原脚本"若有异议联系群管理员演示管理员")
    "contact_text": "若有异议联系群管理员{admin}",

    # ── 执行错误检测(日志中出现任一关键词视为命令执行失败) ─────────────────
    "error_patterns": [
        "错误的参数用法",
        "不存在该玩家",
        "玩家不存在",
        "没有这个玩家",
        "权限不足",
        "无权限",
    ],

    # ── Debug 模式 ────────────────────────────────────────────────────────────
    "debug": {
        # 是否启用 debug 模式（跳过真实查询，使用模拟数据）
        "enabled": False,
        # 跳过的阶段列表：可选 "list"、"member"、"decide"、"notify"、"execute"
        "skip_stages": ["list", "member"],
        # 模拟数据配置
        "simulation": {
            # 模拟公会名
            "guild_name": "Debug公会",
            # 每个等级生成的玩家数量：可以是整数（所有等级统一），或对象（如 {"high":3, "mid":2, ...}）
            # 对象中的键必须是 rank_names 中定义的等级，缺失的等级默认使用 1
            "players_per_rank": 3,
            # 周贡献随机范围（最小值，最大值），若未指定则使用 [0, 50000]
            "contribution_range": [0, 50000],
        }
    },

    # ── 异步执行 ────────────────────────────────────────────────────────────
    "async": {
        # 是否启用异步执行:true=启用,fals=串行执行(调试建议关闭)
        "enabled": True,
        # 后台执行线程数:固定 1——游戏聊天命令经模拟键盘发送,
        # 并发会交叉输入导致命令错乱;"逐一读取验证"即单线程串行
        "num_workers": 1,
        # 命令队列最大容量:满时丢弃后续命令
        "max_queue_size": 100,
        # 验证失败最大重试次数(重试耗尽的任务进入补做队列)
        "max_retries": 3,
        # 重试间隔(秒)
        "retry_delay": 1.0,
    },

    # ── 日志 ────────────────────────────────────────────────────────────────
    "log": {
        # 是否启用日志文件
        "enabled": True,
        # 日志级别:数字越大越详细,每级包含上一级(更严重)的全部
        # 1=致命(启动失败/意外关闭) 2=严重错误 3=错误 4=信息(默认) 5=调试 6=追踪
        "level": 4,
        # 日志目录(相对脚本运行目录,自动创建)
        "dir": "logs",
    }
}

# 生成默认配置文件时,每个键插入的 "// 注释" 键(JSONC 风格,加载时自动递归过滤)。
# 键路径用 '.' 连接(如 "thresholds.high_demote");注释内容与 DEFAULT_CONFIG 逐键注释一致(作用+取值范围)。
CONFIG_COMMENTS = {
    # ── 人员信息 ────────────────────────────────────────────────────────────
    "admin": "管理员名:当前执行操作的人,用于报告与通知标注(原脚本公告尾部'若有异议联系群管理员<ID>')。"
             "留空=启动时自动从游戏日志的 --username, <ID> 启动参数中识别;填写后以填写值为准",
    "guild_qq": "公会 QQ 群号:通知文案尾部'公会群:<号>'(原脚本硬编码 YOUR_QQ_GROUP)",
    # ── 游戏文件路径 ────────────────────────────────────────────────────────
    "game_log": "游戏日志路径:网易版客户端 logs/latest.log,日志钩子增量读取。取正在运行的客户端的日志",
    "game_options": "游戏配置文件路径:Minecraft options.txt,用于 auto 模式读取聊天键(key_key.chat)",
    "log_encoding": "日志编码:auto=自动检测(依次尝试 utf-8/gbk/gb2312);或手动指定 'utf-8'/'gbk'/'gb2312'。"
                    "网易版客户端日志通常为 GB2312",
    # ── 窗口与按键 ──────────────────────────────────────────────────────────
    "window_keywords": "游戏窗口标题关键词(任一命中即视为游戏窗口;大小写不敏感;首个命中优先)",
    "chat_key": "聊天栏打开按键:auto=读 options.txt 的 key_key.chat 自动映射(本机通常为回车);"
                "也可直接写 pyautogui 键名(如 't'/'enter'/'space'),或中文别名(如 '回车'/'空格'/'esc')",
    "pause_key": "暂停键:运行中随时暂停/继续(pynput 键名,如 'u'/'p';键盘全局监听)",
    "guild_name": "公会名:留空=运行时从 /guild list 输出自动识别(推荐);也可手动指定以校验",
    # ── 查询范围 ────────────────────────────────────────────────────────────
    "query_scope": "逐人查询范围:'all'=全员(会长/管理员除外);或仅查指定等级:"
                   "'high'(高活跃)/'mid'(中等活跃)/'low'(低活跃)/'entry'(入门)",
    "skip_ranks": "跳过的职位(不查询不操作):'leader'(会长)/'vice_leader'(副会长)/'admin'(管理员);如需查询请从列表移除",
    "kick_confirm": "踢出确认:true=执行踢出前在控制台列出清单,输入 kick-confirm 才执行(踢出不可逆,建议保持 true)",
    # ── 升/降/踢 阈值(原样继承自原脚本,勿改动数值) ────────────────────────
    "thresholds": "升/降/踢阈值:取值非负整数,单位=公会经验周贡献。语义与边界(含/不含)见 docs/业务规则继承清单.md §1",
    "thresholds.high_demote": "高活跃:周贡献 <= 此值 → 降职(原 run_auto.py,含边界)",
    "thresholds.mid_promote": "中等活跃:周贡献 >= 此值 → 升职(原 run_mid.py,含边界)",
    "thresholds.mid_demote": "中等活跃:周贡献 < 此值 → 降职(原 run_mid.py,不含边界)",
    "thresholds.low_promote_2": "低活跃:周贡献 >= 此值 → 升职×2",
    "thresholds.low_promote_1": "低活跃:周贡献 >= 此值 → 升职×1",
    "thresholds.low_demote": "低活跃:周贡献 < 此值 → 降职(不含边界)",
    "thresholds.entry_promote_3": "入门:周贡献 >= 此值 → 升职×3",
    "thresholds.entry_promote_2": "入门:周贡献 >= 此值 → 升职×2",
    "thresholds.entry_promote_1": "入门:周贡献 >= 此值 → 升职×1",
    "thresholds.entry_kick": "入门:周贡献 < 此值 → 踢出(不含边界)",
    # ── 模拟人工延迟(秒,随机范围) ──────────────────────────────────────────
    "delays": "模拟人工延迟(秒,随机范围):相邻命令与按键间随机等待,防频率检测",
    "delays.min_send_interval": "命令间最小间隔:相邻两条游戏命令的随机等待下限。建议 0.5~5",
    "delays.max_send_interval": "命令间最大间隔:随机等待上限(须 >= min)。建议 1~8",
    "delays.min_paste_to_enter": "粘贴后到按回车的最小延迟(秒)",
    "delays.max_paste_to_enter": "粘贴后到按回车的最大延迟(秒)",
    "delays.min_chat_key_delay": "按聊天键后的最小延迟(秒,等输入框打开)",
    "delays.max_chat_key_delay": "按聊天键后的最大延迟(秒)",
    # ── 超时与重试(秒) ─────────────────────────────────────────────────────
    "timeouts": "超时与重试(秒):发命令→等响应→超时处理",
    "timeouts.response_wait": "发送命令后等待日志出现响应块的最长秒数(服务器慢可调大,如 20)",
    "timeouts.try_wait": "试错式发送的首次等待(秒):直接粘贴+回车后若聊天栏未开则无响应,"
                         "用此短超时快速判断并重试(重试时聊天栏已打开,发送成功)。建议 2~5",
    "timeouts.try_times": "试错式发送的最大尝试次数(每次=粘贴+回车;聊天栏未开/菜单界面场景需 2~3 次)",
    "timeouts.max_retries": "单个玩家查询失败后的最大重试次数(原脚本 MAX_RETRIES=10,重构默认 3;建议 2~5)",
    "timeouts.poll_interval_ms": "日志轮询间隔(毫秒):读 latest.log 新行的频率",
    "timeouts.error_check_window": "执行命令后错误回显检测窗口(秒):发送后收集该时长内日志,命中 error_patterns 判定失败;"
                                   "服务器响应慢可调大(如 6)",
    # ── 异步执行 ────────────────────────────────────────────────────────────
    "async": "异步执行配置:多线程后台执行命令,加快查询+操作流程",
    "async.enabled": "启用开关:true=启用异步执行,fals=串行执行(调试建议关闭)",
    "async.num_workers": "后台线程数:每个线程独立轮询日志验证命令结果，默认 2",
    "async.max_queue_size": "命令队列最大容量:满时丢弃后续命令，默认 100",
    # ── 功能开关 ────────────────────────────────────────────────────────────
    "switches": "功能开关:每类操作可单独开关;执行开关 false=dry-run(仅查询+通知+输出报告)",
    "switches.promote_enabled": "升职开关:false=即使满足条件也不执行 promote",
    "switches.demote_enabled": "降职开关:false=不执行 demote",
    "switches.kick_enabled": "踢出开关:false=不执行 kick(建议谨慎,踢出不可逆)",
    "switches.execution_enabled": "执行开关(dry-run):false=仅查询+通知+输出报告,不执行任何升/降/踢。命令行 --dry-run 等价",
    "switches.notify_enabled": "通知开关:false=不发送 /gc 广播",
    "switches.notify_mode": "通知模式:'summary'=查询完成后发一条汇总广播;'per_player'=逐人按原文案发送",
    "switches.verbose_log": "详细日志开关:true=控制台打印每条查询/解析细节",
    # ── 界面状态检测(截图 + Windows OCR) ───────────────────────────────────
    "state_check": "界面状态检测(截图+Windows OCR):识别游戏菜单界面并自动按 Esc 关闭;"
                    "需 winsdk 与系统中文 OCR 语言包,不可用时自动降级为纯试错式发送",
    "state_check.enabled": "检测开关:false=跳过检测(始终按聊天键发送)",
    "state_check.menu_keywords": "菜单界面关键词:OCR 识别到任一即认为游戏处于菜单界面,发送前自动按 Esc 关闭",
    "state_check.chat_bar_state": "聊天栏状态处理:'auto'=用下方 chat_bar_keywords 检测(留空=不检测,视为未打开);"
                                  "'open'=手动告知聊天栏已打开,直接粘贴发送(不按聊天键);'closed'=始终按聊天键打开再发送",
    "state_check.chat_bar_keywords": "聊天栏输入框特征文字(不同客户端可能不同;命中任一即认为聊天栏已打开)。"
                                     "多数网易版客户端输入框无提示文字,默认留空即可",
    "state_check.cache_seconds": "OCR 结果缓存秒数:发送前用缓存结果判断,避免每条命令都截图识别(截图耗时约 0.5~1 秒)",

    # ── 命令格式(原样继承原脚本) ───────────────────────────────────────────
    "commands": "命令格式(原样继承原脚本,勿改格式)。{player}/{reason} 为占位符",
    "commands.guild_list": "查列表命令",
    "commands.guild_member": "查成员命令,{player} 为占位符",
    "commands.guild_promote": "升职命令(按次数重复发送)",
    "commands.guild_demote": "降职命令",
    "commands.guild_kick": "踢出命令,{player}/{reason} 为占位符",
    "commands.kick_reason": "踢出原因(原脚本固定文案)",
    "commands.gc_prefix": "公会聊天广播前缀",
    # ── 通知文案模板(原样继承原脚本) ───────────────────────────────────────
    "notify_templates": "通知文案模板(原样继承原脚本)。占位符:{player}/{weekly}/{count}/{guild_qq}",
    "notify_templates.none": "无操作通知(原脚本文案)",
    "notify_templates.promote": "升职通知,count=升职次数(原脚本文案)",
    "notify_templates.demote": "降职通知(原脚本文案)",
    "notify_templates.kick": "踢出通知(原脚本文案)",
    "notify_templates.summary": "汇总广播(summary 模式,查询完成后、执行前发送;{promote}/{demote}/{kick}/{admin} 为占位符)",
    # ── 等级显示名(与游戏 /guild list 分组标题对应) ────────────────────────
    "rank_names": "等级显示名(与游戏 /guild list 分组标题对应);游戏输出格式变更时在此调整",
    "rank_names.leader": "会长(默认跳过,见 skip_ranks)",
    "rank_names.vice_leader": "副会长(默认跳过,见 skip_ranks;如需处理请从 skip_ranks 移除)",
    "rank_names.admin": "管理员",
    "rank_names.high": "高活跃成员",
    "rank_names.mid": "中等活跃成员",
    "rank_names.low": "低活跃成员",
    "rank_names.entry": "入门成员",
    # ── 输出与错误检测 ──────────────────────────────────────────────────────
    "report_dir": "报告输出目录(相对脚本运行目录;自动创建)",
    "contact_text": "公告尾部联系人文案(原脚本'若有异议联系群管理员演示管理员'的风格),{admin} 为占位符",
    "error_patterns": "执行错误检测:日志中出现任一关键词视为命令执行失败(如'错误的参数用法'/'权限不足')",
    # ── Debug ────────────────────────────────────────────────────────────
    "debug": "Debug 模式 （仅调试/测试时使用）",
    "debug.enabled": "Debug 模式开关",
    "debug.skip_stages": "跳过的阶段列表：可选 \"list\"、\"member\"、\"decide\"、\"notify\"、\"execute\" （仅在开启Debug 模式时才生效）默认值: \"list\", \"member\"",
    "debug.simulation": "模拟数据 （用于模拟被跳过数据获取阶段的数据 仅在开启Debug模式生效）",
    "debug.simulation.guild_name": "模拟公会名数据",
    "debug.simulation.players_per_rank": "每个等级生成的玩家数量：整数（所有等级统一）或对象（各等级单独指定，如 {\"high\":3,\"mid\":2}），缺失的等级默认为 1",
    "debug.simulation.contribution_range": "模拟周贡献值的周贡献随机范围（最小值，最大值），若未指定则默认使用 [0, 50000]",
    "log": "日志系统：写入日志目录下的 latest.log；每次启动自动归档旧日志为按天压缩包 app-YYYY-MM-DD.gz",
    "log.enabled": "是否启用日志文件（true=记录到文件，false=不写文件）",
    "log.level": "日志级别：数字越大越详细，每级包含上一级更严重的全部。1=致命(启动失败/意外关闭) 2=严重错误 3=错误 4=信息(默认) 5=调试 6=追踪",
    "log.dir": "日志目录（相对脚本运行目录，自动创建）"
}

# 中文键名别名(聊天键/暂停键配置可直接用中文)
KEY_ALIASES = {
    "回车": "enter",
    "空格": "space",
    "esc": "esc",
    "esc键": "esc",
    "退出": "esc",
    "tab": "tab",
    "制表键": "tab",
    "回车键": "enter",
}

VALID_SCOPES = ("all", "high", "mid", "low", "entry")


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _deep_merge(base, override):
    """递归合并:override 中的键覆盖 base,保留 base 中 override 没有的键。"""
    result = dict(base)
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(base[key], value)
        else:
            result[key] = value
    return result


def _dump_jsonc(obj, indent=0, prefix=""):
    """把配置 dict 输出为 JSONC 文本:每个键前插入 "// 注释" 注释行(无引号,行首 //)。
    返回嵌套内容的行串(不含外层大括号);注释行不参与 JSON 结构,加载时按行首剥离。"""
    pad = " " * indent
    out = []
    if isinstance(obj, dict):
        keys = list(obj.keys())
        for i, k in enumerate(keys):
            path = f"{prefix}.{k}" if prefix else k
            comment = CONFIG_COMMENTS.get(path)
            if comment:
                out.append(f"{pad}  // {comment}")
            v = obj[k]
            if isinstance(v, dict):
                out.append(f'{pad}  "{k}": {{')
                out.append(_dump_jsonc(v, indent + 4, path))
                out.append(f"{pad}  }}" + ("," if i < len(keys) - 1 else ""))
            else:
                out.append(f'{pad}  "{k}": {json.dumps(v, ensure_ascii=False)}'
                            + ("," if i < len(keys) - 1 else ""))
    return "\n".join(out)


def _strip_comment_keys(node):
    """递归删除 '//' 与 '_' 开头的键(兼容旧版生成的"注释即数据键"格式;
    新版为行首 // 注释行,加载前已剥离,此处过滤对新格式无副作用)。"""
    if isinstance(node, dict):
        return {k: _strip_comment_keys(v) for k, v in node.items()
                if not (k.startswith("//") or k.startswith("_"))}
    if isinstance(node, list):
        return [_strip_comment_keys(v) for v in node]
    return node


def _load_jsonc_text(path):
    """读取 JSONC 配置文件:剥离行首 '//' 注释行后按 JSON 解析。"""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    lines = [l for l in text.splitlines() if not l.lstrip().startswith("//")]
    return json.loads("\n".join(lines))


def ensure_default_config(path):
    """若配置文件不存在,写入默认配置(JSONC 格式)。
    生成的文件每个配置键前有一行 "// 中文注释"(无引号,含作用与取值范围),便于直接阅读;
    加载时自动剥离注释行,不影响 JSON 解析。"""
    if os.path.exists(path):
        return False
    header = ("// 布吉岛公会管理脚本配置文件。每个配置键前有 '//' 中文注释行,说明其作用与取值范围;"
                "详细说明见同目录《配置说明.md》。修改后保存即可,重启脚本生效。")
    text = "{\n" + _dump_jsonc(DEFAULT_CONFIG, 2) + "\n}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + "\n" + text + "\n")
    return True


def load_config(path):
    """读取配置文件,缺失键与默认配置深合并(向前兼容旧配置文件)。
    支持 JSONC(行首 // 注释)与标准 JSON 两种格式。"""
    if not os.path.exists(path):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    user_cfg = _strip_comment_keys(_load_jsonc_text(path))
    merged = _deep_merge(DEFAULT_CONFIG, user_cfg)
    return merged


def save_config(path, cfg):
    """保存配置(UTF-8、缩进 2、保留中文)。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


def validate_config(cfg):
    """校验配置,返回问题列表(每条为 (级别, 消息);级别: error/warning)。"""
    issues = []

    def check_num(d, key, path, minimum=0):
        val = d.get(key)
        # 排除 bool(bool 是 int 子类,true/false 不应通过数值校验)
        if isinstance(val, bool) or not isinstance(val, (int, float)) or val < minimum:
            issues.append(("error", f"配置项 {path}.{key} 应为不小于 {minimum} 的数字,当前: {val!r}"))

    th = cfg.get("thresholds", {})
    for k in th:
        check_num(th, k, "thresholds")

    d = cfg.get("delays", {})
    for k in ("min_send_interval", "max_send_interval"):
        check_num(d, k, "delays")
    if d.get("min_send_interval", 0) > d.get("max_send_interval", 0):
        issues.append(("error", "delays.min_send_interval 不能大于 max_send_interval"))

    t = cfg.get("timeouts", {})
    for k in ("response_wait", "max_retries"):
        check_num(t, k, "timeouts")
    if t.get("max_retries", 0) < 0:
        issues.append(("error", "timeouts.max_retries 不能为负"))

    scope = cfg.get("query_scope", "all")
    if scope not in VALID_SCOPES:
        issues.append(("error", f"query_scope 取值非法: {scope!r},应为 {VALID_SCOPES} 之一"))

    for rank in cfg.get("skip_ranks", []):
        if rank not in cfg.get("rank_names", {}):
            issues.append(("warning", f"skip_ranks 中未知等级: {rank!r}"))

    for key, tmpl in cfg.get("notify_templates", {}).items():
        if not isinstance(tmpl, str) or not tmpl.strip():
            issues.append(("error", f"notify_templates.{key} 不能为空"))

    for cmd in cfg.get("commands", {}).values():
        if not isinstance(cmd, str) or not cmd.strip():
            issues.append(("error", f"commands 中存在空命令: {cmd!r}"))

    if cfg.get("chat_key") != "auto" and not isinstance(cfg.get("chat_key"), str):
        issues.append(("error", "chat_key 应为字符串('auto' 或键名)"))

    # debug skip_stages 合法性
    skip = cfg.get("debug", {}).get("skip_stages", [])
    valid_stages = {"list", "member", "decide", "notify", "execute"}
    for stage in skip:
        if stage not in valid_stages:
            issues.append(("error", f"debug.skip_stages 中无效阶段: {stage}，应为 {valid_stages} 之一"))

    # 校验 players_per_rank
    ppr = cfg.get("debug", {}).get("simulation", {}).get("players_per_rank")
    if ppr is not None:
        if isinstance(ppr, dict):
            rank_names = cfg.get("rank_names", {})
            for k, v in ppr.items():
                if k not in rank_names:
                    issues.append(("warning", f"debug.simulation.players_per_rank 中未知等级: {k!r}"))
                if not isinstance(v, int) or v < 0:
                    issues.append(("error", f"debug.simulation.players_per_rank[{k}] 应为非负整数，当前: {v!r}"))
        elif not isinstance(ppr, int) or ppr < 0:
            issues.append(("error", f"debug.simulation.players_per_rank 应为整数或对象，当前: {ppr!r}"))

    # 贡献范围必须为长度为2的列表且 min <= max
    cr = cfg.get("debug", {}).get("simulation", {}).get("contribution_range")
    if cr is not None:
        if not isinstance(cr, list) or len(cr) != 2 or cr[0] > cr[1]:
            issues.append(("error", "debug.simulation.contribution_range 应为 [min, max] 且 min <= max"))

    # 日志级别:必须为 1~6 的整数
    lv = cfg.get("log", {}).get("level")
    if lv is not None:
        if not isinstance(lv, int) or isinstance(lv, bool) or lv < 1 or lv > 6:
            issues.append(("error", f"log.level 应为 1~6 的整数，当前: {lv!r}"))

    # 异步配置校验
    async_cfg = cfg.get("async", {})
    if not isinstance(async_cfg.get("enabled"), bool):
        issues.append(("error", "async.enabled 应为布尔值"))
    if not isinstance(async_cfg.get("num_workers"), int) or async_cfg.get("num_workers", 0) < 1:
        issues.append(("error", "async.num_workers 应为 >=1 的整数"))
    if not isinstance(async_cfg.get("max_queue_size"), int) or async_cfg.get("max_queue_size", 0) < 1:
        issues.append(("error", "async.max_queue_size 应为 >=1 的整数"))

    # game_log 防呆:不得指向脚本自身的日志目录(读自己写的日志 → 解析器被脚本输出污染、
    # 归档互相触发,形成自读自写死循环)
    gl = str(cfg.get("game_log") or "")
    if gl:
        log_dir_cfg = str(cfg.get("log", {}).get("dir", "logs"))
        try:
            gl_abs = os.path.abspath(gl)
            own_logs_abs = os.path.abspath(log_dir_cfg)
            if os.path.normcase(gl_abs).startswith(os.path.normcase(own_logs_abs) + os.sep) \
                    or os.path.normcase(gl_abs) == os.path.normcase(own_logs_abs):
                issues.append((
                    "error",
                    f"game_log 指向了脚本自身的日志目录({log_dir_cfg}),"
                    f"会导致脚本读取自己产生的日志。请改为游戏客户端的 latest.log 路径",
                ))
            else:
                # 同文件精确比对兜底(处理相对/绝对路径写法不同但指向同一文件的情况):
                # 脚本运行目录下的 logs/latest.log
                script_latest = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), log_dir_cfg, "latest.log")
                if os.path.normcase(gl_abs) == os.path.normcase(script_latest):
                    issues.append((
                        "error",
                        "game_log 指向了脚本自己的 logs/latest.log,"
                        "请改为游戏客户端日志路径"
                        "(如 C:\\Path\\To\\Minecraft\\logs\\latest.log)",
                    ))
        except (OSError, ValueError):
            pass  # 路径解析失败不阻塞校验(后续打开日志时会自然报错)

    return issues


def apply_cli_overrides(cfg, args):
    """命令行参数覆盖配置(--admin/--log/--options/--config 已在 main 处理路径,这里覆盖其余)。"""
    if getattr(args, "admin", None):
        cfg["admin"] = args.admin
    if getattr(args, "log", None):
        cfg["game_log"] = args.log
    if getattr(args, "options", None):
        cfg["game_options"] = args.options
    return cfg


def console(msg, level="info"):
    """控制台输出(兼容 GBK 控制台,避免编码崩溃);同时按级别写入日志文件。

    level: "critical"/"error"/"warning"/"info"/"debug"/"trace"(默认 info)。
    logger 未初始化或已禁用时仅打印控制台,不写文件。"""
    try:
        print(msg)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))
    try:
        import logger as _logger_mod
        getattr(_logger_mod, f"log_{level}", _logger_mod.log_info)(str(msg))
    except Exception:
        pass  # 日志系统不可用时不影响控制台输出
