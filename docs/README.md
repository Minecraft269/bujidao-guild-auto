# 布吉岛公会管理脚本(全自动版)

> ## ⚠️ 免责声明
>
> **本项目完全由人工智能(AI)生成与管理。** 代码、文档、测试与 CI 配置均由 AI 编写,
> 人类参与者主要提供需求、业务规则与方向性决策。请注意:
>
> * 本项目**不是**经过专业团队审计的成熟软件,请自行评估风险后再使用;
> * 本项目会**模拟键盘鼠标操作你的游戏客户端**,请在明确了解其行为的前提下运行,
>   并对可能产生的任何后果(包括但不限于游戏内操作失误、账号风险)**自行负责**;
> * 本项目**按"原样"提供,不含任何明示或默示的担保**。作者及贡献者不对因使用本项目
>   造成的任何直接或间接损失承担责任;
> * 使用本项目可能违反你所玩游戏的服务条款 —— 请自行确认并承担相应后果。
>
> 设立本声明,是为避免"AI 生成的项目被误认为有人工背书与长期维护承诺"而导致的误解。
> 如果你寻求有支持承诺的生产级工具,本项目不满足该期待。
>
> 由原半自动脚本(演示公会演示管理员 / 演示公会演示管理员)重构而来:**日志钩子驱动全自动化**。
> 完全继承原有业务规则(升职/降职/踢出的阈值、命令格式、通知文案、报告风格),逐项对照见 `docs/业务规则继承清单.md`。
> 原有"人工复制名单 / 点击复制按钮 / 手动搬运文件"的环节全部由脚本自动完成。

## CI 状态

| 流水线 | 说明 |
|---|---|
| [![Lint](https://github.com/Minecraft269/bujidao-guild-auto/actions/workflows/lint.yml/badge.svg)](../.github/workflows/lint.yml) | ruff 风格检查 |
| [![Tests & Coverage](https://github.com/Minecraft269/bujidao-guild-auto/actions/workflows/test.yml/badge.svg)](../.github/workflows/test.yml) | 测试 + 双档覆盖率门禁(核心层 ≥98% / 总体 ≥92%) |
| [![Build](https://github.com/Minecraft269/bujidao-guild-auto/actions/workflows/build.yml/badge.svg)](../.github/workflows/build.yml) | PyInstaller 打包 Windows 可执行文件 |
| [![Security](https://github.com/Minecraft269/bujidao-guild-auto/actions/workflows/security.yml/badge.svg)](../.github/workflows/security.yml) | CodeQL + 依赖审查 + pip-audit |

## 文档索引

| 文档 | 内容 |
|---|---|
| **[使用说明.md](使用说明.md)** | **怎么用**：启动流程、两种模式差异、交互按键、CLI 参数、Debug 模式、日志排障 |
| **[配置说明.md](配置说明.md)** | 逐个配置项的作用与可填值 |
| [覆盖率报告.md](覆盖率报告.md) | 分档覆盖率(核心层/硬件层/总体) + 硬件层 mock 策略 |
| [CI路径过滤验证.md](CI路径过滤验证.md) | 四条流水线的路径过滤规则与实测验证(文档变更不触发重活) |
| [本次变更声明.md](本次变更声明.md) | 本轮改了什么、为什么、修了哪些缺陷 |
| [更改声明.md](更改声明.md) | 2026-08 历史变更记录 |
| [流程与架构图.md](流程与架构图.md) | **架构真源**：分层依赖、双模式流程、UI 识别模式、构建变体矩阵 |
| [业务规则继承清单.md](业务规则继承清单.md) | **业务规则真源**：阈值矩阵/命令格式/通知文案(零改动) |
| [UI识别模式详解.md](UI识别模式详解.md) | **UI 模式真源**：界面层级、tooltip 六字段、踢出交叉验证、扫掠与按键试探 |
| [详细报告规范.md](详细报告规范.md) | **第三份报告真源**：七节结构、数据来源按模式区分、名册对账口径 |
| [验收证据.md](验收证据.md) | **25 条硬性验收**逐条证据（命令 + 输出摘录） |
| [审查记录.md](审查记录.md) / [审查记录-复审.md](审查记录-复审.md) / [审查记录-复审2.md](审查记录-复审2.md) | 独立审查（审查与实现分离）三轮记录：REJECT → REJECT → APPROVE |

> 首次运行会自动生成带中文注释的 `config.json`,照着注释填即可。

## 版权声明

版权所有 (c) 2026。本项目保留所有权利(All Rights Reserved)。

**未经授权禁止转载、分发。** 本仓库为公开源码仅供学习参考使用,任何形式的转载、
二次分发或商业使用均需事先获得版权所有人书面许可。转载或分发视为接受本条款。

No LICENSE file is provided. All rights reserved.
Unauthorized redistribution or dissemination is prohibited.

## 一、与原脚本的对比

| 环节 | 原脚本 | 本版本 |
|---|---|---|
| 成员名单获取 | 人工粘贴 `/guild list` 到 guild_list.txt,再运行 parse_guild.py | 自动发送 `/guild list`,日志钩子捕获并解析 |
| 逐人贡献查询 | 模拟点击游戏 UI"复制按钮"+ 剪贴板 | 自动发送 `/guild member <ID>`,日志钩子捕获面板 |
| 判定规则 | 4 个脚本分别按职位 | 同一套阈值矩阵,按等级判定(数值完全一致) |
| 通知 | 逐人游戏聊天发送 | `/gc` 公会广播(查询完成后、执行前),文案继承 |
| 执行 | actions_*.txt 手动搬运 + run_execute_all.py | 自动执行,带错误回显检测与踢出确认 |
| 报告 | 汇总脚本 + 公告脚本 | 自动输出"贡献报告 + 操作清单" |

## 二、环境要求

- Windows + Python 3.10+(pyproject.toml 的 requires-python 为 >=3.10)
- 依赖(二选一):
  - `pip install -r requirements.txt`(推荐,已含版本要求)
  - 或 `pip install pyautogui pynput pyperclip winsdk`
  - winsdk 为可选:用于游戏菜单界面 OCR 检测(需系统中文 OCR 语言包);未安装时自动降级
- 游戏:网易版《布吉岛》(HeyPixel 服务器),**游戏须已启动并进入服务器**
- 窗口识别:优先只识别**游戏进程**(java.exe/javaw.exe/minecraft.exe 等白名单)的窗口,其他程序标题含"Minecraft"等关键词不会误判;无白名单进程窗口时回退按标题匹配

## 三、快速开始

```bat
:: 0. 安装项目本身(src-layout:必须装包,否则 python -m guild_auto 会 ModuleNotFoundError)
pip install -e .
:: 如需 OCR 界面检测:  pip install -e ".[ocr]"

:: 1. 首次运行自动生成 config.json(请按需修改,每个键的说明见 docs/《配置说明.md》)
:: 2. 试运行(不执行任何升/降/踢,只查询+通知+报告):
python -m guild_auto --admin 你的游戏ID --dry-run

:: 3. 确认查询与判定无误后,正式运行:
python -m guild_auto --admin 你的游戏ID
```

> ⚠️ **重要**:运行期间请勿移动鼠标/切换窗口;脚本会自动把游戏窗口置前并发送命令。
> 找不到游戏窗口时会询问是否继续(防止误操作其他程序)。
> 暂停键(默认 `U`)可随时暂停/继续。

## 四、命令行参数

| 参数 | 说明 | 默认 |
|---|---|---|
| `--config <路径>` | 配置文件路径 | `./config.json` |
| `--admin <管理员名>` | 管理员名(报告与通知标注) | 配置中的 admin |
| `--log <路径>` | 游戏日志路径(latest.log) | 配置中的 game_log |
| `--options <路径>` | 游戏配置路径(options.txt) | 配置中的 game_options |
| `--dry-run` | 试运行:仅查询+通知+报告,不执行升/降/踢 | 关 |

## 五、自动执行流程

```text
启动 → 读取配置(校验)→ 管理员名自动识别(日志 --username 启动参数;配置已填则跳过)
     → 连接日志(自动检测编码 utf-8/gbk/gb2312)→ 查找并置前游戏窗口 → 暂停键监听(U)
     → [断点续做] 检查待办文件 reports/pending_actions.json:
         若有上次未完成/失败的操作 → 打印清单 → 询问是否执行 → 逐条执行
         (成功→从待办移除;失败→保留待下次)→ 询问:退出 or 继续新的统计操作
     → 若无待办(或选择继续):进入正常统计流程
[1/5] 发送 /guild list → 捕获面板 → 解析 公会名/分组/成员总数/在线数
[2/5] 按查询范围过滤玩家(默认全员,跳过 会长/副会长/管理员——与原脚本 skip_groups 一致;可在 skip_ranks 调整)
[3/5] 逐人发送 /guild member <ID> → 捕获面板 → 解析 加入时间/上次在线/周贡献(含"今天",千分位)
      → 按继承阈值判定 升职/降职/踢出(失败自动重试,默认 3 次,仍失败记入报告并跳过)
[4/5] /gc 广播通知(查询完成后、执行前;summary 汇总 或 per_player 逐人,按配置)
[5/5] 执行操作(先询问"是否执行?",输入 y 或回车=执行,n=跳过;踢出前另需 kick-confirm 确认;
      每条命令发送前重新激活游戏窗口;执行后检测错误回显,失败记入报告)
      输出报告(原脚本风格):
        reports/<日期> <公会名>公会 所有玩家详细贡献.txt —— ---高活跃--- 等分组 + 玩家:ID 周贡献为:N 执行操作:X
        reports/<公会名>_<日期>.txt —— 群公告(被升职（N人）/被踢出（N人）/被降职（N人)+ ID 列表 + 联系人)
      流程结束后按回车退出
```

### 判定规则(继承原脚本,勿改阈值数值)

| 当前等级 | 条件(周贡献) | 操作 |
|---|---|---|
| 高活跃 | `<= 30000` | 降职 ×1 |
| 中等活跃 | `>= 30000` | 升职 ×1 |
| 中等活跃 | `< 5000` | 降职 ×1 |
| 低活跃 | `>= 30000` | 升职 ×2 |
| 低活跃 | `>= 10000` | 升职 ×1 |
| 低活跃 | `< 3500` | 降职 ×1 |
| 入门 | `>= 30000` | 升职 ×3 |
| 入门 | `>= 10000` | 升职 ×2 |
| 入门 | `>= 3500` | 升职 ×1 |
| 入门 | `< 3500` | 踢出 |

> 边界语义(含/不含)与原脚本逐字一致;周贡献 = 面板"公会经验周贡献"段全部数值之和(含"今天"行)。

## 六、开关与按键

配置 `switches` 段:

| 开关 | 说明 |
|---|---|
| `promote_enabled` | 升职开关 |
| `demote_enabled` | 降职开关 |
| `kick_enabled` | 踢出开关 |
| `execution_enabled` | 执行开关;`false` = dry-run(仅查询+通知+报告) |
| `notify_enabled` | /gc 通知开关 |
| `notify_mode` | `summary`(汇总一条)/ `per_player`(逐人按原文案) |
| `verbose_log` | 详细日志(控制台打印每条查询细节) |

- **暂停键**:默认 `U`(可配 `pause_key`),随时暂停/继续;
- **聊天键**:默认 `auto`(自动读 options.txt 的 `key_key.chat`;本机实测为回车键),也可直接写 `t`/`enter` 或中文别名 `回车`/`空格`;
- **试错式发送**:发送命令采用"直接粘贴+回车"试探——聊天栏已打开则直接发送成功;未打开则回车会打开聊天栏,无日志响应后自动重试(菜单界面同样自动收敛)。直接粘贴会覆盖输入框残留内容,不会误发残留文字;
- **菜单自动处理**:发送前通过 Windows OCR 检测游戏是否处于菜单界面(识别"回到游戏/断开链接"等字样,需 winsdk+系统中文 OCR 语言包),若在菜单则自动按 Esc 关闭;OCR 不可用时自动降级为纯试错式发送。

## 七、常见问题

| 现象 | 原因与处理 |
|---|---|
| 找不到游戏窗口 | 确认游戏已启动、窗口未最小化;窗口标题须含配置关键词(`window_keywords`) |
| 查询全部超时 | 游戏不在服务器内(需进入服务器)/ 游戏窗口不在前台 / 日志路径错误 / 面板消息未写入日志(重启游戏后重试) |
| 日志面板是"一行" | 正常:网易版把面板整体写成一条物理行,内部换行以 `\n` 字符存储,脚本已自动兼容(单行与多行两种格式) |
| 日志乱码 | 配置 `log_encoding` 手动指定 `gbk` 或 `gb2312`(网易版日志默认 GB2312) |
| 按键没反应 | options.txt 里聊天键不是 t(如本机是回车);确认 `chat_key: auto` 已生效(启动时打印聊天键名) |
| 踢出未执行 | `kick_confirm` 需输入 `kick-confirm` 确认;或 `kick_enabled` 为 false |

## 八、打包为 exe

```bat
build.bat            :: 单文件模式,产物 dist\布吉岛公会管理脚本.exe(约 18MB)
```

- 打包依赖:pyinstaller(脚本会自动检查/安装)
- 体积优化:已排除未使用的 cv2(31MB)/PyQt5(53MB)/numpy/tkinter 等重模块
- **注意**:首次运行时,360/Defender 可能拦截临时解压(报 `Failed to load Python DLL`)——在杀软中信任该 exe 后重试即可
- 首次运行 exe:生成 config.json(带逐键中文注释)→ 提示查看配置 → 回车退出;再次启动即正常运行

## 九、测试

```bat
python -m pytest tests/test_parsers.py -v
```

解析器测试覆盖:两种日志格式前缀、分组/●/多行名/统计行解析、千分位周贡献、字段缺失与玩家校验失败等。

## 九、文件结构

```text
guild_auto_v2/
├── pyproject.toml            # 项目元数据 + 入口点 + ruff/pytest/coverage 配置
├── CLAUDE.md                 # 项目纪律(零硬编码、业务规则零改动、结论需证据)
├── src/guild_auto/           # 全部生产代码(src-layout:根目录不放 .py)
│   ├── main.py               #   CLI 入口(参数解析 → 控制台流程)
│   ├── __main__.py           #   python -m guild_auto 入口
│   ├── config/               #   配置层:defaults(默认值) schema(校验) loader(读写)
│   ├── domain/               #   领域层:decisions(判定/文案/命令) parsers reporter
│   ├── application/          #   应用层:stages runtime(状态) pause pending debug_data
│   ├── infrastructure/       #   基础设施:window_input game_state log_watcher
│   │                         #             logger action_executor
│   ├── concurrency/          #   并发层:async_system async_executor
│   ├── vision/               #   UI 识别层(阶段 6):bindings tooltip sweep navigator
│   │                         #             runner verifier adapters models
│   ├── ui/                   #   UI 层(阶段 3):app config_panel confirm_dialog
│   │                         #             toolbar rules_panel
│   ├── capabilities.py       #   能力注册表 + 构建变体拼接名(阶段 7)
│   └── ui_mode.py            #   UI 模式查询阶段(导航→扫掠→同构 records)
├── scripts/build-variants.py # 生成 CI 构建矩阵 / 写入打包期能力常量
├── tests/                    # 单元测试(1300+ 个)
├── docs/                     # 文档:README/流程与架构图/使用说明/配置说明/
│                             #       业务规则继承清单/UI识别模式详解/详细报告规范/
│                             #       验收证据/审查记录/覆盖率报告/变更声明
├── config.json               # 配置文件(首次运行自动生成,已 gitignore)
├── logs/  reports/           # 运行期产物(已 gitignore)
└── .github/workflows/        # CI
```

> 依赖方向严格单向:`domain`(零 IO)← `application` ← `ui`;
> `infrastructure` 只被调用不反向 import;`concurrency` 经接口注入。
> 详见 [流程与架构图.md](流程与架构图.md)。
>
> 构建变体的权威标识是拼接名（如 `Lite-Async-Gui-Vision`）；`Full` 只是它的别名，产物名一律用拼接名。
