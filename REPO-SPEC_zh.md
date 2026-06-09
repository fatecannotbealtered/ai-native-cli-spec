# AI 原生开源项目仓库规范


本文定义个人所有开源项目的统一仓库标准：必备文档、模板文件、目录布局、发布约定。目标是让开十个项目长一个样，新人（含 AI Agent）进任何一个仓库都能在固定位置找到固定的东西。

消费工具仓库的配套规范放在 `.agent/` 下，入口是 `AGENT.md`。
本文作为仓库骨架标准保留在 `ai-native-cli-spec` 根目录，消费仓库不复制、
不分叉本文件。

- `AGENT.md` —— 入口/总纲：怎么在这建&扩工具，导航到本地规范与本文这份共享仓库标准。
- `CLI-SPEC.md` —— CLI 的机器契约（工具怎么说话）。
- `SKILL-SPEC.md` —— Skill 编写规范（Agent 怎么用）。
- `SEC-SPEC.md` —— 安全基线（怎么不被坑、不坑人）。
- 本文 —— 仓库骨架（项目长什么样）。

## 1. 文件清单（必备矩阵）

等级：**MUST** 必须有；**SHOULD** 强烈建议；**可选** 按需。

| 文件 | 等级 | 职责 |
|------|------|------|
| `README.md` | MUST | 项目门面：是什么、装什么、怎么用 |
| `README_zh.md` | SHOULD | 中文版（i18n 约定见 §3） |
| `LICENSE` | MUST | 开源许可（默认 MIT） |
| `CHANGELOG.md` | MUST | 版本变更记录（Keep a Changelog + SemVer） |
| `CONTRIBUTING.md` | MUST | 如何贡献：环境、分支、提交、测试、PR 流程 |
| `SECURITY.md` | MUST | 漏洞披露渠道与支持版本 |
| `CODE_OF_CONDUCT.md` | SHOULD | 行为准则（Contributor Covenant） |
| `NOTICE.md` | MUST† | 第三方商标 / 归属声明（†包装第三方产品时必须） |
| `docs/COMPATIBILITY.md` | MUST† | 已验证的后端版本矩阵（†对接外部系统时必须） |
| `docs/OPEN_SOURCE_CHECKLIST.md` | SHOULD | 首次公开 push 前的安全门禁清单 |
| `docs/E2E.md` | SHOULD† | E2E / 集成测试环境说明（†对接外部 API 时建议） |
| `.gitignore` | MUST | 按语言忽略产物、虚拟环境、IDE、缓存 |
| `.github/workflows/ci.yml` | MUST | 推送 / PR 触发：lint + test |
| `.github/workflows/release.yml` | SHOULD | tag 触发：构建 + 发布 |
| `.github/ISSUE_TEMPLATE/` | SHOULD | bug / feature 模板 |
| `.github/PULL_REQUEST_TEMPLATE.md` | SHOULD | PR 自检清单 |
| `.github/dependabot.yml` | 可选 | 依赖更新 |
| `AGENTS.md` | MUST* | 跨工具入口钩子，指向 `.agent/AGENT.md` |
| `.agent/AGENT.md` | MUST* | AI 原生工具入口/总纲，导航到本地规范与本文这份共享仓库标准 |
| `.agent/CLI-SPEC.md` | MUST* | CLI 工具的机器契约（*仅 CLI 类项目） |
| `.agent/SKILL-SPEC.md` | MUST* | Skill 编写规范（*带 Skill 的项目） |
| `.agent/SEC-SPEC.md` | MUST* | 安全基线（风险分级 + 注入/权限/凭证/供应链） |
| `skills/<name>/SKILL.md` | MUST* | Agent Skill（*AI 原生工具必备） |
| `skills/<name>/test-prompts.json` | SHOULD* | Skill 审查 / 达尔文式验证用回归 prompt |

带 `*` 的是「AI 原生工具」专属——普通库可不带，但凡是给 Agent 用的 CLI / 工具，这几件是硬指标。带 `†` 的按场景触发：包装第三方产品（Outlook/Exchange、GitLab、Jira、Kibana 等）就必须有商标声明与兼容矩阵。

## 2. README 必备骨架

每个 README 按固定顺序，便于横向对比，也便于 Agent 直接接入：

1. **标题 + 语言切换 + 徽章** —— 徽章顺序固定为 CI、语言质量徽章（适用时）、npm version、license。
2. **一句话定位** —— Agent-native 工具描述。不要把这些工具定位成主要给交互式人工 CLI 使用。
3. **Agent Install** —— Agent 可复制执行的一段安装块：npm 壳安装、Skill 安装、必要环境变量占位、`context`、`doctor`、`reference`。
4. **What It Does** —— 一段话、风险等级，以及必要的第三方/免责声明。
5. **Capabilities** —— 命令组概览表；README 是地图，不是完整手册。
6. **Agent Workflow** —— 安装、配置、预检、通过 `reference` 发现能力、用 `--compact`/`--fields` 降 token、写操作用 `--dry-run` -> `--confirm`、更新后读 `changelog`。
7. **Machine Contract** —— JSON 默认、envelope 形状、stdout/stderr 规则、错误码、`_untrusted`、`--json` 兼容说明。
8. **Configuration** —— 配置路径、环境变量、凭据优先级与存储方式。
9. **Project Structure** —— 顶层目录树，让每个仓库一眼可导航。
10. **Development** —— 本地构建/测试/lint 命令。
11. **Links** —— `AGENTS.md`、Skill、`.agent/CLI-SPEC.md`、安全、兼容性、E2E、变更记录、贡献、声明、许可证。

约定：

- 徽章数量保持克制。不要加入不会提升 Agent 信任或发布质量的装饰性徽章。
- 安装块必须可被 Agent 复制执行，用密钥占位符，不放真实凭据。
- 命令细节归实时 `reference` 输出维护。README 命令表保持高层，避免过期手册。
- 示例命令用真实可执行的形式，时间用 ISO 8601，ID 用占位符 `<message-id>`。

## 3. 国际化（i18n）约定

- 主文档英文 `README.md`，中文 `README_zh.md`，文件名加 `_zh` 后缀。
- 两份顶部互相提供切换链接。
- 其余治理类文档（CONTRIBUTING / SECURITY / 行为准则）默认只维护英文，除非项目主要面向中文社区。
- 双语文档**内容同步**：改一份必须改另一份，CI 可加链接 / 章节一致性检查。

## 4. 版本与发布约定

- **SemVer**：`MAJOR.MINOR.PATCH`。破坏性变更升 MAJOR，向后兼容新增升 MINOR，修复升 PATCH。
- 工具版本（package 版本）与 CLI 输出的 `schema_version` 解耦——后者只在 JSON 契约破坏时升（见 `CLI-SPEC.md`）。
- **CHANGELOG 用 Keep a Changelog 格式**：`Unreleased` 在顶，分 `Added/Changed/Fixed/Deprecated/Removed/Security`。
- 发布即打 git tag `vX.Y.Z`，由 `release.yml` 触发构建发布。
- 版本号单一真相源（如 `package.json` / `setup.py`），其余引用它，不手抄多处。

### CHANGELOG 单一真相源

`CHANGELOG.md` 是变更的唯一人工维护源，其余皆为**派生物**，禁止单独维护、禁止把派生物提交入库：

```
CHANGELOG.md （人工维护，唯一真相源）
   ├─ release.yml 按 "## [version]" 段落截取 → GitHub Release body（构建时生成）
   └─ build 时嵌入二进制                      → 运行时 changelog 命令（见 CLI-SPEC.md §11）
```

- release-notes 由 `release.yml` 在发布时截取生成（`sed -n "/^## \[VERSION\]/,/^## \[/p"`），**不要在仓库里保留一份手维护的 `release-notes.md`**。
- 运行时 `changelog` 命令复用同一份 CHANGELOG，构建时嵌入——零新真相源，只是多一个出口。

**运行时 changelog 文件命名。** 为运行时 `changelog` 命令嵌入 `CHANGELOG.md` 的源文件命名为 `changelog.go`（Go）/ `changelog.py`（Python）——**单数，且不带 `_embed` 后缀**。Go 里它持有 `//go:embed` 指令，并把嵌入内容暴露为一个名为 `ChangelogMarkdown` 的包级变量。

**Go 版本钉法。** Go 项目在 `go.mod` 里钉一个当前 Go 版本（`go 1.XX`），且**不**加 `toolchain` 行；CI 通过 `setup-go` 的 `go-version-file: go.mod` 读取这唯一来源，而不是在 workflow 里写死版本号。

## 4b. 分发约定（npm 壳）

跨语言工具统一用 npm 壳分发，让 Go 二进制、Python 包都能 `npm install -g` 装、被 Agent 一致调用：

- `package.json` 声明 scope 包名、`bin` 入口与 OS/CPU 专属 optional 平台包。它是 **npm 壳的 scope/包名与版本号的单一真相源**——`scripts/run.js`、平台包生成脚本与 CI 都从 `package.json` 读取，绝不把 scope 或版本号手抄到别处。
- `scripts/run.js`：薄转发层，解析已安装的 npm 平台包并 `execFileSync` 调其中的二进制，透传 `argv` 与 stdio，平台包缺失时给出可执行的重装提示。
- `scripts/prepare-npm-platform-packages.js`：release 阶段把 CI 已构建的二进制打成 npm optional 平台包。npm 运行时安装不得从 GitHub 下载二进制。
- 二进制本身（`bin/`、`*.exe`、`dist/`）进 `.gitignore`，由 CI 构建产出，不入库。
- 发布产物附带 `checksums.txt`；release pipeline 通过 tagged GitHub Actions release workflow 使用 Sigstore/Cosign keyless 签署该文件，并把 bundle 与 checksum 一起发布。
- npm 分发发布主包和全部平台包，并使用 npm provenance。npm registry tarball integrity 与 provenance 覆盖 npm 安装路径；standalone GitHub 二进制更新路径继续保留 checksum/signature 验证。
- 版本通知是面向 Agent 的结构化契约，不是全局横幅。`update --check`
  刷新更新状态，并可在工具的用户配置目录写入短缓存；`doctor` 可以用短超时主动刷新，网络失败静默降级；`context` 和 `--help`
  只读缓存。业务命令不得为了提示升级而主动联网。
- 有新版本时，维护类命令的 JSON 在 `data.notices[]` 中追加
  `type: "update_available"`、当前/最新版本、安装方式、推荐命令、已知 release URL、检查时间和下一步。文本/help 输出可追加一句简短提示。
- `update --confirm` 负责完整生命周期：二进制/包更新加整个 `skills/<name>/` 目录同步，最终状态等同于 `npx skills add <repo> -y -g`。

## 5. 目录布局约定

```text
project/
├── README.md / README_zh.md
├── LICENSE / CHANGELOG.md / CONTRIBUTING.md / SECURITY.md
├── .gitignore
├── AGENTS.md                   # 跨工具入口钩子，指向 .agent/AGENT.md
├── .github/
│   ├── workflows/{ci,release}.yml
│   ├── ISSUE_TEMPLATE/
│   └── PULL_REQUEST_TEMPLATE.md
├── .agent/                     # AI 原生规范
│   ├── AGENT.md                # 入口/总纲
│   ├── CLI-SPEC.md             # CLI 机器契约
│   ├── SKILL-SPEC.md           # Skill 编写规范
│   └── SEC-SPEC.md             # 安全基线
├── skills/<name>/              # 内置 Agent Skill
│   ├── SKILL.md                # 可执行 Skill 契约
│   └── test-prompts.json       # Skill 审查回归 prompt
├── <package>/                  # 源码（包名）
├── tests/                      # 测试，目录结构镜像源码
├── scripts/                    # 开发 / 构建辅助脚本
└── <build manifest>            # 语言相关：package.json / setup.py / pyproject.toml ...
```

约定：

- 源码、测试、脚本、文档各归其位，不混放根目录。
- 测试目录结构镜像源码结构，便于定位。
- 构建产物、缓存、虚拟环境、IDE 配置一律进 `.gitignore`，不入库。

### 5b. 模板模型：`common/` + `<lang>/`

本仓库 `template/` 里的种子做了拆分：上面的骨架由两层拼装而成，加一门语言的成本也因此很低：

```text
template/
├── common/        # 语言无关，拷进每一个仓库：AGENTS.md、本地 .agent/ 规范、
│                  #   全部治理文档、.github/、package.json、
│                  #   scripts/{install,run}.js、docs/、skills/SKILL.md.tmpl、.gitignore
├── go/            # 叠加层：ci.yml、release.yml、.goreleaser.yml、Makefile、
│                  #   .golangci.yml、.gitignore
└── python/        # 叠加层：ci.yml、release.yml、ruff.toml、.gitignore
```

- 实例化方式：拷 `common/.`，再叠加**恰好一种** `<lang>/.`；把 `<lang>` 的 `.gitignore` 追加到通用那份后面。详见 `template/INSTANTIATE.md`。
- **加一门语言 = 加一个 `template/<lang>/`**（约 6 个文件：CI / release / 格式化 / 构建管线），100% 复用 `common/`。
- `.agent/` 的行为规范（`AGENT`、`CLI-SPEC`、`SKILL-SPEC`、`SEC-SPEC`）放在 `common/`，**绝不按语言分叉**——机器契约跨语言完全一致，不同的只有构建/CI 管线。

## 6. 质量门禁约定

- CI 必须跑 lint + 测试，红了不许合并。
- 统一格式化工具（按语言：Python 用 ruff，JS 用 prettier 等），配置入库，不靠人肉对齐。
- PR 模板内置自检项：测试通过、文档同步、CHANGELOG 已更、双语已同步。

## 7. 新建项目检查清单

- [ ] `README.md`（+ 按需 `README_zh.md`）按 §2 骨架
- [ ] `LICENSE` 选定
- [ ] `CHANGELOG.md` 初始化，含 `Unreleased`
- [ ] `CONTRIBUTING.md` / `SECURITY.md` 就位
- [ ] `.gitignore` 按语言配好
- [ ] `.github/workflows/ci.yml`（lint + test）就位
- [ ] 版本号单一真相源；CHANGELOG 为唯一变更源，派生物不入库
- [ ] 源码 / 测试 / 脚本目录分置，测试镜像源码
- [ ] 格式化工具配置入库，CI 强制
- [ ] （npm 壳分发）`package.json` + `scripts/run.js` + `scripts/prepare-npm-platform-packages.js`，二进制不入库
- [ ] （包装第三方产品）`NOTICE.md` + `docs/COMPATIBILITY.md` 就位
- [ ] （对接外部 API）`docs/E2E.md` + 集成测试就位
- [ ] `docs/OPEN_SOURCE_CHECKLIST.md` 就位，首次 push 前过一遍
- [ ] （AI 原生工具）根 `AGENTS.md` + `.agent/{AGENT,CLI-SPEC,SKILL-SPEC,SEC-SPEC}.md` + `skills/<name>/{SKILL.md,test-prompts.json}` 齐
- [ ] PR / Issue 模板就位
