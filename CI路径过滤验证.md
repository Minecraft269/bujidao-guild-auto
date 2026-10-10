# CI 路径过滤验证记录

本文件用于**验证** GitHub Actions 的 paths-ignore 规则是否真的生效。

## 规则（2026-10-11 加）

四条流水线对 push / pull_request 都设了路径过滤：

| 流水线 | 过滤方式 | 范围 |
|---|---|---|
| Lint | paths-ignore | `**.md` `docs/**` `.gitignore` `.gitattributes` `LICENSE` |
| Tests & Coverage | paths-ignore | 同上 |
| Build | paths-ignore | 同上 |
| Security | paths 白名单 | 只认 `**.py` `requirements.txt` `pyproject.toml` `.github/workflows/**` |

Security 用白名单而非 ignore，因为它内含三个 job 且敏感面不同
（CodeQL 看 `.py`、pip-audit 看依赖清单、dependency-review 需 PR diff）。
白名单也更安全：将来新增配置类文件不会被误扫或漏扫。

## 验证方法

推一个**只改 `.md`** 的提交（比如只改本文件），预期四条流水线**全部不触发**。
若 Build 仍跑了，说明 paths-ignore 没生效，需要排查。

## 为什么值得验证

本仓库迭代节奏里文档提交占比很高。实测记录：一次纯文档提交
（README + 使用说明 + 覆盖率报告 + 变更声明，3 文件 437 行）
曾触发全部 4 条流水线，包括全矩阵 3.10/3.11/3.12 的测试
和 PyInstaller + UPX 的构建 —— 那是纯粹的浪费。

## tag 触发不受影响

GitHub Actions 对 tag 推送**不应用** paths-ignore，所以打 tag 发 Release 的
路径仍然畅通。`schedule`（每周一定期扫描）同样不受约束，作为 CVE 兜底。
