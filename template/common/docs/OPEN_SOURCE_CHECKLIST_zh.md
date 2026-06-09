# 开源检查清单

[English](OPEN_SOURCE_CHECKLIST.md) | [中文](OPEN_SOURCE_CHECKLIST_zh.md)

在 `{{TOOL_NAME}}` **首次公开推送之前**逐项走查。这是一道安全与质量关卡，不是文档 —— 仓库公开前每一项都必须勾选（或明确写明理由后豁免）。一旦公开，历史中泄露的密钥就无法收回。

## 密钥

- [ ] 工作区任何位置都没有凭据、token、API key 或密码。
- [ ] **git 历史**中没有密钥 —— 已用扫描工具检查（如 `gitleaks detect`、`git log -p | grep`）；若发现，需重写历史或重建仓库，而不是只从 `HEAD` 删除。
- [ ] 代码、配置、注释中没有内部主机名、内网 IP、内部 URL 或公司内部标识符。
- [ ] 测试夹具和录制的响应只含合成 / 脱敏数据 —— 没有真实账户数据，没有真实 token。
- [ ] `.env`、`*.local`、凭据文件以及 `~/.{{TOOL_NAME}}/` 产物已列入 `.gitignore` 并确认未被跟踪（`git status --ignored`）。
- [ ] 凭据静态加密存储（操作系统钥匙串或加密信封，`0600`），绝不以明文写入配置文件。

## 文档

- [ ] `README.md` 遵循 REPO-SPEC §2 骨架（标题/徽章 → Agent 安装 → 它做什么 → 能力 → Agent 工作流 → 机器契约 → 配置 → 项目结构 → 开发 → 链接）。
- [ ] `README.md` 与 `README_zh.md` **内容同步** —— 章节一致、命令一致、占位符解析为相同的真实值。
- [ ] `CHANGELOG.md` 存在，使用 Keep a Changelog 格式，顶部有 `## [Unreleased]` 小节。
- [ ] `LICENSE` 存在，许可证经过有意选择（默认 MIT），`{{COPYRIGHT_YEAR}}` / `{{COPYRIGHT_HOLDER}}` 已填写。
- [ ] 安装块可直接复制运行，使用真实已发布的 `{{NPM_PKG}}` / `{{REPO_SLUG}}`。

## 治理

- [ ] `SECURITY.md` 存在，含可用的披露渠道（`{{CONTACT_EMAIL}}`）和受支持版本表。
- [ ] `CONTRIBUTING.md` 存在（环境搭建、分支/提交、测试、PR 流程）。
- [ ] 若项目接受外部贡献，`CODE_OF_CONDUCT.md` 存在（Contributor Covenant）。
- [ ] 若 `{{TOOL_NAME}}` 包装第三方产品（{{THIRD_PARTY}}），`NOTICE.md` 载明商标 / 非隶属声明，且 `docs/COMPATIBILITY.md` 列出已验证的后端版本矩阵。

## 构建 / CI

- [ ] 待推送的提交上 CI（`.github/workflows/ci.yml`）为**绿色**。
- [ ] CI **强制**执行 lint 和测试 —— lint 失败或测试失败会阻断合并（不仅仅是提示性的）。
- [ ] 格式化工具配置已提交（按语言：ruff / golangci-lint / prettier），且 CI 运行格式校验。
- [ ] 没有提交构建产物、缓存、虚拟环境或 IDE 配置（已由 `.gitignore` 覆盖）。

## 分发

- [ ] `package.json` 的 `version` 与待发布的 git tag 一致（`vX.Y.Z` ↔ `X.Y.Z`）；`release.yml` 对此做守卫，不一致即失败。
- [ ] 二进制本身（`bin/`、`*.exe`、`dist/`）**不提交** —— 由 CI 产出并被 gitignore。
- [ ] GitHub Release 发布产物附带 `checksums.txt`；standalone 二进制安装/更新路径校验 checksum，且在不匹配或缺少条目时**失败关闭**。
- [ ] release pipeline 使用 Sigstore/Cosign keyless 签署 `checksums.txt`，发布 bundle，standalone 安装/更新路径把签名验证状态与 checksum 校验分开报告。
- [ ] npm 分发从 CI 构建产物发布主 wrapper 包和每个受支持 OS/CPU 的平台包，并使用 `npm publish --provenance`。
- [ ] 版本号有唯一真相来源；运行时 `changelog` 命令和 GitHub Release 正文均派生自 `CHANGELOG.md`，而非手工复制。

## AI 原生

- [ ] 根目录 `AGENTS.md` 存在并指向 `.agent/AGENT.md`。
- [ ] `.agent/{AGENT,CLI-SPEC,SKILL-SPEC,SEC-SPEC}.md` 规格文件齐全；共享仓库骨架标准引用 `ai-native-cli-spec/REPO-SPEC.md`。
- [ ] `skills/{{TOOL_NAME}}/SKILL.md` 存在；frontmatter 包含 `version`、`license: MIT`、`user-invocable: true`，且 `metadata.requires.min_version` 匹配 CLI 版本。
- [ ] `SKILL.md` 包含 `When to use`、`Do not use`、`First Step`、Agent 默认规则、JSON contract、写操作配方或明确只读边界、`STOP CHECKPOINT`、错误决策树、安全边界、自更新和评估场景。
- [ ] `skills/{{TOOL_NAME}}/test-prompts.json` 存在、JSON 合法，并覆盖 fresh-agent read、写操作安全或只读边界、权限边界、`_untrusted` 处理和自更新。
- [ ] `update --confirm` 同步整个 `skills/{{TOOL_NAME}}/` 目录，或返回等价于 `npx skills add {{REPO_SLUG}} -y -g` 的 `skill_sync_command`。
- [ ] `{{TOOL_NAME}} reference`、`{{TOOL_NAME}} context`、`{{TOOL_NAME}} doctor` 可运行并输出合法的 JSON 信封 —— 代理能从干净的检出自助上手。
- [ ] `SECURITY.md` 中的风险等级与 `.agent/SEC-SPEC.md` 声明的等级一致（`{{RISK_TIER}}`）。
