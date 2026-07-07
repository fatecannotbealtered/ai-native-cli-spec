# 从模板实例化一个仓库

**English → [INSTANTIATE.md](INSTANTIATE.md)**

本模板拆成两层：语言无关的 **`common/`** 层，加上一层很薄的按语言叠加层
（**`go/`**、**`python/`** …）。你把 `common/` 拷一次，**只**叠加一种语言，填好占位符，
就得到一个开箱即合规的仓库。

```
template/
├── INSTANTIATE.md          # 本指南
├── common/                 # 语言无关——拷进每一个仓库
│   ├── AGENTS.md (+_zh)
│   ├── .agent/{AGENT,CLI-SPEC,SKILL-SPEC,SEC-SPEC}.md (+_zh)
│   ├── README.md (+_zh) / LICENSE / CHANGELOG.md
│   ├── CONTRIBUTING.md (+_zh) / SECURITY.md (+_zh)
│   ├── CODE_OF_CONDUCT.md (+_zh) / NOTICE.md (+_zh)
│   ├── .github/ (PR 模板、ISSUE_TEMPLATE/、dependabot.yml)
│   ├── package.json / scripts/{run,prepare-npm-platform-packages}.js
│   ├── docs/OPEN_SOURCE_CHECKLIST.md (+_zh)
│   ├── skills/{SKILL.md.tmpl,test-prompts.json.tmpl}
│   └── .gitignore / .gitattributes
│                                 # 通用仓库卫生；按语言追加 .gitignore
├── go/                     # 叠加层——ci.yml、release.yml、dependabot.yml、
│                           #   .goreleaser.yml、Makefile、.golangci.yml、
│                           #   cmd/fcc_guard_test.go、.gitignore
└── python/                 # 叠加层——ci.yml、release.yml、ruff.toml、
│                           #   tests/test_fcc_guard.py、.gitignore
```

## 占位符字典

SHARED PLACEHOLDER DICTIONARY — use these literal tokens, never bake in real values:
- {{TOOL_NAME}}        kebab-case CLI/binary/repo name, e.g. my-cli
- {{DESCRIPTION}}      one-line tool description
- {{MODULE}}           Go module path, e.g. github.com/OWNER/{{TOOL_NAME}}   (go templates only)
- {{CMD_PATH}}         Go main package path, e.g. ./cmd/{{TOOL_NAME}}        (go templates only)
- {{NPM_PKG}}          full npm package name incl. scope, e.g. @SCOPE/{{TOOL_NAME}}
- {{TOOL_PKG}}         Python import 包名（下划线），例如 my_cli            (python templates only)
- {{VERSION}}          semver, e.g. 0.1.0
- {{COPYRIGHT_YEAR}}   e.g. 2024-2026
- {{COPYRIGHT_HOLDER}} e.g. Your Name
- {{CONTACT_EMAIL}}    security disclosure contact
- {{RISK_TIER}}        T0 | T1 | T2  (see SEC-SPEC)
- {{RISK_TIER_DESC}}   short justification of the tier
- {{THIRD_PARTY}}      wrapped third-party product name, e.g. GitLab (NOTICE only)
- {{ENV_PREFIX}}       UPPER_SNAKE env var prefix for runtime/update env vars, e.g. MY_CLI
- {{REPO_SLUG}}        GitHub owner/repo, e.g. OWNER/{{TOOL_NAME}}
- {{PRIMARY_TRIGGER}}  触发 Skill 的主要用户意图
- {{SECONDARY_TRIGGER}} 触发 Skill 的次要用户意图
- {{OUT_OF_SCOPE_CASE}} 不该使用本 Skill 的具体场景
- {{DOMAIN_1}} / {{DOMAIN_2}} 复杂工具的 reference 文档 slug，例如 issue、ci
- {{DOMAIN_1_INTENT}} / {{DOMAIN_2_INTENT}} 映射到这些 reference 文档的用户意图
Keep placeholders consistent across files. Write files with the Write tool to the EXACT paths given.

## 步骤

1. **拷通用层。** 把 `template/common/.` 的全部内容拷进你的（新建或已有）仓库根目录——
   含点文件（`.agent/`、`.github/`、`.gitignore`）。

2. **只叠加一种语言。** 把 `template/<lang>/.`（如 `template/go/.`）的全部内容覆盖拷到
   同一个仓库根目录。只选一种——绝不把 `go/` 和 `python/` 混进同一个仓库。

3. **拼接 .gitignore。** 语言叠加层自带一份 `.gitignore`。把它**追加**到通用的那份后面，
   而不是覆盖：

   ```bash
   cat template/<lang>/.gitignore >> .gitignore
   ```

   （`common/.gitignore` 已在第 1 步进了仓库根目录；`<lang>` 那份是你要追加的部分。）

4. **填好每一个占位符。** 把所有拷入文件里的每一个 `{{...}}` token 换成上面字典里的真实值。
   `{{MODULE}}` 和 `{{CMD_PATH}}` 只出现在 `go/` 文件里——Python 仓库不会有它们。
   检查没有残留的 `{{`：

   ```bash
   # 只匹配大写占位符 token——直接 grep '{{' 会误中 workflow 里合法的
   # ${{ ... }} 和 .goreleaser.yml 里的 {{ .Var }}。
   grep -rnE '\{\{[A-Z_0-9]+\}\}' . || echo "占位符已全部填好"
   ```

5. **不包装第三方就删掉 NOTICE.md。** `NOTICE.md`（+ `_zh`）只在工具包装第三方产品时必需
   （即 `{{THIRD_PARTY}}` 场景——Outlook/Exchange、GitLab、Jira …）。不包装就删掉
   `NOTICE.md` 和 `NOTICE_zh.md`。

6. **重命名 Skill 模板。** `skills/SKILL.md.tmpl` 和
   `skills/test-prompts.json.tmpl` 都是桩文件。把它们移进按工具命名的目录，并去掉
   `.tmpl` 后缀：

   ```bash
   mkdir -p skills/{{TOOL_NAME}}
   mv skills/SKILL.md.tmpl skills/{{TOOL_NAME}}/SKILL.md
   mv skills/test-prompts.json.tmpl skills/{{TOOL_NAME}}/test-prompts.json
   ```

   发布前要把两个文件都改实。`SKILL.md` 必须保留 AI-native 章节：
   `Do not use`、`First Step`、`Write Recipe` 或只读边界、`STOP CHECKPOINT`、
   错误决策树、安全边界、自更新和评估场景。`test-prompts.json` 是 Skill 审查用的
   回归集，prompt 要写成本 CLI 的真实场景。

7. **创建构建清单和 lockfile。** CI workflow 假定语言构建清单已存在——模板不带它，
   因为它是项目相关的：

   - Go：`go mod init {{MODULE}}`（CI 从 `go.mod` 读取 Go 版本）。
   - Python：提供 `[dev]` extra（pytest、ruff、pip-audit、pyinstaller）的
     `pyproject.toml`/`setup.py`、供 `pip-audit -r` 用的 `requirements.txt`、
     以及 `release.yml` 调用的 PyInstaller 入口 `build.py`。
   - npm 壳：生成并提交 CI 里 `npm ci` / `npm audit` 需要的 lockfile：
     `npm install --package-lock-only --ignore-scripts`。

8. **跑一遍开源清单。** 首次公开 push 前，过一遍 `docs/OPEN_SOURCE_CHECKLIST.md`。

## 轻量契约

这套拆分的存在，是为了让**加一门语言的成本很低，且行为规范永不分叉**。

- **加一门语言 = 加一个 `template/<lang>/`**，约 6 个文件（CI、release、格式化/lint 配置、
  构建清单、语言级 `.gitignore`）。`common/` 里什么都不用动。
- **每个仓库都 100% 复用 `common/`**——同一份 `AGENTS.md`、同样的 `.agent/{AGENT,CLI-SPEC,SKILL-SPEC,SEC-SPEC}` 本地规范、
  同样的治理文档、同一个 npm 壳。
- **`.agent/` 的行为规范（`AGENT`、`CLI-SPEC`、`SKILL-SPEC`、`SEC-SPEC`）是语言无关的，
  绝不允许按语言分叉。** Go 工具暴露的机器契约，和 Python 工具暴露的完全一致；
  不同的只有构建/CI 管线，而这管线是语言叠加层**唯一**可以携带的东西。

如果你发现自己想要一条 Go 专属或 Python 专属的行为规则，这恰恰说明这条规则应当以语言中立的
表述写进 `common/.agent/`，而不是放进叠加层。
