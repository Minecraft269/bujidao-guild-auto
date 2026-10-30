# CI 与 Bot 规范

> 本文档描述本项目的持续集成策略、自动化机器人配置与分支合并规范。
> 相关文件：`.github/workflows/`（工作流定义）、`.github/dependabot.yml`（依赖机器人）。

---

## 一、现有工作流

| 文件 | 触发 | 作用 |
|---|---|---|
| `build.yml` | push 任意分支 / PR / tag / 手动 | PyInstaller 打包（onefile + UPX），上传 artifact；打 tag 时自动发 Release |
| `lint.yml` | push / PR / 手动 | `ruff check`（阻塞）+ `ruff format --check` |
| `test.yml` | push / PR / 手动 | pytest 全量测试 + 双档覆盖率门禁（核心层 ≥98% / 总体 ≥92%），Python 3.10 / 3.11 / 3.12 矩阵 |
| `security.yml` | push / PR / 手动 / 每周一 04:27 UTC | CodeQL 代码扫描 + dependency-review（仅 PR）+ pip-audit 依赖漏洞审计 |

### 公共约定

- **权限最小化**：默认 `contents: read`，仅确需写入的 job 单独提权
- **并发控制**：每个工作流带 `concurrency` 分组 + `cancel-in-progress`，同一分支重复推送只保留最新一次
- **路径过滤**：纯文档改动（`**.md`、`docs/**`、`.gitignore`、`.gitattributes`、`LICENSE`）不触发构建与测试；**tag 推送不受路径过滤影响**，保证发布链路畅通
- **缓存**：pip 缓存 key 绑定 `requirements.txt`，多个工作流共享同一缓存条目
- **编码**：Windows runner 的 stdout 为 cp1252 管道，任何可能输出中文的步骤必须设置
  `PYTHONIOENCODING=utf-8` 与 `PYTHONUTF8=1`，否则打印中文会抛 `UnicodeEncodeError` 导致 job 失败
- **覆盖率**：门禁只对核心层强制；硬件交互层（`pyautogui` / `winsdk` OCR / 文件轮询）以 mock 覆盖分支与异常路径，真实设备调用不计入分母，单独成节报告

---

### gui.yml（界面门禁，已落地）

界面是独立的 tauri 进程（`gui/`），与 Python 核用两套工具链，因此单独一道工作流：

| 作业 | 步骤 | 阻塞 |
|---|---|---|
| 前端 | `biome check` / `vitest run --coverage`（纯逻辑 ≥80%）/ 快照 4 组（差异 ≤0.1%） | 是 |
| Rust | `cargo fmt --check` / `cargo clippy -D warnings` / `cargo test` / `cargo llvm-cov ≥60%` / `cargo build --release` | 是 |

**覆盖率口径**：只统计纯逻辑。Rust 侧统计 `protocol.rs`，排除 `main.rs`（窗口/托盘装配段）、
`ipc.rs`（OS 管道胶水）与 `gen/**`；前端统计 `src/lib/**`，不含 `app.js` 的 DOM 装配段。
被排除的部分由 GUI 冒烟（真进程 + IPC 握手）与 4 组快照兜底，而不是放低门槛。

**不做的事**：不跑 `tauri bundle`（MSI 要下载 WiX，属于发布流程，不进 PR 门禁）。

---

## 二、待补充的工作流

| 工作流 | 作用 | 阻塞策略 |
|---|---|---|
| `mypy.yml` | 静态类型检查 | 阻塞 |
| `vulture.yml` | 死代码检测（未使用的导入/函数/变量） | 阻塞 |
| `mutmut.yml` | 变异测试，验证测试是否真能捕获缺陷 | 非阻塞，出报告 |
| `format.yml` | `ruff format --check` 改为阻塞门禁 | 阻塞 |
| `codecov.yml` | 覆盖率报告上传，PR 自动评论覆盖率变化 | 非阻塞 |
| `stale.yml` | 自动关闭长期无响应的 issue / PR | — |
| `release-drafter.yml` | 按 PR 标签自动起草 Release notes | — |
| `super-linter.yml` | 非 Python 文件（Markdown / YAML / JSON）格式检查 | 阻塞 |

**格式化的落地顺序**：先跑一次 `ruff format .` 并单独提交（避免与业务改动混在一起产生巨大 diff），再把 `format.yml` 从 `continue-on-error` 改为阻塞。

---

## 三、自动化机器人

### 依赖更新

- **Dependabot**（`.github/dependabot.yml`）
  - pip 依赖每周检查一次；GitHub Actions 依赖每周检查一次
  - minor / patch 更新分组为一个 PR，减少噪音；major 更新单独开 PR 便于评审
  - 自动加 `dependencies` 标签与指定 assignee
  - 与 `dependency-review` 联动：Dependabot 开出的 PR 必须通过依赖审查才能合并
- **Renovate**（可选，可与 Dependabot 并存）
  - 提供更细的调度与分组策略
  - 若启用，必须在 `renovate.json` 中排除构建敏感依赖的自动升级（如 PyInstaller、UPX 相关）

### 代码审查

- **CodeRabbit / GitHub Copilot code review**（可选）
  - 在 PR 上自动生成审查意见，作为人工评审的补充
  - ⚠ **不替代独立代码审查**：本项目要求审查与实现分离，AI 审查机器人的评论不构成验收依据

### 仓库安全

- **Allstar**（可选）：强制分支保护规则、检测危险的工作流权限配置

### 依赖 PR 自动合并的工作流（推荐实现）

> 参考实现：同组织下 `project-init-mcp` 仓库的 `dependabot-auto-merge.yml`，
> 其中记录了两个已踩过的坑，务必遵守。

```yaml
name: Dependabot auto-merge
on:
  pull_request:
    types: [opened, synchronize, reopened]
permissions:
  contents: write
  pull-requests: write
  checks: read          # 读 check-runs 必需，不声明默认为 none → API 403
jobs:
  auto-merge:
    if: github.actor == 'dependabot[bot]'   # 只处理 Dependabot PR，人工 PR 一律等人看
    runs-on: windows-latest
    steps:
      - name: 等其它检查全绿后 squash 合并
        # 自行轮询 check-runs，排除本 job（名为 Auto-merge）后判断
        ...
```

**两个必须避开的坑**：

| 坑 | 现象 | 正确做法 |
|---|---|---|
| 用 `gh pr checks --watch` 等检查 | 本 job 自己的检查也在列表里，会一直等自己 → **死锁** | 自己轮询 `repos/{repo}/commits/{sha}/check-runs`，**排除名为 `Auto-merge` 的检查** |
| 合并时加 `--delete-branch` | 仓库已开 `delete_branch_on_merge`，GitHub 合并时已删分支；再删一次会报错，可能被误判为「合并不了」 | **不加 `--delete-branch`**，交给仓库设置 |

**失败处理**：

- 有检查失败 → **不合并**，`exit 0` 留给人看（不静默重试、不强推）
- 合并失败（多为冲突）→ 自动评论 `@dependabot rebase` 让机器人自己 rebase
- 轮询超时 → 不报错，留给人看

**权限与安全**：`contents: write` + `pull-requests: write` + `checks: read`；
`if` 条件限定 `github.actor == 'dependabot[bot]'`，确保**人工 PR 永远走人工评审**。

---

## 四、分支与合并策略

以下能力**都是仓库设置或机器人**，不需要写进工作流：

| 能力 | 实现方式 | 说明 |
|---|---|---|
| 合并后自动删 head 分支 | 仓库设置 `delete_branch_on_merge` | 推荐启用；零成本、不消耗 CI 分钟 |
| PR 自动合并 | 仓库设置 `allow_auto_merge` + PR 上启用 auto-merge | GitHub 原生能力；所有必需检查通过后自动合并 |
| 合并时删分支（工作流内） | `gh pr merge --delete-branch` | 仅对工作流自身发起的合并生效 |
| 整链自动化（合并 + 删分支 + 打 tag） | Mergify / Kodiak 等机器人 | 能力更强，但引入外部依赖，非必需 |

**当前状态**：`allow_auto_merge` 与 `delete_branch_on_merge` 均已启用。

### 自动创建 PR 的三种来源

| 来源 | 说明 |
|---|---|
| 依赖更新机器人 | Dependabot / Renovate 自动开 PR |
| 构建产物工作流 | 如 `peter-evans/create-pull-request`，用于自动更新版本号、生成 changelog 的 PR |
| 人工发起 | 开发者手动开 PR |

三者产出的 PR **一律走完整 CI**，不允许任何来源绕过门禁。

### 合并纪律

- auto-merge 仅用于依赖更新类 PR
- 涉及代码逻辑的 PR 必须人工评审后合并
- 任何机器人产出的 PR 都必须等所有必需检查通过
- 合并信息应能追溯到对应的 issue 或验收项

---

## 五、构建变体与产物

项目支持按能力裁剪的多种构建（详见《流程与架构图.md》第八节「构建变体矩阵」）：
`Lite` / `Lite-Async` / `Lite-Gui` / `Lite-Vision` / `Lite-Async-Gui` /
`Lite-Async-Vision` / `Lite-Gui-Vision` / `Lite-Async-Gui-Vision`

- 每个变体各产出一条 artifact，**文件名含变体拼接名**
- `Full` 是 `Lite-Async-Gui-Vision` 的别名（大小写不敏感，`capabilities.resolve_variant_alias()` 解析）；**拼接名才是权威标识**。产物名与 artifact 名 = **拼接名 + `-Full` 后缀**（仅全能力变体，形如 `布吉岛公会管理脚本-Lite-Async-Gui-Vision-Full.exe`）；**启动横幅与日志只打印拼接名**，不带后缀
- 每个变体单独跑**横幅冒烟**：在源码树里 `PYTHONPATH=src python` 读打包期能力常量，比对「拼接名 + 能力清单」。
- ⚠ CI **不运行** `dist/*.exe`：产物冷启动、以及"配置了不支持的功能时报错"这两项，由发布前的本地冒烟执行（见 `docs/验收证据.md` 第 19 条的冷启动记录）。
- 构建矩阵由配置驱动（`pyproject.toml` extras 或构建脚本的能力表），**禁止把组合硬编码进工作流**

---

## 六、验收

- 每个工作流提交后必须实际触发并留下运行结果，不接受"配置完成但从未运行"
- 覆盖率门禁必须真实拦截：故意降低覆盖率时应看到 CI 失败
- 依赖机器人必须能实际开出 PR
- 所有机器人产出的 PR 走完整 CI，无绕过门禁的自动合并
- AI 审查机器人的结论不计入独立代码审查
