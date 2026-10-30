# -*- coding: utf-8 -*-
"""
config/defaults.py —— 默认值与中文注释(单一真源)
================================================
本模块是**唯一**允许出现业务默认值的地方:阈值、文案模板、命令格式、
等级显示名、QQ 群号占位符等全部在此定义。业务代码一律从配置对象读取,
缺键抛 KeyError,不提供兜底串(硬编码兜底会悄悄覆盖用户修改)。

每个键都必须在 CONFIG_COMMENTS 里有中文说明,内容包含两部分:
  1. 作用 —— 这个键控制什么行为
  2. 可填值 —— 取值范围 / 合法值 / 示例
生成的 config.json 是 JSONC 格式,每个键上方有一行 // 注释。
"""

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
    # ── 暂停与自动暂停(US-009)────────────────────────────────────────────
    "pause": {
        # UI 模式执行中检测到用户键鼠活动是否自动暂停
        "auto_on_user_input": True,
        # 模拟输入后的忽略窗口(毫秒):脚本自己按的键/移动的鼠标不得触发自动暂停
        "user_input_grace_ms": 300,
    },
    # 公会名:留空=运行时从 /guild list 输出自动识别(推荐);也可手动指定以校验
    "guild_name": "",
    # ── 查询范围 ────────────────────────────────────────────────────────────
    # 逐人查询范围:"all"=全员(会长/管理员除外);或仅查指定等级:
    # "high"(高活跃)/"mid"(中等活跃)/"low"(低活跃)/"entry"(入门)
    "query_scope": "all",
    # 踢出确认:true=执行踢出前在控制台列出清单,输入 kick-confirm 才执行(踢出不可逆,建议保持 true)
    "kick_confirm": True,
    "kick_confirm_text": "kick-confirm",
    # 跳过职位:默认跳过 会长/副会长/管理员(不查询不操作,与原脚本 skip_groups 一致);
    # 如需查询某职位,将其从列表移除即可
    "skip_ranks": ["leader", "vice_leader", "admin"],
    # ── 名单(阶段 5)────────────────────────────────────────────────────
    # 名字匹配一律**大小写不敏感**;match_wildcard=true 时支持 * 与 ? 通配。
    # 优先级(短路):功能开关 > blacklist > 屏蔽词策略 > whitelist
    #               > whitelist_protected > 阈值判定
    "blacklist": [],
    "whitelist": [],
    "whitelist_protected": [],
    "match_wildcard": True,
    # 屏蔽词玩家(名字被网易过滤成整串星号)的策略:
    #   "skip"(默认)     = 不发送查询指令,直接记 blocked(指令不可查)
    #   "query_attempt"  = 仍尝试查询(通常会超时并浪费重试,仅调试用)
    "blocked_name_policy": "skip",
    # ── 操作模式与 UI 识别(阶段 6)────────────────────────────────────────
    # 操作模式:"command"(默认)=发聊天命令 + 读日志验证;"ui"=识别游戏界面并操作界面元素
    # UI 模式需要含 vision 能力的构建;当前构建未接线时启动即报错,不会静默降级
    "operation_mode": "command",
    "vision": {
        # 快捷栏槽位:进入「装扮与个人设定」的 hotbar 槽号,可填 1~9
        "hotbar_slot": 7,
        # 扫掠停留时长(毫秒):每个停留点等 tooltip 渲染;不得小于 120(太短会漏数据)
        "stay_ms": 120,
        # 扫掠步长比例:(0, 1];1.0=恰好一格一次,<1 更密(重叠采样),>1 会漏格
        "sweep_gap_ratio": 1.0,
        # OCR 置信度下限(0~1):低于此值的停留点重扫一次再判定
        "low_confidence": 0.7,
        # 连续误按键阈值(≥1):达到后按 Esc 回退并重新全扫,避免在错误界面乱点
        "max_misclick": 3,
        # 踢出验证方式:"grid"(格位消失)/"count"(人数对账)/"time_window"(时间窗)/
        # "any"(任一交叉验证成立即可,默认);三种都不成立时标记「执行结果待确认」
        "kick_verify": "any",
        # 外部 OCR 命令(可选):写 pytesseract 之外的识别工具;{image}=截图临时文件路径
        "ocr_command": "",
        # 扫掠网格(界面结构,与分辨率相关;材质包改不了格子布局)
        "grid": {
            # 网格左上角坐标(像素)
            "origin_x": 0,
            "origin_y": 0,
            # 单个格子宽/高(像素)
            "cell_width": 36,
            "cell_height": 36,
            # 网格行列数
            "columns": 9,
            "rows": 6,
            # 预计有物品的格子数(完整性对账用;按实际界面物品格数标定)
            "expected_cells": 54,
            # 允许的缺口比例(0=一格都不能少;空槽多时可调大)
            "max_missing_ratio": 0.0,
        },
    },
    # ── UI 配置界面(阶段 3)──────────────────────────────────────────────
    # ── 自定义规则(阶段 4)──────────────────────────────────────────────
    "rules_enabled": False,
    "rules_strategy": "first_match",
    "rules": [],
    "ui": {
        # 默认开启：gui 变体双击即出界面（用户实测要求）；非 gui 构建找不到 Gui.exe 时
        # 会「明确报错 + 回落控制台」，不会静默卡住。
        "enabled": True,
        "start_mode": "normal",
        "always_on_top": False,
        "kick_confirm_mode": "dialog",
        "kick_confirm_text": "kick-confirm",
        # ── tauri 版新增（见 docs/GUI设计规范.md §3/§4/§5）──────────────────
        # 主题:auto=跟随系统(读注册表 AppsUseLightTheme)/dark/light
        "theme": "auto",
        # 标题栏:true=自绘无边框(36px,含小窗/置顶/最小化/关闭);false=原生栏+32px 工具条
        "custom_titlebar": True,
        "window": {
            # 默认窗口尺寸(规范 §3:1000×680;最小 900×620 由 GUI 侧钳制)
            "width": 1000,
            "height": 680,
            # 记住上次位置与大小;上次位置在屏幕外时回落居中
            "remember_geometry": True,
        },
        "tray": {
            # 托盘常驻开关:false=点关闭直接退出,不询问不驻留
            "enabled": True,
            # 关闭行为:ask=每次问/minimize=最小化到托盘/exit=直接退出
            "close_action": "ask",
            # 首次关窗是否询问(勾选"不再询问"后写 false,设置页可改回)
            "ask_on_close": True,
        },
    },
    # ── 自动更新(见 docs/GUI设计规范.md §8)────────────────────────────────
    "update": {
        # check=仅后台查一次/notify=弹窗让用户决定/auto=静默下载安装
        "mode": "notify",
        # latest.json 地址;留空=不做更新检查(自建发布源时再填)
        "manifest_url": "",
    },
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
    # 第三份「运行分析报告」的开关与内容范围(见 docs/详细报告规范.md)
    "report": {
        # 是否生成第三份运行分析报告;false=只出贡献报告与群公告
        "analysis_enabled": True,
        # 是否包含配置快照(阈值/开关/名单/时间参数),便于事后复现与追责
        "include_config_snapshot": True,
        # 是否包含每人每日贡献明细;成员多时关掉可显著缩短报告
        "include_daily_detail": True,
    },
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
        # 游戏在"职位不够"时给的是整句回显,短词"权限不足"匹配不到它 ——
        # 漏掉就会被当成普通超时,只报"重试耗尽",不给权限失败的告警。
        "你只能修改低于自己职位的公会成员职位",
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
        },
    },
    # ── 异步执行 ────────────────────────────────────────────────────────────
    "async": {
        # 是否启用异步(后台)模式:false=串行(**流程规格的默认模式**:
        # 逐条发送、逐条读日志验证);true=异步(主线程只发不验,验证交给后台系统)
        "enabled": False,
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
    },
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
    "pause": "暂停与自动暂停(仅 operation_mode='ui' 生效):执行中检测到用户键鼠活动即自动暂停;"
    "阶段确认等待/空闲/指令模式不触发",
    "pause.auto_on_user_input": "UI 模式执行中检测到用户键鼠活动是否自动暂停。"
    "true(默认)=任意键盘按键/鼠标移动/点击即暂停;false=只有手动按暂停键才暂停。"
    "脚本自己模拟的输入不会触发(见 user_input_grace_ms)",
    "pause.user_input_grace_ms": "模拟输入后的忽略窗口(毫秒,默认 300,可填 ≥ 0 的整数):"
    "脚本按键/移动鼠标后的这段时间内忽略键鼠事件,避免脚本自己的输入把自己暂停。"
    "机器慢、pyautogui 动作耗时长时调大(如 500)",
    "guild_name": "公会名:留空=运行时从 /guild list 输出自动识别(推荐);也可手动指定以校验",
    # ── 查询范围 ────────────────────────────────────────────────────────────
    "query_scope": "逐人查询范围:'all'=全员(会长/管理员除外);或仅查指定等级:"
    "'high'(高活跃)/'mid'(中等活跃)/'low'(低活跃)/'entry'(入门)",
    "skip_ranks": "跳过的职位(不查询不操作):'leader'(会长)/'vice_leader'(副会长)/'admin'(管理员);如需查询请从列表移除",
    "kick_confirm": "踢出确认:true=执行踢出前在控制台列出清单,输入 kick_confirm_text 指定的确认词才执行(踢出不可逆,建议保持 true)",
    "kick_confirm_text": "踢出二次确认的确认词(控制台):必须**逐字**输入该字样才执行踢出,大小写敏感、前后空格忽略。仅在 kick_confirm=true 时生效;GUI 弹窗用的是 ui.kick_confirm_text(可分别设置)",
    "blacklist": '黑名单:名单内的玩家**完全不处理**(不查询/不通知/不升降踢)。可填玩家名列表,如 ["捣乱玩家"];'
    '大小写不敏感,match_wildcard=true 时支持 * 通配(如 ["测试*"])。优先级最高(仅次于功能开关)',
    "whitelist": "白名单:非空时**只有**名单内的玩家参与升降踢判定,其余玩家一律不处理。"
    "留空 [] 表示不启用该限制。可填玩家名或通配模式(需 match_wildcard=true)",
    "whitelist_protected": "保护名单:名单内的玩家**免于降职与踢出**(升职仍然生效)。"
    "适合保护新人或管理人小号。留空 [] 表示不启用",
    "match_wildcard": "名字匹配是否启用通配符:true=支持 * 与 ? (如 '测试*' 匹配所有以'测试'开头的名字);"
    "false=只做精确匹配。两种情况都大小写不敏感",
    "blocked_name_policy": "屏蔽词玩家策略(名字被网易过滤成整串星号,身份不可读):"
    "'skip'(默认)=不发送 /guild member *** 指令,直接记为 blocked 并在报告单列;"
    "'query_attempt'=仍尝试查询(通常超时浪费重试,仅用于调试)",
    # ── 操作模式与 UI 识别(阶段 6)────────────────────────────────────────
    "operation_mode": "操作模式:'command'(默认)=发聊天命令 + 读日志验证;"
    "'ui'=识别游戏界面并操作界面元素(需含 vision 能力的构建,且需配置 OCR 适配器)。"
    "任一条件不满足时启动阶段明确报错,不会静默降级回指令模式",
    "vision": "UI 识别模式参数(仅 operation_mode='ui' 时生效):快捷栏槽位、扫掠停留时长、"
    "步长比例、OCR 置信度下限、误按阈值、踢出验证方式、外部 OCR 命令、扫掠网格",
    "vision.hotbar_slot": "进入「装扮与个人设定」的快捷栏槽位(可填 1~9,默认 7)。"
    "实际按键在运行时从 options.txt 的 key_key.hotbar.<槽位> 读取,不内置任何默认键值",
    "vision.stay_ms": "扫掠停留时长(毫秒):每个停留点停住等 tooltip 渲染后再截图识别。"
    "不得小于 120(太短会漏数据);网络/低配机可调大到 200~300",
    "vision.sweep_gap_ratio": "扫掠步长比例:(0, 1]。1.0(默认)=步长等于格子宽高,每格采一次;"
    "小于 1 = 更密(重叠采样,更慢但更稳);大于 1 会留缝漏格,校验会拒绝",
    "vision.low_confidence": "OCR 置信度下限(0~1,默认 0.7):低于该值的停留点会重扫一次,"
    "两次取置信度更高者。识别不稳时调高,耗时敏感时调低",
    "vision.max_misclick": "连续误按键阈值(≥1,默认 3):按错键且未进目标界面达到该次数后,"
    "按 Esc 回退到干净状态并重新全扫,避免在错误界面继续乱点",
    "vision.kick_verify": "踢出验证方式(踢出回显不含玩家名,必须交叉验证):"
    "'grid'=踢后全扫确认格位消失;'count'=人数 N 前后对账(须 N-1);"
    "'time_window'=只接受操作后时间窗内恰好一条「踢出成功」;"
    "'any'(默认)=以上任一成立即可。都不成立时标记「执行结果待确认」",
    "vision.ocr_command": "外部 OCR 命令(可选):留空且未安装 pytesseract 时,UI 模式启动即报错"
    "(不会静默降级回指令模式)。填法:命令行模板,用 {image} 代表截图临时文件路径,"
    "如 'C:\\\\tools\\\\ocr.exe --png {image}';命令的 stdout 视为识别文本",
    "vision.grid": "扫掠网格(界面结构参数,与分辨率相关):材质包改不了格子布局,"
    "但窗口大小/分辨率不同会变,需按实机标定",
    "vision.grid.origin_x": "网格左上角 X 坐标(像素,≥0)。标定方法:截图量出第一个物品格左上角",
    "vision.grid.origin_y": "网格左上角 Y 坐标(像素,≥0)。与 origin_x 一起定位网格起点",
    "vision.grid.cell_width": "单个格子宽度(像素,≥1);扫掠步长 = 该值 × sweep_gap_ratio",
    "vision.grid.cell_height": "单个格子高度(像素,≥1);扫掠步长 = 该值 × sweep_gap_ratio",
    "vision.grid.columns": "网格列数(≥1);与 rows 一起决定停留点总数",
    "vision.grid.rows": "网格行数(≥1);与 columns 一起决定停留点总数",
    "vision.grid.expected_cells": "预计有物品的格子数(≥1):扫掠结束后与实际识别到语义的格数对账,"
    "缺口超过允许比例即判扫掠不完整并要求重扫。按公会界面实际物品格数标定",
    "vision.grid.max_missing_ratio": "允许的缺口比例([0, 1],默认 0):0=一格都不能少(最安全);"
    "界面空槽较多时可调大(如 0.2),但越大越可能拿漏掉的成员做判定",
    "ui.window": "GUI 窗口几何。作用=默认尺寸与是否记住上次位置。可填值=见下列 width/height/remember_geometry",
    "ui.tray": "GUI 托盘行为。作用=是否常驻、点关闭怎么做、首次是否询问。可填值=见下列 enabled/close_action/ask_on_close",
    "update": "自动更新。作用=决定是否/何时检查并安装新版本。可填值=见下列 mode/manifest_url",
    "ui.theme": "GUI 主题。作用=决定界面用深色还是浅色 token。可填值=auto(默认,跟随系统注册表 AppsUseLightTheme)/dark/light",
    "ui.custom_titlebar": "GUI 标题栏样式。作用=true 用自绘无边框标题栏(36px,含小窗/置顶/最小化/关闭);false 用系统标题栏+32px 工具条。可填值=true/false",
    "ui.window.width": "GUI 默认窗口宽度(像素)。作用=首次启动与未记录几何时的宽度。可填值=正整数,建议 ≥900(规范默认 1000;小于最小尺寸会被 GUI 钳到 900×620)",
    "ui.window.height": "GUI 默认窗口高度(像素)。作用=同 ui.window.width。可填值=正整数,建议 ≥620(规范默认 680)",
    "ui.window.remember_geometry": "是否记住窗口位置与大小。作用=true 记住上次位置;上次落在屏幕外(换显示器)时自动回落居中。可填值=true(默认)/false",
    "ui.tray.enabled": "托盘常驻开关。作用=false 时点关闭直接退出,不询问也不驻留。可填值=true(默认)/false",
    "ui.tray.close_action": "关闭窗口的行为。作用=决定点关闭后做什么。可填值=ask(每次询问,默认)/minimize(最小化到托盘)/exit(直接退出)",
    "ui.tray.ask_on_close": '首次关闭时是否询问。作用=true 弹一次对话框让用户选并勾选"不再询问";勾选后本键写 false,可在设置页改回。可填值=true(默认)/false',
    "update.mode": "自动更新档位。作用=check 仅后台查一次并在关于页提示;notify 弹窗让用户决定(默认);auto 静默下载安装、退出后生效。可填值=check/notify/auto",
    "update.manifest_url": "更新清单 latest.json 的地址。作用=留空则不做更新检查;填写后按变体匹配下载。可填值=https URL 或空字符串(默认)",
    "ui": "UI 配置界面(阶段 3):是否允许图形入口、窗口初始形态、是否置顶、"
    "踢出二次确认方式与确认词。无 --ui 参数时全部不起作用",
    "rules_enabled": "是否启用自定义规则引擎:false(默认)=只用内置阈值规则;"
    "true=按 rules 列表判定,内置阈值规则作为其中一条默认规则参与(同一接口)",
    "rules_strategy": "多规则命中时的策略:'first_match'=按列表顺序首个命中(默认);"
    "'highest_priority'=命中里 priority 最大者;'all'=应用全部命中的规则",
    "rules": "自定义规则列表(JSON 数组)。每条含 id/name/enabled/priority/stop/"
    "conditions/actions。条件字段可用:weekly_total(数值比较) rank(== != in not_in) "
    "online(== !=) player(== != matches) joined_at/last_online(before after)。"
    "动作可用:promote(times) demote(times) kick(reason) notify ignore",
    "ui.enabled": "是否启用图形界面入口:true=启动即拉起界面(找不到 Gui.exe 会明确报错并回落控制台);false=只走控制台流程。默认 true。"
    "无 --ui 参数时默认走控制台,向后兼容",
    "ui.start_mode": "窗口初始形态:'normal'=窗口化 600x400(默认);'compact'=小窗 200x150"
    "(只显示状态与进度)。运行时可用工具栏按钮切换",
    "ui.always_on_top": "窗口是否始终置顶:true=用 win32gui 设为 HWND_TOPMOST;false=普通窗口。"
    "运行时可用图钉按钮切换",
    "ui.kick_confirm_mode": "踢出二次确认的方式:'dialog'=弹窗(默认,需输入确认词);"
    "'inline'=在主界面确认(不弹窗)。无论哪种都必须二次确认",
    "ui.kick_confirm_text": "弹窗确认时要求输入的确认词:必须逐字匹配才执行踢出(踢出不可逆)。"
    "可填任意字符串,如 'kick-confirm' 或 '确认踢出'",
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
    "async.enabled": "启用开关:true=启用异步(主线程只发不验),false=串行执行(逐条发送并读日志验证;调试建议关闭)",
    "async.num_workers": "后台线程数(≥1,默认 1):每个线程独立读日志验证命令结果",
    "async.max_queue_size": "命令队列最大容量:满时丢弃后续命令，默认 100",
    "async.max_retries": "验证失败最大重试次数:重试耗尽的任务进入补做队列。建议 1~5",
    "async.retry_delay": "重试间隔(秒):失败后等待多久再重试,建议 0.5~3",
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
    "report": "运行分析报告(第三份报告)的开关与内容范围:控制是否生成、是否附配置快照、"
    "是否附每人每日贡献明细。详见 docs/详细报告规范.md",
    "report.analysis_enabled": "是否生成第三份「运行分析报告」:true(默认)=三份报告都出;"
    "false=只出贡献报告与群公告,前两份不受影响",
    "report.include_config_snapshot": "是否在分析报告第 5 节附配置快照(阈值/开关/名单/时间参数):"
    "true(默认)=附上,便于事后复现与追责;false=该节只留标题",
    "report.include_daily_detail": "是否在分析报告第 2 节附每人 7 天每日贡献明细:"
    "true(默认)=附上;false=每人只写周贡献合计,成员多时可显著缩短报告",
    "contact_text": "公告尾部联系人文案(原脚本'若有异议联系群管理员演示管理员'的风格),{admin} 为占位符",
    "error_patterns": "执行错误检测:日志中出现任一关键词视为命令执行失败。作用=命中即判该条命令失败并告警。可填值=任意子串,一条一个元素;建议同时保留短词'权限不足'与游戏整句回显'你只能修改低于自己职位的公会成员职位'(只写短词会漏掉整句回显)",
    # ── Debug ────────────────────────────────────────────────────────────
    "debug": "Debug 模式 （仅调试/测试时使用）",
    "debug.enabled": "Debug 模式开关",
    "debug.skip_stages": '跳过的阶段列表：可选 "list"、"member"、"decide"、"notify"、"execute" （仅在开启Debug 模式时才生效）默认值: "list", "member"',
    "debug.simulation": "模拟数据 （用于模拟被跳过数据获取阶段的数据 仅在开启Debug模式生效）",
    "debug.simulation.guild_name": "模拟公会名数据",
    "debug.simulation.players_per_rank": '每个等级生成的玩家数量：整数（所有等级统一）或对象（各等级单独指定，如 {"high":3,"mid":2}），缺失的等级默认为 1',
    "debug.simulation.contribution_range": "模拟周贡献值的周贡献随机范围（最小值，最大值），若未指定则默认使用 [0, 50000]",
    "log": "日志系统：写入日志目录下的 latest.log；每次启动自动归档旧日志为按天压缩包 app-YYYY-MM-DD.gz",
    "log.enabled": "是否启用日志文件（true=记录到文件，false=不写文件）",
    "log.level": "日志级别：数字越大越详细，每级包含上一级更严重的全部。1=致命(启动失败/意外关闭) 2=严重错误 3=错误 4=信息(默认) 5=调试 6=追踪",
    "log.dir": "日志目录（相对脚本运行目录，自动创建）",
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
