# 为 {{TOOL_NAME}} 贡献代码

*[English](CONTRIBUTING.md) | 中文*

感谢改进 **{{TOOL_NAME}}** —— {{DESCRIPTION}}。本文档介绍如何构建、测试和提交改动。

> 这是为 AI 工具实验而分享的业余项目；维护者不提供商业支持或生产环境保证 —— 详见 README 免责声明。

## 构建总纲在 spec 里

本仓库是一个 **AI 原生 CLI 工具**，优先面向 AI Agent。在实现或修改任何功能前，先读总纲：

- **[AGENTS.md](AGENTS.md)** —— 入口，导航到本地规范与共享仓库骨架标准。
- **[`.agent/AGENT_zh.md`](.agent/AGENT_zh.md)** —— 项目总纲。
- **[`.agent/CLI-SPEC_zh.md`](.agent/CLI-SPEC_zh.md)** —— CLI 输出 / 错误 / 写操作闭环契约。
- **[`.agent/SKILL-SPEC_zh.md`](.agent/SKILL-SPEC_zh.md)** —— AI Skill 包规范。
- **[`.agent/SEC-SPEC_zh.md`](.agent/SEC-SPEC_zh.md)** —— 安全基线（风险分级、不可信内容、凭证、供应链）。

这些 spec 是权威来源，优先级高于默认习惯。违反 CLI 契约（stdout 是契约、同形 envelope、错误三件套、写操作闭环）的代码不会被合并。

## 开发环境

<!--
语言工具链 —— 下面只保留一个块，删掉其余：
  - Go 1.25+        ：编译型二进制 + npm 壳
  - Python 3.10+    ：PyInstaller 二进制 + npm 壳
  - Node 16+        ：所有变体的 npm 壳 / 平台包脚本都需要
形态始终是：装依赖 -> 构建 -> 测试 -> 跑 `--help` 冒烟测试。
-->

```bash
# 克隆
git clone https://github.com/{{REPO_SLUG}}.git
cd {{TOOL_NAME}}

# --- Go 变体 ---
go mod download
make build                      # 或：go build -o bin/{{TOOL_NAME}} {{CMD_PATH}}
go test -race ./...
./bin/{{TOOL_NAME}} --help

# --- Python 变体 ---
# pip install -e ".[dev]"
# python build.py
# pytest tests/ -v
# {{TOOL_NAME}} --help

# 可选：如果改动 npm wrapper 或平台包脚本，需要 Node.js 16+
```

如果拉依赖慢，用区域代理（如 Go：`GOPROXY=https://goproxy.cn,direct`；pip 用镜像源）。

## 命令

| 目标 | ▶ 命令 |
|------|--------|
| 构建 | `make build`（Go）/ `python build.py`（Python） |
| 测试 | `make test` → `go test -race ./...` / `pytest tests/ -v` |
| 检查 | `make lint` → `golangci-lint run ./...` / `ruff check .` |
| 格式化 | `make fmt` → `gofmt -w .` / `ruff format .` |

`make` 目标由变量驱动；Windows 上回退到底层工具命令。新贡献者推送前应在本地跑 **lint + test**。

## 分支与提交规范

- 从默认分支拉出：`git checkout -b feat/your-feature`。
- 一个分支只做一件逻辑改动；请求 review 前先 rebase 默认分支。
- 提交遵循 [Conventional Commits](https://www.conventionalcommits.org/)：

```
<type>: <description>

<optional body>
```

| 类型 | 用于 |
|------|------|
| `feat` | 新功能 |
| `fix` | 缺陷修复 |
| `refactor` | 不改变行为的代码重构 |
| `docs` | 文档改动 |
| `test` | 新增或更新测试 |
| `chore` | 构建、CI、依赖或工具链改动 |
| `perf` | 性能优化 |
| `ci` | CI/CD 流水线改动 |

示例：`feat: add export command`、`fix: handle nil pointer in status check`、`docs: sync README_zh`。

## CI 镜像

`.github/workflows/ci.yml` 里的 CI 镜像本地校验：在支持的 OS / 运行时矩阵上跑 **lint + test**（外加 `--help` 冒烟测试和依赖审计）。PR 合并前 **lint 和 test 都必须通过** —— 先在本地跑，避免反复折返。

## 功能契约覆盖率

发布标准：**Functional Contract Coverage = 100%**。`README`、`SKILL`、reference 页面、`{{TOOL_NAME}} reference`、`--help`、`context`、`doctor`、`changelog` 或 `update` 中记录的每个公开行为，都必须有自动化命令级测试。

每个新增或变更的命令，至少覆盖成功路径、非法参数、配置/认证/权限失败（适用时）、上游失败或超时（适用时）、JSON envelope 形状、输出 schema、exit code、stdout/stderr 边界，以及非交互行为。每个改变可观察行为的 bug fix 都要带回归测试。

数字代码覆盖率单独跟踪，并可按仓库逐步抬升；但它不能替代缺失的契约测试。

发布就绪等级是机器可读契约：

- `stable`：FCC 达到 100%，mock upstream / contract tests 覆盖成功与失败路径，并且该 release candidate 有真实环境 smoke/E2E 记录。
- `beta`：FCC 达到 100%，mock upstream / contract tests 完整，但缺少真实环境 smoke/E2E 记录，或明确不具备真实 E2E 条件。
- `unpublishable`：任一公开行为缺少命令级测试，或 mock upstream / contract tests 只覆盖 happy path。

当测试证据变化时，同步保持 `{{TOOL_NAME}} reference` 的 `release_readiness` 和 `{{TOOL_NAME}} doctor` 的 `release_readiness` 检查真实可信。

## 新增命令 / 领域

工具按领域切分（一个领域 ≈ 被封装产品的一个功能面）。新增领域时，每一层都要动：

1. **DTO** —— 在对应领域的 API 方法旁定义请求/响应类型。
2. **Client** —— 增加 API 客户端方法；复用共享的 HTTP/认证工具与参数化 URL 构造（绝不把用户输入拼进 URL）。
3. **Command** —— 增加 Cobra/argparse 命令与子命令；注册 flag；对每个写命令调用写标记（`markWrite` / 等价物），使其进入审计日志和 `--dry-run → --confirm` 闭环。
4. **Tests** —— API 层测试（打 HTTP 测试服务器）**以及**命令层行为测试。
5. **SKILL** —— 新增 `skills/{{TOOL_NAME}}/reference/<domain>.md` 页面，并从 `skills/{{TOOL_NAME}}/SKILL.md` 链接过去（SKILL.md 保持为简短的渐进式披露索引）。
6. **Docs** —— 更新 `README.md` / `README_zh.md` 命令列表，并在 `CHANGELOG.md` 的 `## [Unreleased]` 下加一行。

`reference` 会自动遍历命令树，新命令无需额外接线即出现在 `{{TOOL_NAME}} reference` 中。

## Pull Request 指南

1. 尽量 **一个 PR 一件逻辑改动**。
2. **测试**：行为改动要加/改测试。
3. **文档**：flag/流程变化时更新面向用户的文档。
4. **提交**：Conventional Commits；任何地方都不得出现密钥或真实 token。

### PR 检查清单

- [ ] 单一逻辑改动，diff 聚焦
- [ ] 测试已加/更新且通过（`make test`）
- [ ] 公开行为仍保持 100% 功能契约覆盖率
- [ ] `release_readiness` 仍准确（`stable` 必须有真实环境 smoke/E2E 记录）
- [ ] Lint 通过（`make lint`）
- [ ] 文档与行为同步（`README` 及受影响的 `SKILL`/reference 页面）
- [ ] `CHANGELOG.md` 已在 `## [Unreleased]` 下更新
- [ ] **双语文档同步** —— 每处 `*.md` 改动都在对应 `*_zh.md` 中镜像（反之亦然）
- [ ] 代码、测试、fixture、提交历史中无密钥、token 或真实凭证
- [ ] 提交信息遵循 Conventional Commits

## 安全

不要为未披露的安全漏洞开公开 issue。见 [SECURITY_zh.md](SECURITY_zh.md)。
