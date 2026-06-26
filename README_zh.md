<h1 align="center">ai-native-cli-spec</h1>

<p align="center">
  <strong>用于构建 AI-native CLI 的复制即用种子规范</strong>
</p>

<p align="center">
  <a href="README.md">English</a> &middot; <a href="README_zh.md">中文</a>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-7C3AED?style=for-the-badge"></a>
</p>

<p align="center">
  <img alt="Agent native" src="https://img.shields.io/badge/agent-native-111827?style=for-the-badge">
  <img alt="AI-native CLI spec" src="https://img.shields.io/badge/spec-AI--native%20CLI-0891B2?style=for-the-badge">
  <img alt="Copy-paste seed" src="https://img.shields.io/badge/seed-copy--paste%20ready-F59E0B?style=for-the-badge">
</p>

一套**规范 + 可直接拉取的种子**，用来构建 **AI 原生 CLI 工具**——专为 AI Agent 可靠驱动而设计的命令行工具，而不只是给人用。

把 `.agent/` 种子拉进新项目，让你的 AI 照着它干，产出的工具开箱就符合一套稳定的 agent 契约。

模板拆成两层：语言无关的 **`common/`** 层，加上一层很薄的按语言叠加层（**`go/`**、**`python/`** …）。先拉 `common/`，再叠加**恰好一种**语言：

```bash
# 在新的（或已有的）项目目录里：
#   1. 语言无关的通用层
npx degit fatecannotbealtered/ai-native-cli-spec/template/common .
#   2. 恰好一种语言叠加层（go 或 python，二选一）
npx degit fatecannotbealtered/ai-native-cli-spec/template/go .

# 然后照 template/INSTANTIATE.md 填占位符，并对你的 AI 说：
#   "读 AGENTS.md，然后按 .agent/ 规范把这个工具搭起来。"
```

完整的复制即用指南见 [`template/INSTANTIATE.md`](template/INSTANTIATE.md)（占位符字典、`.gitignore` 拼接、Skill 模板重命名、`test-prompts.json` 设置、不包装第三方就删 `NOTICE.md` 的规则）。

这会往你的仓库里放下一套连贯的骨架。来自 `common/`：

```
AGENTS.md          # 每个 agent 最先读的入口钩子
.agent/
├── AGENT.md       # 总纲：导航 + 新建/扩展工作流
├── CLI-SPEC.md    # CLI 机器契约——工具"怎么说话"
├── SKILL-SPEC.md  # Skill 编写规范——agent"怎么听"
└── SEC-SPEC.md    # 安全基线——怎么不被坑、不坑人
README / LICENSE / CHANGELOG / CONTRIBUTING / SECURITY / CODE_OF_CONDUCT / NOTICE
.github/（PR + issue 模板、dependabot）  ·  package.json + scripts/{run,prepare-npm-platform-packages}.js
docs/OPEN_SOURCE_CHECKLIST.md  ·  skills/{SKILL.md.tmpl,test-prompts.json.tmpl}  ·  .gitignore
```

外加：你选的那一种语言叠加层（`go/` 或 `python/`）带来的 CI / release 工作流、格式化与 lint 配置、构建管线，以及一份待追加的语言级 `.gitignore`。仓库骨架标准本身则放在 [`REPO-SPEC.md`](REPO-SPEC.md)——本规范的元文档。

## 为什么

LLM Agent 时刻在调 CLI，但绝大多数 CLI 是给人用的：散文式输出、随意的退出码、在自动化里会卡死的交互提示。这套规范把它们拧成**一套连贯、有主见的标准**，让**你建的每个工具都按 agent 预期的方式行事**，也让 AI 从第一天就能照着建新工具，不必每次重新推导规则。

它刻意做到**分层、而非笨重**：

- **一个入口**（`AGENT.md`）→ 本地聚焦规范 + 共享仓库骨架规范，只读任务需要的那份。
- **风险分级 + 可选模式**。只读工具保持轻量；执行 SQL、持有会过期令牌的工具才按需加码。规范随工具复杂度伸缩。

## 它统一了什么

- **Agent-safe 输出契约**——stdout 只出一个 JSON 文档，统一 `ok` / `schema_version` / `data` / `meta` 信封，日志走 stderr。
- **结构化错误**——稳定的 `E_*` 码 ↔ 退出码 ↔ `retryable`，让 agent 能判断重试 / 改参 / 求助于人。
- **写操作安全**——`--dry-run` → `--confirm <token>`，token 绑定操作内容。
- **自描述**——`reference` / `context` / `doctor` / `changelog`，让 agent 发现能力、并在自更新后知道"变了什么"。
- **Skill**——对齐 Anthropic 的 `SKILL.md` 编写规范、渐进式披露、版本协商。
- **安全**——不可信内容/注入防护、最小权限、凭证落盘、供应链。
- **发布质量门禁**——README / Skill / reference / help / context / doctor / changelog / update 中声明的公开行为必须达到 Functional Contract Coverage = 100%；数字代码覆盖率是辅助指标。
- **仓库与分发**——必备文档、双语 README 约定、CHANGELOG 单一真相源、npm 壳分发。

## 怎么用

- **加进已有仓库：** 先拉 `template/common`，再叠加恰好一种 `template/<lang>`：
  ```bash
  npx degit fatecannotbealtered/ai-native-cli-spec/template/common .
  npx degit fatecannotbealtered/ai-native-cli-spec/template/go .   # 或 template/python
  ```
- **固定版本：** 在每条路径后加 `#v1.5.0`，如 `.../template/common#v1.5.0 .`
- **从零起新仓库：** 在 GitHub 上点 **Use this template**，然后只保留你需要的那一种 `template/<lang>`。

然后照 [`template/INSTANTIATE.md`](template/INSTANTIATE.md) 填好占位符，填好 `AGENTS.md` 底部的 `## 本项目` 一节，让 agent 照着 `.agent/AGENT.md` 里的**工作流 A（新建工具）**走。

## 参考实现

[**outlook-cli**](https://github.com/fatecannotbealtered/outlook-cli)——一个按本规范构建的 Outlook/Exchange CLI。可作为信封、dry-run/confirm 流程、自描述命令、`changelog` 机制的完整范例。

## 语言

每份文档都是双语：英文为主 `X.md`，中文为 `X_zh.md`，两者顶部互相挂切换链接。READMEs、`AGENTS.md`、`.agent/` 本地规范以及根部 `REPO-SPEC.md` 均如此。

## 许可

[MIT](LICENSE)
