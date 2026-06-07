# AI 原生开源项目仓库规范

**English → [REPO-SPEC.md](REPO-SPEC.md)**

本文定义个人所有开源项目的统一仓库标准：必备文档、模板文件、目录布局、发布约定。目标是让开十个项目长一个样，新人（含 AI Agent）进任何一个仓库都能在固定位置找到固定的东西。

配套规范（均在 `.agent/` 下，入口是 `AGENT.md`）：

- `AGENT.md` —— 入口/总纲：怎么在这建&扩工具，导航到下面四份。
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
| `.agent/AGENT.md` | MUST* | AI 原生工具入口/总纲，导航到下面四份规范 |
| `.agent/CLI-SPEC.md` | MUST* | CLI 工具的机器契约（*仅 CLI 类项目） |
| `.agent/SKILL-SPEC.md` | MUST* | Skill 编写规范（*带 Skill 的项目） |
| `.agent/SEC-SPEC.md` | MUST* | 安全基线（风险分级 + 注入/权限/凭证/供应链） |
| `skills/<name>/SKILL.md` | MUST* | Agent Skill（*AI 原生工具必备） |

带 `*` 的是「AI 原生工具」专属——普通库可不带，但凡是给 Agent 用的 CLI / 工具，这几件是硬指标。带 `†` 的按场景触发：包装第三方产品（Outlook/Exchange、GitLab、Jira、Kibana 等）就必须有商标声明与兼容矩阵。

## 2. README 必备骨架

每个 README 按固定顺序，便于横向对比：

1. **标题 + 一句话定位** + 徽章（版本 / CI / license / 语言切换链接）
2. **What** —— 一段话讲清解决什么问题，给谁用（人 / Agent / 两者）
3. **Install** —— 复制即用的安装块（CLI 装、Skill 装分开列）
4. **Quick Start** —— 最小可用示例（配置 → 验证 → 第一条命令）
5. **Usage / Commands** —— 核心能力，细节指向 `reference` 或独立文档
6. **Configuration** —— 配置项、环境变量、凭证位置
7. **For AI Agents** —— Agent 接入说明，指向 `SKILL.md` 与 `.agent/CLI-SPEC.md`
8. **Development** —— 本地开发、构建、测试
9. **License / Contributing / Security** —— 指向对应文件

约定：

- 安装块必须可复制即跑，标注前置依赖。
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

## 4b. 分发约定（npm 壳）

跨语言工具统一用 npm 壳分发，让 Go 二进制、Python 包都能 `npm install -g` 装、被 Agent 一致调用：

- `package.json` 声明 scope 包名与 `bin` 入口。
- `scripts/install.js`：安装时拉取 / 链接对应平台二进制到 `bin/`。
- `scripts/run.js`：薄转发层，`execFileSync` 调真正的二进制，透传 `argv` 与 stdio，二进制缺失时给出可执行的重装提示。
- 二进制本身（`bin/`、`*.exe`、`dist/`）进 `.gitignore`，由 CI 构建产出，不入库。

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
│   ├── SEC-SPEC.md             # 安全基线
│   └── REPO-SPEC.md            # 仓库骨架规范
├── skills/<name>/SKILL.md      # Agent Skill
├── <package>/                  # 源码（包名）
├── tests/                      # 测试，目录结构镜像源码
├── scripts/                    # 开发 / 构建辅助脚本
└── <build manifest>            # 语言相关：package.json / setup.py / pyproject.toml ...
```

约定：

- 源码、测试、脚本、文档各归其位，不混放根目录。
- 测试目录结构镜像源码结构，便于定位。
- 构建产物、缓存、虚拟环境、IDE 配置一律进 `.gitignore`，不入库。

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
- [ ] （npm 壳分发）`package.json` + `scripts/{install,run}.js`，二进制不入库
- [ ] （包装第三方产品）`NOTICE.md` + `docs/COMPATIBILITY.md` 就位
- [ ] （对接外部 API）`docs/E2E.md` + 集成测试就位
- [ ] `docs/OPEN_SOURCE_CHECKLIST.md` 就位，首次 push 前过一遍
- [ ] （AI 原生工具）根 `AGENTS.md` + `.agent/{AGENT,CLI-SPEC,SKILL-SPEC,SEC-SPEC,REPO-SPEC}.md` + `skills/<name>/SKILL.md` 齐
- [ ] PR / Issue 模板就位
