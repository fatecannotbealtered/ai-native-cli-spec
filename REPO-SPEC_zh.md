# AI 原生 CLI 工具的仓库规范

**English → [REPO-SPEC.md](REPO-SPEC.md)**

这是基于 ai-native-cli-spec 2.0 构建的工具所遵循的仓库标准：工具仓库有哪些文件、各放在哪里，版本、发布和分发怎样运作，规范和 aicli 库怎样以副本方式引入，以及 CI 运行什么。目标是让每个工具仓库都长得一样，人或 Agent 在其中任何一个仓库里，都能在同样的位置找到同样的东西。

本文件位于规范仓库的根目录，不以副本方式引入工具；`.agent/AGENT.md` 链接到它。以副本方式引入工具的配套文件有：

- `.agent/AGENT.md`：操作手册和入口。
- `.agent/CLI-SPEC.md`：机器契约（`CLI-xxx`）。
- `.agent/SEC-SPEC.md`：安全（`SEC-xxx`）。
- `.agent/SKILL-SPEC.md`：Skill（`SKL-xxx`）。
- `.agent/VERIFY-SPEC.md`：验证与 Agent 评测（`VER-xxx`）。

规范仓库还包含 `tools/`：VER-006 的 Agent 评测框架（`tools/agent-eval/`）和 VER-007 的门控审计工作流（`tools/gate-audit/`）。工具从不以副本方式引入它们；这两条规则要求的证据就是用它们产出的。

本文的规则为 `REPO-xxx`。「必须」（MUST）、「不得」（MUST NOT）、「应当」（SHOULD）、「可以」（MAY）取 RFC 2119 中的含义；标有 † 的条目只在其条件成立时适用。ID 保持稳定，从不重新编号。

## 1. 文件

### REPO-001 必备文件

工具仓库必须包含标为「必须」的文件，应当包含标为「应当」的文件。「内置副本」文件从固定 tag 的规范中复制而来（REPO-061），从不编辑（REPO-063）。

| 文件 | 级别 | 作用 |
|---|---|---|
| `README.md` | 必须 | 门面，骨架见 REPO-010 |
| `README_zh.md` | 应当 | 译文（REPO-020） |
| `LICENSE` | 必须 | 默认 MIT |
| `CHANGELOG.md` | 必须 | Keep a Changelog 格式；`changelog` 命令的来源（REPO-032） |
| `CONTRIBUTING.md`、`SECURITY.md` | 必须 | 贡献流程；漏洞披露渠道、受支持的版本、风险档（SEC-001） |
| `CODE_OF_CONDUCT.md` | 应当 | Contributor Covenant |
| `NOTICE.md` | 必须† | 商标声明（†包装第三方产品时） |
| `docs/COMPATIBILITY.md` | 必须† | 已验证的上游版本（†对接外部系统时） |
| `docs/E2E.md` | 应当† | 真实测试环境和运行记录，即 `verified` 成熟度背后的证据（VER-005）（†调用外部 API 时） |
| `docs/OPEN_SOURCE_CHECKLIST.md` | 应当 | 首次公开推送前的检查清单 |
| `.gitignore`、`.gitattributes` | 必须 | 构建产物、缓存、IDE 文件；行尾符 |
| `.github/workflows/ci.yml`、`release.yml` | 必须 | 合并门禁（REPO-070）、发布门禁（REPO-071） |
| `.github/ISSUE_TEMPLATE/`、`PULL_REQUEST_TEMPLATE.md` | 应当 | issue 表单、PR 自检（REPO-074） |
| `AGENTS.md` | 必须 | 所有 Agent 的入口钩子；指向 `.agent/AGENT.md` |
| `.agent/{AGENT,CLI-SPEC,SEC-SPEC,SKILL-SPEC,VERIFY-SPEC}.md`（+ `_zh`） | 必须，内置副本 | 5 份规范 |
| `.agent/SPEC_VERSION` | 必须 | 固定的规范版本（REPO-060） |
| `contract/contract.json`、`contract/schemas/*.schema.json` | 必须，内置副本 | 机器契约和 JSON Schema |
| `contract/contract-ext.json` | 必须† | 工具专属错误码（†工具有这类错误码时）（REPO-064） |
| Go：`internal/aicli/*.go`、`internal/aicli/LICENSE`、`internal/aicli/MANIFEST.json` | 必须，内置副本 | aicli 库、它的许可证和文件哈希 |
| Python：`<pkg>/_aicli/**`、`<pkg>/_aicli/LICENSE`、`<pkg>/_aicli/MANIFEST.json` | 必须，内置副本 | aicli 库、它的许可证和文件哈希 |
| `scripts/{spec-files,sync-spec,check-spec}.js` | 必须，内置副本 | 同步与漂移检查 |
| `scripts/{version-files,sync-version,check-version}.js`、`.npmrc` | 必须 | 单一版本来源（REPO-031） |
| `package.json`、`scripts/{run,prepare-npm-platform-packages}.js` | 必须† | npm 壳（†通过 npm 分发时，REPO-040） |
| 一个符合性测试，例如 `cmd/conformance_test.go`、`tests/test_conformance.py` | 必须 | 为每条写命令提供样例并运行测试套件（VER-001）；报告覆盖及单独运行的 R06 结果（VER-003） |
| `skills/<name>/SKILL.md`、`skills/<name>/test-prompts.json` | 必须 | Skill 及其评测提示词（SKL-011） |

以下内容在 2.0 中退役，2.0 工具一样都没有：FCC 源码扫描守卫 `cmd/fcc_guard_test.go` 和 `tests/test_fcc_guard.py`（由 VER-003 取代）；每个工具各自的契约模块 `internal/contract/contract_gen.go` 和 `<pkg>/contract_gen.py`（库自带生成好的契约）；工具自己的确认令牌、密钥和已消费令牌代码（CLI-050）；工具自己的 `update` 代码，由 aicli 的内置命令取代（CLI-070）；代码和文档中的 `release_readiness`（CLI-027）。

原因：一份统一的清单让任何人都能在每个仓库中找到同一个文件，也让评审 Skill 能机械地给仓库评分。
检查：`review-ai-native-cli` 静态评审；缺少内置副本文件时 `check-spec.js` 失败。

## 2. README

### REPO-010 README 骨架

工具的 README 必须按以下顺序包含这些章节：

1. **标题**、语言切换、徽章：CI、语言质量徽章（如适用）、npm 版本、许可证。不放其他徽章。
2. **一句话**，把工具定位为面向 Agent 的工具。
3. **Agent Install**（Agent 安装）：一个可直接复制粘贴的代码块，它安装 CLI，用固定版本的安装器从已安装的包中安装同版本的 Skill（`npx skills@<version> add "$(npm root -g)/<pkg>" -y -g`，SKL-009），并设置凭证占位符。它不先运行 `context`、`doctor` 或 `reference`（CLI-034）。
4. **What It Does**（它做什么）：一段话，写明风险档（SEC-001）、工具可能产生的效果（CLI-025），以及第三方免责声明（如有）。
5. **Authorization**（授权）：在有人授予授权范围文件之前，写入一律被拒绝（SEC-010）；写明该文件的路径（`<PREFIX>_SCOPE_FILE`，未设置时为 `<state_dir>/scope.json`）、一份最小的授予内容，以及 SEC-013 的保护声明。
6. **Capabilities**（能力）：命令地图，每个名词一行，带上它的动词：即 `CommandMap()` 的输出，放在与 Skill 相同的 `aicli:command-map` 标记之间，由同一个测试检查（SKL-002）。
7. **Agent Workflow**（Agent 工作流）：直接运行任务命令；用 `--help`、`reference --index` 和 `reference --command <path>` 发现命令；用 `--fields` 收窄输出；写入时先带 `--dry-run` 运行，再去掉它、加上 `--confirm <token>` 运行同一条命令（两个 flag 都不带的裸调用，也会返回同样的预览和令牌），需要时再加 `--dangerous`；回复丢失后运行 `operation show --id <operation_id>`；`update` 之后运行 `changelog --since <previous_version>`。
8. **Machine Contract**（机器契约）：固定的规范版本、`schema_version` `"2.0"`、信封、错误详情（`dispatch_state`、`next_action`）、`meta.untrusted`，以及指向 `.agent/CLI-SPEC.md` 的链接。
9. **Configuration**（配置）：各项设置及其环境变量（凭证、`<PREFIX>_STATE_DIR`、`<PREFIX>_SCOPE_FILE`、`<PREFIX>_SETTINGS_FILE`、`<PREFIX>_PROFILE`、`<PREFIX>_CA_FILE`）、设置文件及其中的配置档（CLI-100、CLI-101），以及凭证存储方式（SEC-040）。
10. **Uninstall**（卸载）：怎样移除工具安装或留下的一切（REPO-011）。
11. **Project Structure**（项目结构）：顶层目录树（REPO-050）。
12. **Development**（开发）：构建、测试、符合性测试和 lint 命令。
13. **Links**（链接）：`AGENTS.md`、Skill、`.agent/CLI-SPEC.md`、`SECURITY.md`、`docs/COMPATIBILITY.md`、`docs/E2E.md`、`CHANGELOG.md`、`CONTRIBUTING.md`、`NOTICE.md`、`LICENSE`。

原因：安装块是 Agent 最先运行的东西；在 2026 年 10 月的 gitlab-cli 评测中，Skill 强制要求的 `context`/`doctor`/`reference` 引导步骤每个任务耗费 1.6 次调用，准确率却没有提高。
检查：评审：各标题齐全且顺序正确；安装块在干净的 shell 中能运行。

### REPO-011 README 内容

- 安装块必须粘贴即可运行，机密信息用占位符，绝不放真实凭证。
- README 是一张地图。flag、Schema 和错误码表放在 `reference` 和 `--help` 中；README 不得复制它们。
- 示例使用可运行的形式、ISO 8601 UTC 时间和占位 ID（`<merge-request-ref>`）。
- README 告诉用户怎样授予授权范围。它不得让 Agent 创建或编辑授权范围文件（SKL-006）。
- Uninstall 一节必须覆盖工具留下的一切：包或二进制；状态目录（`~/.<tool>`，或 `<PREFIX>_STATE_DIR`）及其中的密钥、操作记录、授权范围文件和设置文件；操作系统钥匙串中的凭证（对每个配置档运行 `<tool> auth logout`，或使用钥匙串自己的管理工具）；放在别处的设置文件或授权范围文件（`<PREFIX>_SETTINGS_FILE`、`<PREFIX>_SCOPE_FILE`）；已安装的 Skill 目录（`~/.agents/skills/<name>`、`~/.claude/skills/<name>`）；对独立二进制而言，还有它旁边的 Skill（`<exe dir>/skills/<name>`，`update` 会刷新它们），以及在 Windows 上更新可能留下的 `<exe>.old*` 文件。

原因：复制过来的 flag 列表会过时；让 Agent 自己写授权范围的 README，会让 SEC-010 失效；卸载后若把凭证留在钥匙串里、把 Skill 留在 Agent 的目录里，一个已经不在的工具就留下了访问权限和指令。
检查：评审。

## 3. 语言

### REPO-020 英文为源，中文为译

英文 `X.md` 是源文本。中文 `X_zh.md` 根据英文定稿翻译，两者在顶部互相链接。README、`AGENTS.md` 和以副本方式引入的 `.agent` 规范是双语的；治理类文档（CONTRIBUTING、SECURITY、CODE_OF_CONDUCT）可以是双语的。改动了双语英文文件的发布，必须同时发布更新后的译文。

原因：单一来源不会自相矛盾；只翻译定稿，就不必翻译中途反复的改动。
检查：两个文件都存在且互相链接；CI 可以比对两者的标题结构。

## 4. 版本与发布

### REPO-030 语义化版本

工具版本遵循 SemVer。破坏 Agent 所依赖内容（信封、键、错误码、命令路径或参数、写入流程）的变更要升 MAJOR；把工具从规范 1.x 迁移到 2.0 就是这样的变更。工具版本、固定的规范版本（`.agent/SPEC_VERSION`）和 `schema_version` 是 3 个相互独立的编号（CLI-080）。

原因：Skill 和 `doctor` 依据工具版本（`metadata.min_version`）确定自己的预期。
检查：`CHANGELOG.md` 中的破坏性条目位于新的 MAJOR 版本之下。

### REPO-031 单一版本来源

`package.json` 的 `version` 是唯一手工编辑的版本号。`npm version <patch|minor|major|x.y.z>` 会运行 `scripts/sync-version.js`，由它重写 `scripts/version-files.js` 列出的每一处：`optionalDependencies` 中固定的版本、`package-lock.json` 的版本字段（含各平台子条目）、每个 Skill 的 `metadata.version` 和 `metadata.min_version`（SKL-001）、Python 的 `__version__`，以及把 `## [Unreleased]` 滚动为带日期的标题。

- `.npmrc` 设置 `git-tag-version=false`：升版本只改文件；CI 变绿之后由人来打 tag。
- `scripts/check-version.js` 在 `ci.yml` 和 `release.yml` 中运行，发现任何不一致都会失败。
- 测试在运行时读取版本号，从不把它写成字面量。
- 升版本时逐个字段重写 lockfile，绝不使用 `npm install --package-lock-only`，因为它会把开发者自己的 registry 镜像写进公开文件。第一份 lockfile 用 `--registry=https://registry.npmjs.org/` 一次性创建，提交之前检查其中每一个 `resolved` URL（`template/INSTANTIATE.md` 第 3 步）。

原因：手工抄写的版本号会漂移；这道检查让漂移既合并不进去，也发布不出去。
检查：两个工作流中都运行 `node scripts/check-version.js`。

### REPO-032 CHANGELOG 是唯一的变更来源

`CHANGELOG.md` 遵循 Keep a Changelog：最上面是 `## [Unreleased]`，下面是各个 `## [x.y.z] - YYYY-MM-DD` 小节，分为 `Added`、`Changed`、`Fixed`、`Deprecated`、`Removed`、`Security`。其他一切都是派生的，不提交入库：发布工作流把该版本的小节截取为 release 正文，构建时把这个文件嵌入，供 `changelog` 命令使用（CLI-037）。Go 在 `changelog.go` 中把它嵌入为 `ChangelogMarkdown`，再传给 `App.Changelog`；Python 在 `changelog.py` 中做同样的事。仓库里没有 `release-notes.md`。

原因：Agent 在 `update` 之后读到的文字，和人在 release 页面上读到的是同一份。
检查：没有提交入库的 release-notes 文件；有测试针对嵌入的文件运行 `changelog --since <previous>`。

### REPO-033 tag 与工具链

一次发布就是一个 git tag `vX.Y.Z`，在被打 tag 的提交上 CI 变绿之后推送；`release.yml` 只从 tag 构建、签名和发布。Go 工具在 `go.mod` 中固定一个 Go 版本（`go 1.XX`，没有 `toolchain` 行），工作流用 `go-version-file: go.mod` 读取它。

原因：从打了 tag 的源码构建，可以追溯（SEC-050）；工具链只有一个来源，`govulncheck` 的发现可以推动它变更。
检查：`release.yml` 由 `v*` tag 触发；工作流中没有写死任何 Go 版本。

## 5. 分发

### REPO-040 npm 壳

工具应当通过 npm 壳分发，让 Go 和 Python 工具以同样的方式安装。采用这种方式分发的工具必须遵守以下规则：

- `package.json` 是包的 scope 和包名、`bin` 名称以及版本号的唯一来源；脚本和 CI 都从它读取。
- 二进制以按平台拆分的包发布，列在 `optionalDependencies` 中，由 CI 构建，发布时由 `scripts/prepare-npm-platform-packages.js` 打包。安装过程不会从任何别的地方下载二进制，也没有任何安装脚本拉取代码（SEC-050）。
- `scripts/run.js` 解析出平台包，用 `execFileSync` 运行其中的二进制，原样透传 argv 和 stdio；平台包缺失时打印一条重新安装的命令。
- 二进制（`bin/`、`dist/`、`*.exe`）是构建产物，从不提交入库。

原因：整个工具群只有一条安装路径，并且这条路径带有 registry 的完整性校验和来源证明（provenance）。
检查：`package.json` 的 `bin` 和 `optionalDependencies`；`.gitignore` 覆盖构建产物。

### REPO-041 启动器耗时预算

Agent 从 PATH 运行的命令应当是原生二进制，或者是一个最多只增加 100 ms 的启动器（CLI-090）。CI 应当在每个发布平台上测量这一点：经由已安装的命令运行 `<tool> --version` 至少 10 次取中位数，减去直接运行二进制得到的同类中位数；超过 100 ms 则该任务失败。npm 启动器超出预算时，README 的 Agent Install 一节应当为 Agent 把原生二进制放到 PATH 上，例如使用签过名的独立二进制，或者用一个不联网的安装步骤，从已安装的平台包中链接出该二进制。

原因：Agent 的每次调用都是一次往返；在工具群的 Windows 测试机上，经由 npm 启动器的一次调用耗时 1.1–2.3 s，单独运行二进制只要 0.4–0.7 s。
检查：CI 中的启动器预算任务；启动器超出预算时，README 写明原生路径。

### REPO-042 签名的发布物

发布归档附带 `checksums.txt`，它在由 tag 触发的发布工作流中用 Sigstore 无密钥签名（`cosign sign-blob --new-bundle-format`）签署，签名 bundle（`checksums.txt.sigstore.json`）与它一同发布；npm 包带来源证明发布。签名方式与 1.x 相同。在二进制安装路径上，aicli 的 `update` 在进程内对照 `UpdateConfig.Workflow`（默认 `release.yml`）指定的确切工作流校验签名，失败即拒绝（fail closed），返回 `E_INTEGRITY`（CLI-070、SEC-050）。

原因：校验和只能证明字节与某个文件一致；只有签名才能证明文件来自发布者。
检查：`release.yml` 先签名再发布；aicli 的 `update` 测试覆盖签名缺失和签名无效两种情况。

### REPO-043 Skill 随工具发布

每个分发渠道都必须携带与自身版本一致的 Skill：npm 包在 `files` 中列出 `skills/`；发布归档命名为 `<tool>-<version>-<os>-<arch>.tar.gz`（Windows 上为 `.zip`），根目录下放着二进制和 `skills/`。安装和 `update` 都从那里获取 Skill，绝不从经网络拉取的分支获取：aicli 的 `update` 用新归档中的 `skills/` 替换 `<exe dir>/skills`，再从那里或从 npm 包把 Skill 复制到已经安装了它们的 Skill 目录中（SKL-009、CLI-070）。

原因：1.x 规定每次更新都运行 `npx skills add <repo> -y -g`，它装的是默认分支上当时的内容，而不是为已安装二进制编写的那份 Skill。
检查：`npm pack --dry-run` 列出 `skills/`；发布归档的根目录下列出二进制和 `skills/`；aicli 的 `update` 测试表明 Skill 同步不发出任何网络请求。

### REPO-044 三端产品交付

每个 CLI 产品（包括软件服务 CLI）都必须提供真实可用的 Windows、macOS 和 Linux 包，每端至少明确一种 CPU 架构。发布支持矩阵必须区分操作系统、CPU、最低系统版本以及已测试、未测试或模拟运行的组合。其他架构、系统版本和故障恢复场景可以迭代增加，不是通用首发前提；既有支持组合不得悄悄消失。

每个宣称支持的组合都必须在干净或隔离环境中安装实际版本化归档，启动可执行文件，并针对真实或适当的受控目标执行代表性的关键产品操作。记录包摘要、源码版本、环境、命令和结果。已知安装失败或关键路径不可用时，先修复再宣称该组合支持；否则明确受影响的组合或操作不可用。未运行或失败不能写成通过。包发布表达通过上述冒烟的产品支持，不等于所有命令达到某个 VER-005 成熟度，也不等于工程设计最终签核。

二进制应尽量自包含应用运行依赖。必须列出外部应用、服务、许可、系统库、最低操作系统与 CPU 等必要前提。直接二进制的前提与 npm 壳所需的 Node.js 运行时分别说明。原应用仅能在特定宿主运行时，区分三端 CLI 客户端与远端宿主、协议和宿主架构；包必须能完成真实支持的远端任务，不能只是能启动的空壳。不得宣称原应用可在不支持的系统原生运行。

检查：发布矩阵包含三种系统且各至少一种架构；实际发布归档的安装、启动及关键操作冒烟结果绑定包摘要。签名、依赖审计和授权要求保持不变（REPO-042、SEC-010、SEC-050）。

## 6. 目录布局

### REPO-050 目录布局

```text
<tool>/
├── README.md, README_zh.md, LICENSE, CHANGELOG.md, CONTRIBUTING.md, SECURITY.md
├── AGENTS.md                 entry hook: read .agent/AGENT.md first
├── .agent/                   five specs (+ _zh) and SPEC_VERSION, vendored
├── contract/                 contract.json and schemas/ (vendored), contract-ext.json (tool-owned)
├── .github/                  workflows, issue and PR templates
├── skills/<tool>/            SKILL.md, test-prompts.json, optional references/
├── scripts/                  sync and drift guard (vendored), version tooling, npm launcher
├── docs/                     COMPATIBILITY.md, E2E.md, OPEN_SOURCE_CHECKLIST.md
├── package.json              npm wrapper
├── Go:     go.mod, cmd/, internal/ ─ internal/aicli/ (vendored library + MANIFEST.json)
└── Python: pyproject.toml, <pkg>/, tests/ ─ <pkg>/_aicli/ (vendored library + MANIFEST.json)
```

- 源码、测试、脚本和文档各有自己的目录；测试目录镜像源码的布局。
- 库只存在于它的内置副本路径。工具不得保留第二份副本（例如复制叠加层时留下的顶层 `aicli/`），也不得从其他任何位置导入该库。
- 运行时状态（确认密钥、操作记录、授权范围文件、通知缓存）放在状态目录中（SEC-030），绝不放在仓库里。

原因：每个仓库的位置都相同，并且只有一份库副本供漂移检查核对。
检查：评审；`check-spec.js` 只检查内置副本路径，所以第二份副本要靠评审发现。

### REPO-051 模板模型

规范仓库的 `template/` 包含语言无关的 `common/`，以及每种语言各一个叠加层（`go/`、`python/`）。工具复制 `common/` 和恰好一个叠加层，把叠加层的 `.gitignore` 追加到通用的那份后面，再用 `scripts/sync-spec.js` 以副本方式引入库（`template/INSTANTIATE.md`）。叠加层中的 `aicli/` 和 `example/` 目录是规范仓库内部的源码，不是工具的文件。行为规范从不按语言分叉；叠加层只包含构建、CI 和打包文件。增加一种语言，就是增加一个叠加层，外加一个能通过同一套符合性测试套件的库移植版本。

原因：Agent 看到的契约不能取决于工具是用什么语言编写的。
检查：固定在同一版本的 Go 工具和 Python 工具中，`.agent/*` 逐字节相同。

## 7. 规范与库的单一来源

### REPO-060 固定版本

`.agent/SPEC_VERSION` 必须记录工具所遵循的规范 tag，例如 `v2.0.0`。这一处固定同时覆盖规范、契约和 Schema、库以及同步工具；它们从不分开固定。

原因：这些文件在规范仓库中是一起测试的；混用版本会构建出一个谁都没测过的组合。
检查：`check-spec.js` 读取固定的版本，缺少时失败。

### REPO-061 同步

`scripts/sync-spec.js` 从固定 tag 的规范中，逐字节复制 `scripts/spec-files.js` 列出的每个文件：

| 规范仓库中的来源 | 工具中的位置 |
|---|---|
| `template/common/.agent/*.md` | `.agent/` |
| `template/common/contract/contract.json`、`contract/schemas/*.schema.json` | `contract/` |
| `template/go/aicli/*.go`（`*_test.go` 除外），以及 `LICENSE` 和 `MANIFEST.json` | `internal/aicli/`（Go） |
| `template/python/aicli/` 中的 `aicli` 包文件（测试除外），以及 `LICENSE` 和 `MANIFEST.json` | `<pkg>/_aicli/`（Python；`<pkg>` 是检测到的包目录） |
| `template/common/scripts/{spec-files,sync-spec,check-spec}.js` | `scripts/` |

工具不再生成契约代码：`scripts/gen-contract.js` 在规范仓库中运行，把 `contract_gen.go` 和 `contract_gen.py` 写进各自的库，因此库的内置副本自带这些常量。Go 库是一个扁平的包，Python 库只使用相对导入，所以复制时无需改写导入语句。

原因：副本运行的正是规范仓库测试过的代码，中间不经过任何包 registry。
检查：在符合规范的工具上运行 `node scripts/sync-spec.js` 后，`git diff` 为空。

### REPO-062 清单与漂移检查

每个库都附带一个 `MANIFEST.json`（`{spec_version, files: {path: sha256}}`），由规范仓库中的 `scripts/build-manifest.js` 写出。哈希基于把 CRLF 规范化为 LF 之后的内容计算，因此在启用 `core.autocrlf` 的 Windows 上检出时，算出的哈希与 Linux 上相同。`scripts/check-spec.js` 必须在 `ci.yml` 和 `release.yml` 中运行，并且失败即拒绝：

- 离线且严格：每个库文件都与清单中的哈希一致，清单列出的文件一个不缺，库目录中没有清单之外的文件，清单的 `spec_version` 与固定的版本和 `contract.json` 一致，每个内置副本规范文件都在；
- 在线：每个内置副本文件都与固定 tag 的规范相同，且没有残留的过期文件。下载失败时这一层降级为警告，因为离线层依然有效；使用 `--from <checkout>` 时，比对是严格的。

原因：写入门控（write gate）的副本一旦被编辑，就又变回了每个工具各自的实现；而工具群审计发现，在自带门控的 10 个工具中，每一个的门控都有缺陷。
检查：两个工作流中的 `check-spec.js` 步骤。

### REPO-063 内置副本文件只读

工具不得在其内置副本路径下编辑、添加或删除文件，这些路径是：`.agent/`、`contract/`（`contract-ext.json` 除外）、`internal/aicli/` 或 `<pkg>/_aicli/`，以及内置副本脚本。每个库文件开头都有一段文件头说明这一点。要修改其中某个文件，就在规范仓库里修改，发布一个 tag，在工具中提升 `.agent/SPEC_VERSION` 并运行 `sync-spec.js`；这样产生的 diff 是机械的，`check-spec.js` 会证明这一点。需要修改库的工具，到规范仓库提 issue 或 pull request。

原因：一份实现、只做一次符合性测试，这正是 2.0 的意义所在（CLI-050）。
检查：`check-spec.js`；评审内置副本路径下任何不是同步产生的 diff。

### REPO-064 工具自有的扩展

工具专属的错误码放在 `contract/contract-ext.json` 中，这个文件归工具所有，同步从不触碰它；这些错误码还要向库声明（Go：`App.ExtCodes`）。每个错误码都遵循 CLI-018：使用不属于核心错误码的 `E_*` 名称，声明 `exit` 和 `retryable`，退出码 9 只用于需要人工处理的错误码。

原因：工具需要核心中没有的错误码（如 archery-cli 的 `E_2FA_REQUIRED`），又不能为此分叉核心错误码表。
检查：`contract.json` 中 `error_codes.extension` 的扩展规则。

## 8. 质量门禁

### REPO-070 合并门禁

CI 必须在每次推送和每个 pull request 上运行，任何一步失败都会阻止合并：

- 格式化与 lint：Go 用 `gofmt`、`go vet`、golangci-lint；Python 用 ruff；
- 测试，包括为每条写命令提供样例的符合性测试套件（VER-001）以及对测试产生的每个信封的校验（VER-002）；按 VER-003 记录注册表覆盖和缺口，不以全部命令覆盖作为产品首发支持的前提。Go 的 Makefile 目标是 `test`（含符合性测试）、`conformance` 和 `lint`；
- 对工具发布所在的每个生态做依赖审计（SEC-050）：Go 用 `govulncheck ./...` 和 `npm audit`；Python 用 `pip-audit` 和 `npm audit`；
- `node scripts/check-version.js` 和 `node scripts/check-spec.js`。

原因：工具群的 11 个 Go 仓库中有 9 个只审计 npm 壳。4 个可达的 Go 标准库 CVE 一直无人察觉，直到运行 `govulncheck` 的那 2 个仓库变红（规范 1.6.0）。
检查：`ci.yml` 中的这些步骤。

### REPO-071 发布门禁等同于合并门禁

`release.yml` 必须在签名或发布任何东西之前，运行与 REPO-070 相同的测试、审计和漂移检查（VER-008）。

原因：否则，如果安全公告在最后一次合并变绿之后才公布，有漏洞的依赖就会进入一个没人检查过的签名发布版。2026 年 8 月，4 个 Go 标准库 CVE 在工具群最后一次变绿的运行之后几天公布，那时推送的 tag 就会把这些漏洞带进发布版（规范 1.6.2、1.6.3）。
检查：`release.yml` 在签名和发布之前运行 REPO-070 的各个步骤。

### REPO-072 发布前先有证据

在会改变命令、输出或 Skill 的发布之前，工具应当运行 Agent 评测（VER-006）；在任何命令声明为 `stable` 之前，则必须运行。每条命令的成熟度和证据引用都记录在注册表中（VER-005），`docs/E2E.md` 记录 `verified` 所引用的真实环境运行。

原因：评测发现了任何评审都没有预料到的影响；2026 年 10 月那个没有命令地图的 gitlab-cli Skill，让不常用的命令代价更高。
检查：每个证据引用都能解析；`stable` 命令引用了一次评测运行。

### REPO-073 测试从不触碰用户的状态

测试必须把状态目录指向临时目录（`App.StateDir` 或 `<PREFIX>_STATE_DIR`），这样任何测试，包括编译出的测试二进制（Windows 上的 `*.test.exe`），都不会读写用户的确认密钥、操作记录、授权范围文件或通知缓存。

原因：在真实状态上运行的测试可能消耗掉一项真实的操作，或者只因为某个真实的授权范围文件授予了它才通过。
检查：把 `HOME`（或 `USERPROFILE`）设为只读目录时，测试套件依然通过。

### REPO-074 格式化工具与 pull request 自检

每种语言只用一个格式化工具，配置提交入库（Go 用 `gofmt`，Python 用 ruff，用到 JavaScript 时用 Prettier）；不靠手工对齐任何东西。PR 模板要求确认：测试和符合性测试套件通过，每条写命令都有样例，已记录覆盖缺口及任何 R06 失败；每条改动过的命令，其成熟度与证据相符；新增的写入经过写入门控，且声明的效果属实；授权范围和授权行为没有变化，否则已更新 `SECURITY.md`；没有编辑任何内置副本；已更新 `CHANGELOG.md`；已重新生成 README 和 Skill 的命令地图；已更新译文。

原因：格式噪音会掩盖真正的差异；自检能抓住评审者容易忘记的更新。
检查：CI 中的格式检查；PR 模板。

### REPO-075 规范仓库自己的门禁

本仓库的 CI 必须运行：Go 库和示例的测试（在 `template/go/aicli` 和 `template/go/example` 中运行 `go test ./...`）；Python 库和示例的测试（pytest）；`check-contract.py`（Schema 有效、有效示例通过、无效示例失败、回执规则成立）；清单是否最新（`build-manifest.js` 的输出与已提交的 `MANIFEST.json` 文件一致）；生成的契约是否最新（`gen-contract.js` 的输出与 `contract_gen.go` 和 `contract_gen.py` 一致）；以及参考工具：对 `tools/` 下的每个 `.py` 运行 `python -m py_compile`，在 `tools/agent-eval/harness/recorder` 中运行 `go vet ./...` 和 `go test ./...`。每次规范发布还要在整个工具群上重新运行确认门控审计（VER-007）。

原因：工具信任 tag；这个 tag 就得通过工具所依赖的一切。
检查：本仓库的 `.github/workflows/`。

## 9. 新项目检查清单

- [ ] REPO-001 列出的文件齐全；没有任何已退役的文件
- [ ] README 遵循 REPO-010 和 REPO-011，有 Authorization、Configuration 和 Uninstall 三节，没有引导步骤
- [ ] 已固定 `.agent/SPEC_VERSION`；已运行 `sync-spec.js`；`check-spec.js` 通过
- [ ] 库只在 `internal/aicli/` 或 `<pkg>/_aicli/`，未经编辑
- [ ] 符合性测试为每条写命令提供样例；已开启覆盖率枚举（VER-003）
- [ ] 如有工具专属错误码，都放在 `contract/contract-ext.json` 中
- [ ] 单一版本来源；Skill 的 `metadata.version` 和 `metadata.min_version` 由它派生；`check-version.js` 通过
- [ ] `CHANGELOG.md` 含 `Unreleased`，并已嵌入供 `changelog` 使用
- [ ] CI：格式化、lint、含符合性测试的测试、按发布生态做的依赖审计、版本和规范检查；发布路径上也同样执行
- [ ] 已测量启动器预算；启动器超出预算时，已写明原生安装路径
- [ ] 发布时签名 `checksums.txt`；npm 带来源证明发布；`App.Update` 写明了仓库和包名
- [ ] 每个渠道都随附 Skill，发布归档中 Skill 放在二进制旁边；`test-prompts.json` 存在
- [ ] 测试使用临时状态目录
- [ ] `SECURITY.md` 写明风险档；适用时提供 `NOTICE.md`、`docs/COMPATIBILITY.md`、`docs/E2E.md`
- [ ] 双语文档互相链接
- [ ] 首次公开推送前已逐项走完 `docs/OPEN_SOURCE_CHECKLIST.md`
