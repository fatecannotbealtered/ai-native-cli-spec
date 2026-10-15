# ai-native-cli-spec

[English](README.md) · [中文](README_zh.md) · [MIT 许可证](LICENSE)

一份核心规范，适用于主要调用方是 AI Agent 的命令行工具，外加实现这份规范的模板和共享库。符合规范的工具以单个 JSON 信封作答；在每个错误中说明是否已有内容发出、下一步该做什么；在有人授予授权范围之前拒绝写入；只报告自己观察到的东西。

当前版本：2.0.0（2026-10-15）。改动见 [CHANGELOG.md](CHANGELOG.md)；1.6.x 工具的迁移见 [MIGRATION-2.0_zh.md](MIGRATION-2.0_zh.md)。

## 2.0 包含什么

| 组成部分 | 位置 | 给工具提供什么 |
|---|---|---|
| 5 份规范 | `template/common/.agent/` | `AGENT.md`（操作手册）、`CLI-SPEC.md`（机器契约，`CLI-xxx`）、`SEC-SPEC.md`（安全，`SEC-xxx`）、`SKILL-SPEC.md`（Agent Skills，`SKL-xxx`）、`VERIFY-SPEC.md`（验证与 Agent 评测，`VER-xxx`）。每条规范性规则都有稳定的 ID 和一行 `Check:`。 |
| 契约与 Schema | `template/common/contract/` | `contract.json`（错误码、退出码、枚举、键集合、回执规则）、覆盖每种结构的 JSON Schema（draft 2020-12），以及一套有效与无效示例语料。 |
| aicli 库 | `template/go/aicli/`（Go，cobra）、`template/python/aicli/`（Python ≥ 3.10，click） | 信封、错误与 HTTP 映射；由同一个注册表生成的 `--help`、`reference` 和 Skill 命令地图；Agent 体验（根级快捷方式、接受 Agent 的猜测写法、在整棵命令树中查找建议）；写入门控（write gate），包括绑定、带密钥的一次性令牌、操作记录、授权范围检查、回执、批量操作；内置命令 `reference`、`context`、`doctor`、`changelog`、`operation` 和 `update`，后者是完整的自更新，包括签名校验和 Skill 同步；设置与配置档。 |
| 符合性测试套件 | 位于每个库内部（`RunConformance`、`run_conformance`） | 对照写入门控检查每条写命令（`G00`–`G10`），对照契约检查整个工具（`R01`–`R05`）。`R06`（注册表中的每条命令都在工具自己的测试中运行过）单独运行：等这些测试跑完，再由 `CheckCoverage`（`check_coverage`）执行。每个工具都在 CI 中运行这两项检查。 |
| 示例工具 | `template/go/example/`、`template/python/example/` | 基于该库构建的小型「notes」CLI，附带它的符合性测试。在工具群中有工具基于 2.0 发布之前，它就是参考实现。 |
| 仓库标准 | [`REPO-SPEC.md`](REPO-SPEC_zh.md) | 工具仓库的文件、布局、版本管理、分发、内置副本和 CI（`REPO-xxx`）。 |
| 迁移指南 | [`MIGRATION-2.0.md`](MIGRATION-2.0_zh.md) | 把 1.6.x 工具一步步迁移到 2.0。 |

工具以副本方式引入规范、契约、Schema 和库，固定在同一个规范 tag 上，并在 CI 中做漂移检查。库不以软件包形式发布。

2.0 不涵盖的内容：桌面应用和设备的驱动层（会话、物理效果、未保存的状态），它留在构建于这套核心之上的 tool-driver-cli-spec 中；以及 MCP server 生成（属于后续工作）。

## 为什么要做 2.0

2.0 建立在 2026 年 10 月的 2 次测量之上。一次是对 gitlab-cli 做的 Agent 评测（720 次试验，3 个 Claude 模型，分别开启和关闭 Skill）。与同一批次中运行的 gitlab-cli 1.2.17 相比，2.0 的设计在任务成功率相同的情况下，CLI 调用少 31%，无效调用少 87%，墙钟时间少 24%。另一次是对工具群做的只读审计，它发现：在 10 个带确认门控的工具中，没有一个的确认令牌绑定了完整请求，其中 9 个的令牌还能被伪造。所以 2.0 的写入门控是带符合性测试套件的共享代码，不再是一条由每个工具各自实现的规则。方法见 `VERIFY-SPEC.md`：VER-006（Agent 评测）和 VER-007（门控审计）。

## 快速上手：新建工具

```bash
# A checkout of the spec at the release you will pin.
git clone --depth 1 --branch v2.0.0 https://github.com/fatecannotbealtered/ai-native-cli-spec spec
mkdir my-cli && cd my-cli
# The common layer, then only the CI and build files of one overlay (never its aicli/ or example/).
cp -R ../spec/template/common/. .
cp -R ../spec/template/go/.github ../spec/template/go/.golangci.yml ../spec/template/go/.goreleaser.yml ../spec/template/go/Makefile .
cat ../spec/template/go/.gitignore >> .gitignore
# Python instead: cp -R ../spec/template/python/.github ../spec/template/python/ruff.toml . && cat ../spec/template/python/.gitignore >> .gitignore
# Files that belong to the spec repository only.
rm -f scripts/gen-contract.js scripts/build-manifest.js scripts/check-contract.py && rm -rf contract/examples
```

以上是 [`template/INSTANTIATE.md`](template/INSTANTIATE_zh.md) 的第 1 步，其余步骤照它继续：填好占位符；用 `scripts/sync-spec.js` 以副本方式引入库（它写入 `internal/aicli/` 或 `<pkg>/_aicli/`；不要手工复制 `aicli/`）；然后告诉你的 Agent：「先读 AGENTS.md，再按 .agent/AGENT.md 中的工作流 A 构建这个工具」。规范仓库中的示例工具（`template/go/example/`、`template/python/example/`）展示了完成后的样子，请在规范仓库里阅读它们。

## 快速上手：迁移 1.6.x 工具

在 `.agent/SPEC_VERSION` 中固定 `v2.0.0`，用 2.0 版的 `scripts/sync-spec.js` 以副本方式引入 2.0 的文件，然后按顺序完成 [`MIGRATION-2.0.md`](MIGRATION-2.0_zh.md) 中的各个步骤。写入路径什么时候算改完，符合性测试套件会告诉你。

## 仓库结构

```text
README.md, MIGRATION-2.0.md, REPO-SPEC.md   (+ _zh translations)
CHANGELOG.md                                (English only)
template/
├── INSTANTIATE.md          how to create a tool repo from the template (+ _zh)
├── common/                 copied into every tool
│   ├── .agent/             AGENT, CLI-SPEC, SEC-SPEC, SKILL-SPEC, VERIFY-SPEC (+ _zh)
│   ├── contract/           contract.json, schemas/, examples/{valid,invalid}/
│   ├── scripts/            sync-spec, check-spec, spec-files, gen-contract, build-manifest, check-contract.py, version tooling, npm launcher
│   ├── skills/             SKILL.md and test-prompts.json templates
│   └── README, SECURITY, CONTRIBUTING, AGENTS.md (+ _zh), package.json, .github/ ...
├── go/                     Go overlay: CI, release, Makefile, linters
│   ├── aicli/              the Go library, its tests and MANIFEST.json
│   └── example/            example tool with a conformance test
└── python/                 Python overlay: CI, release, ruff config
    ├── aicli/              the Python library, its tests and MANIFEST.json
    └── example/            example tool with a conformance test
tools/                         agent-eval/ (VER-006 harness), gate-audit/ (VER-007 workflows); not vendored
skills/review-ai-native-cli/   Skill that reviews a tool repo against this spec
.github/workflows/             this repo's CI: library tests, examples, contract check, tools
```

## 版本管理

- 每次发布都是一个 git tag `vMAJOR.MINOR.PATCH`。一个 tag 为上表中的全部内容定版本；工具在 `.agent/SPEC_VERSION` 中固定一个 tag，得到的规范、契约、Schema 和库就是一起测试过的那一套。
- `contract.json` 带有 `spec_version`（`2.0.0`）和 `contract_version`（`2.0`）。信封的 `schema_version` 是 `"2.0"`，只在信封出现破坏性变更时才升主版本（CLI-080）。
- 会破坏符合规范的工具或其 Agent 的变更，要升 MAJOR。规则 ID 永不改变含义，也永不复用。
- 历史记录：[CHANGELOG.md](CHANGELOG.md)。英文是源文本，`*_zh.md` 文件是它的译文。

## 许可证

[MIT](LICENSE)。版权所有 © 2026 Sean Guo。
