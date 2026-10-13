# 用本模板实例化一个工具

**English → [INSTANTIATE.md](INSTANTIATE.md)**

一个新工具由三部分组成：语言无关的 `common/` 层、一个语言叠加层，以及共享的 `aicli` 库，后者由 `scripts/sync-spec.js` 按固定的规范标签以副本方式引入。库提供信封、错误、注册表、`--help`、`reference`、写入门控（write gate）、授权范围文件、操作记录、内置命令和符合性测试套件；命令、Skill 和测试由你来写。

```text
template/
├── INSTANTIATE.md               # this guide
├── common/                      # copied into every tool
│   ├── AGENTS.md, README.md, SECURITY.md, CONTRIBUTING.md, ... (+ _zh)
│   ├── .agent/{AGENT,CLI-SPEC,SEC-SPEC,SKILL-SPEC,VERIFY-SPEC}.md (+ _zh)   # vendored
│   ├── contract/contract.json, contract/schemas/*.json                     # vendored
│   ├── scripts/{spec-files,sync-spec,check-spec}.js                        # vendored
│   ├── scripts/{run,prepare-npm-platform-packages,version-files,sync-version,check-version}.js
│   ├── skills/{SKILL.md.tmpl,test-prompts.json.tmpl}
│   └── package.json, .npmrc, .github/, docs/, .gitignore, .gitattributes
│   (spec repository only: scripts/{gen-contract,build-manifest}.js, scripts/check-contract.py,
│    contract/examples/ -- removed in step 1)
├── go/       # overlay: .github/workflows/{ci,release}.yml, .github/dependabot.yml,
│             #   .goreleaser.yml, .golangci.yml, Makefile, .gitignore
│             # plus aicli/ (the library, vendored to internal/aicli/) and example/
└── python/   # overlay: .github/workflows/{ci,release}.yml, ruff.toml, .gitignore
              # plus aicli/ (the library, vendored to <pkg>/_aicli/) and example/
```

## 占位符字典

使用下面这些字面占位符；绝不要把真实值写死进模板。

| 占位符 | 含义 | 出现位置 |
|---|---|---|
| `{{TOOL_NAME}}` | kebab-case 的二进制名、仓库名和 Skill 名，例如 `notes-cli` | 所有地方 |
| `{{ENV_PREFIX}}` | UPPER_SNAKE 形式的环境变量前缀，例如 `NOTES_CLI`；也用作 `App.EnvPrefix` | 文档、Skill、测试提示词 |
| `{{DESCRIPTION}}` | 用第三人称一句话说明工具做什么 | package.json、README、Skill |
| `{{MODULE}}` / `{{CMD_PATH}}` | Go 模块路径 / main 包，例如 `github.com/OWNER/notes-cli`、`./cmd/notes-cli` | Go 叠加层 |
| `{{TOOL_PKG}}` | Python 导入包名，例如 `notes_cli` | Python 叠加层 |
| `{{NPM_PKG}}` / `{{REPO_SLUG}}` | 含 scope 的 npm 包名 / GitHub 的 `OWNER/repo` | package.json、文档、`UpdateConfig`（第 5 步） |
| `{{SKILLS_CLI_VERSION}}` | Agent Install 代码块固定使用的 `skills` 安装器版本，也就是你运行这个代码块时用的版本（`npm view skills version` 可以查到最新版本）；绝不能写 `latest` | README |
| `{{VERSION}}` | 首个发布版本，例如 `0.1.0`；之后从 package.json 派生 | package.json、CHANGELOG、Skill |
| `{{COPYRIGHT_YEAR}}` / `{{COPYRIGHT_HOLDER}}` / `{{CONTACT_EMAIL}}` | 许可证信息和披露联系方式 | LICENSE、文档 |
| `{{RISK_TIER}}` / `{{RISK_TIER_DESC}}` | `T0`、`T1` 或 `T2`（SEC-001），以及定这一档的理由 | README、SECURITY.md |
| `{{THIRD_PARTY}}` | 工具所驱动的产品或服务，例如 GitLab | NOTICE、Skill |
| `{{PRIMARY_TRIGGER}}` / `{{SECONDARY_TRIGGER}}` / `{{OUT_OF_SCOPE_CASE}}` | 用户用自己的话提出的需求；工具不适用的情况 | Skill |
| `{{REF_EXAMPLE}}` / `{{FILTER_EXAMPLE}}` / `{{DEFAULT_FIELDS}}` / `{{UNTRUSTED_EXAMPLES}}` | 一个 `ref`（`ops/n-42`）；一条带过滤条件的列表命令；列表项的默认字段；哪些字段装的是别人写的文本 | Skill |
| `{{PLAYBOOK_1}}` / `{{PLAYBOOK_2}}` | 常见的多步骤任务，每项一行，写真实的命令 | Skill |
| `{{COMMAND_MAP}}` | 由命令地图测试用 `app.CommandMap()` 替换（第 8 步）；绝不手工填写 | README（Capabilities）、Skill |
| `{{PROMPT_*}}` / `{{INJECTED_ACTION}}` | 每个 VER-006 场景一条具体的用户请求；注入文本要求执行的操作 | test-prompts.json |

## 步骤

下面的命令以一个名为 `notes-cli` 的 Go 工具为例；Python 的差异见第 9 步。所有命令都在新仓库的根目录下运行。

### 1. 复制 common 和一个叠加层

```bash
SPEC=../ai-native-cli-spec            # a checkout of the spec at the tag you will pin
cp -R "$SPEC/template/common/." .
cp -R "$SPEC/template/go/.github" "$SPEC/template/go/.golangci.yml" \
      "$SPEC/template/go/.goreleaser.yml" "$SPEC/template/go/Makefile" .
cat "$SPEC/template/go/.gitignore" >> .gitignore
# Python instead: cp -R "$SPEC/template/python/.github" "$SPEC/template/python/ruff.toml" .
#                 cat "$SPEC/template/python/.gitignore" >> .gitignore
rm -f scripts/gen-contract.js scripts/build-manifest.js scripts/check-contract.py
rm -rf contract/examples
```

只复制上面列出的叠加层文件：叠加层的 `aicli/` 和 `example/` 目录不复制（库由 sync-spec 以副本方式引入；example 只作参考）。如果工具不封装任何第三方产品，删除 `NOTICE.md` 和 `NOTICE_zh.md`。

### 2. 填写占位符

在复制过来的文件中替换字典里的占位符，先从名字和环境变量前缀开始；Skill 相关的占位符留到第 8 步（GNU sed；macOS 上用 `gsed`）。

```bash
grep -rlE '\{\{(TOOL_NAME|ENV_PREFIX|MODULE|CMD_PATH|NPM_PKG|REPO_SLUG)\}\}' . --exclude-dir=node_modules --exclude-dir=.git |
  xargs sed -i 's#{{TOOL_NAME}}#notes-cli#g; s#{{ENV_PREFIX}}#NOTES_CLI#g;
    s#{{MODULE}}#github.com/OWNER/notes-cli#g; s#{{CMD_PATH}}#./cmd/notes-cli#g;
    s#{{NPM_PKG}}#@OWNER/notes-cli#g; s#{{REPO_SLUG}}#OWNER/notes-cli#g'
```

### 3. 创建构建清单

- Go：`go mod init github.com/OWNER/notes-cli`。CI 从 `go.mod` 读取 Go 版本。
- Python：在 `pyproject.toml` 里写 `[project] name`（sync-spec 靠它找到包）和 `dynamic = ["version"]`，并配上 `[tool.setuptools.dynamic] version = {attr = "notes_cli.__version__"}`：版本号放在 `notes_cli/__init__.py` 里，由 `npm version` 改写；写成静态的 `version` 会悄悄漂移，`check-version.js` 也发现不了。再加上 `[dev]` extra（pytest、ruff、pip-audit、pyinstaller）、依赖里的 `click` 和 `sigstore`（aicli 的 `update` 用 `sigstore` 校验发布）、给 `pip-audit` 用的 `requirements.txt`、构建发布二进制用的 `build.py`，以及带 `__init__.py` 的包目录。
- npm 壳：对着公共 registry 生成一次 lockfile，提交之前检查一遍，免得私有镜像地址进了公开文件（REPO-031）。之后的版本升级会逐字段改写它（`npm version`）。

  ```bash
  npm install --package-lock-only --ignore-scripts --registry=https://registry.npmjs.org/
  grep '"resolved"' package-lock.json | grep -v '"https://registry.npmjs.org/' && echo "fix the URLs above" || echo "lockfile OK"
  ```

### 4. 固定规范版本并以副本方式引入库

```bash
echo v2.0.0 > .agent/SPEC_VERSION
node scripts/sync-spec.js        # or: --from "$SPEC" to copy from the local checkout
node scripts/check-spec.js
# Go: run the "go get ... && go mod tidy" line sync-spec printed (every module the library requires, indirect ones included, at the library's versions)
```

`sync-spec.js` 写入 `.agent/`、`contract/`、同步工具和库（`internal/aicli/`，Python 为 `<pkg>/_aicli/`），并对照规范的 `MANIFEST.json` 检查每个库文件。绝不要编辑内置副本文件：它们与清单或固定的标签不一致时，`check-spec.js` 会让 CI 失败。以后要升级，就修改固定的版本，再运行一次 `sync-spec.js`；同步工具本身有变化时，它会重新运行自己。

### 5. 把 main 接到 aicli.App 上

```go
// changelog.go (repository root): the runtime changelog command reads this.
package notescli

import _ "embed"

//go:embed CHANGELOG.md
var ChangelogMarkdown string
```

```go
// cmd/app.go
package cmd

import (
	"os"

	"github.com/spf13/cobra"

	notescli "github.com/OWNER/notes-cli"
	"github.com/OWNER/notes-cli/internal/aicli"
)

var version = "dev" // set by -ldflags "-X <module>/cmd.version=..." (Makefile, .goreleaser.yml)

// NewApp builds a fresh App and command tree; an App runs once. main passes the
// real client, tests a fake.
func NewApp(store Store) *aicli.App {
	app := &aicli.App{
		Name: "notes-cli", Version: version, RiskTier: "T2", EnvPrefix: "NOTES_CLI",
		Root:      &cobra.Command{Use: "notes-cli", Short: "Acme Notes for AI agents"},
		Changelog: notescli.ChangelogMarkdown,
		Env:       os.Getenv, // read the environment only through app.Env, so tests and the suite can replace it
		// the built-in update (CLI-070): the repository and the npm package from step 2
		Update: &aicli.UpdateConfig{Repo: "OWNER/notes-cli", NPMPackage: "@OWNER/notes-cli"},
		// settings and profiles (CLI-100): NOTES_CLI_HOST, or "host" in a profile of settings.json
		Settings: []aicli.Setting{{Name: "host", Description: "Acme Notes URL"}},
	}
	addNoteCommands(app, store)
	return app
}
```

```go
// cmd/notes-cli/main.go
package main

import (
	"os"

	"github.com/OWNER/notes-cli/cmd"
)

func main() { os.Exit(cmd.NewApp(cmd.NewClient()).Execute(os.Args[1:])) }
```

`Execute` 会加上 `reference`、`context`、`doctor`、`changelog` 和 `operation`，设置了 `Update` 时还会加上 `update`；它恰好写出一个信封并返回退出码。`Update` 启用 aicli 的内置命令 `update`：`Repo` 填第 2 步中 `{{REPO_SLUG}}` 的值，`NPMPackage` 填 `{{NPM_PKG}}` 的值；通过其他渠道分发时设置 `GoPackage`、`PyPIPackage` 或 `BrewFormula`，发布工作流不是 `release.yml` 时设置 `Workflow`。绝不要自己编写 `update` 命令（CLI-070、CLI-071）。`Settings` 声明工具的设置，`app.Setting(cmd, name)` 依次从标志、`NOTES_CLI_<SETTING>`、`settings.json` 中当前生效的配置档和默认值解析它们；声明了设置时，aicli 还会加上 `--profile`（CLI-100、CLI-101）。可选的钩子：`Identity`（账号和主机，会绑定进令牌）、`DoctorChecks`、`ContextExtra`、`SkillDirs`（`doctor` 的 `version` 检查到哪里找已安装的 Skill，`update` 也同步那里的 Skill；默认是标准的 Agent Skill 目录）、`Shortcuts`、`ShortFlags`、`FlagAliases`、`FlagSynonyms` 和 `VerbSynonyms`（Agent 会做的那些猜测，CLI-022）、`ExtCodes`（扩展错误码，CLI-018）。

### 6. 注册命令，写第一个读命令

每个叶子命令都用 `app.Register` 注册，带上它的效果和输出 schema；命令组用 `app.Group`。上游失败用 `aicli.FromHTTP` 映射。列表的输出 schema 用 `page` 结构，单个对象的用 `object` 结构。`Maturity` 绝不超过 `Evidence`：`experimental` 引用针对模拟上游运行该命令的测试（`simulated:`），`verified` 再加上一次录制下来的真实运行（`live:`，VER-005）。

```go
// cmd/notes.go
package cmd

import (
	"github.com/spf13/cobra"

	"github.com/OWNER/notes-cli/internal/aicli"
)

func addNoteCommands(app *aicli.App, store Store) {
	note := &cobra.Command{Use: "note"}
	app.Root.AddCommand(note)
	app.Group(note, "Read and change notes")
	fields := []string{"ref", "title", "tags", "body", "updated_at"}
	app.Schema("note", aicli.SchemaDef{Shape: "object", Fields: fields, Untrusted: []string{"title", "body"}})
	app.Schema("note_page", aicli.SchemaDef{Shape: "page", Fields: fields,
		DefaultFields: []string{"ref", "title", "tags", "updated_at"},
		Untrusted:     []string{"title", "body"}})

	list := &cobra.Command{Use: "list", Short: "List notes", RunE: func(c *cobra.Command, _ []string) error {
		tag, _ := c.Flags().GetString("tag")
		items, next, err := store.List(c.Context(), tag)
		if err != nil {
			return err
		}
		return app.List(c, aicli.Page{Items: items, HasMore: next != "", NextCursor: next,
			Scope: map[string]any{"tag": tag}, Sort: "updated_at_desc"})
	}}
	list.Flags().String("tag", "", "only notes with this tag")
	note.AddCommand(list)
	app.Register(list, aicli.Meta{Summary: "List notes", Effects: []aicli.Effect{aicli.Observe},
		Output: "note_page", Maturity: aicli.Experimental, Evidence: []string{"simulated:cmd/notes_test.go"},
		Examples: []string{"notes-cli note list --tag urgent"}})
	// step 7 adds note update here
}
```

### 7. 写第一个写命令

写命令声明一个非 `observe` 的效果，并经过 `app.Write`：预演（dry-run）、拒绝响应、令牌、授权范围检查、操作记录和回执都由库处理。你提供只读的 `Preview`、`Apply`，以及目标的规范化键：把 Agent 输入的内容（ref、网页 URL、名字）解析成每个对象唯一的一种写法，再用 `aicli.SetResolved` 绑定这种形式。这样一来，`note update <url>` 和 `note update <ref>` 就是同一个请求（CLI-054），也是同一个授权范围目标（SEC-012）。`cmd/notes.go` 现在还要导入 `context`。

```go
	update := &cobra.Command{Use: "update <ref>", Short: "Change a note's title", RunE: func(c *cobra.Command, _ []string) error {
		raw, _ := c.Flags().GetString("ref")
		ref, err := store.Resolve(c.Context(), raw) // ref, web URL or name -> the canonical ref (CLI-048)
		if err != nil {
			return err
		}
		if err := aicli.SetResolved(c, "ref", ref); err != nil { // bind the canonical form
			return err
		}
		title, _ := c.Flags().GetString("title")
		return app.Write(c, aicli.WriteSpec{
			Target: aicli.Target{Key: ref, Kind: "note", Label: ref},
			Preview: func(ctx context.Context) (aicli.Preview, error) { // runs at dry-run, refusal and confirm
				n, err := store.Get(ctx, ref)
				if err != nil {
					return aicli.Preview{}, err
				}
				return aicli.Preview{Action: "Change the note's title", CanApply: true,
					Changes:     []aicli.FieldChange{{Field: "title", Before: n.Title, After: title}},
					BaseVersion: n.UpdatedAt, BaseSource: "updated_at"}, nil
			},
			Apply: func(ctx context.Context, op aicli.Op) (aicli.Result, error) {
				n, err := store.SetTitle(ctx, ref, title)
				if err != nil {
					return aicli.Result{}, err
				}
				return aicli.Result{Observed: map[string]any{"title": n.Title}, Data: n,
					Untrusted: []string{"title", "body"}}, nil // external text in Data (CLI-046)
			},
		})
	}}
	update.Flags().String("ref", "", "the note")
	update.Flags().String("title", "", "the new title")
	note.AddCommand(update)
	app.Register(update, aicli.Meta{Summary: "Change a note's title", Effects: []aicli.Effect{aicli.Change},
		Positional: "ref", Output: "note", Maturity: aicli.Experimental, Evidence: []string{"simulated:cmd/notes_test.go"},
		Examples: []string{
			"notes-cli note update ops/n-42 --title Done --dry-run",
			"notes-cli note update ops/n-42 --title Done --confirm <confirm_token>"}})
```

`Apply` 返回错误就意味着结果未知（`E_OUTCOME_UNKNOWN`），除非你把它包装起来：什么都没发出去时用 `aicli.NotDispatched(err)`，上游拒绝且什么都没改时用 `aicli.Rejected(err)`。危险命令设置 `Dangerous: true`（删除、发送、共享；`deliver` 和 `configure` 效果默认就是危险的），它的示例要带 `--dangerous`。绝不要自己实现令牌、密钥或操作记录（CLI-050）。

### 8. 添加符合性测试和 Skill

```go
// cmd/conformance_test.go
// conformanceHarness connects the suite to the tool and a fake store (an
// in-memory upstream that counts write requests); TestMain reuses it for R06.
func conformanceHarness(store *fakeStore) aicli.Harness {
	return aicli.Harness{
		NewApp: func(map[string]string) *aicli.App { return NewApp(store) }, // the suite sets the env
		Writes: store.Writes,
		Reset:  store.Reset,
		// one valid write per write command: arguments after the command path, no gate flags
		Samples: map[string][]string{"note update": {"ops/n-1", "--title", "Renamed"}},
	}
}

func TestConformance(t *testing.T) { aicli.RunConformance(t, conformanceHarness(newFakeStore())) }
```

```go
// cmd/main_test.go: every App the tests run records the command it ran into
// NOTES_CLI_COVERAGE_FILE (VER-003); after a full run, R06 checks that every
// registry command is in it, whatever order the tests ran in.
func TestMain(m *testing.M) {
	f, err := os.CreateTemp("", "notes-cli-coverage-*.jsonl")
	if err != nil {
		panic(err)
	}
	f.Close()
	os.Setenv("NOTES_CLI_COVERAGE_FILE", f.Name())
	code := m.Run()
	if code == 0 && fullRun() {
		tb := &mainTB{}
		h := conformanceHarness(newFakeStore())
		h.CoverageFile = f.Name()
		aicli.CheckCoverage(tb, h)
		if tb.failed {
			code = 1
		}
	}
	os.Remove(f.Name())
	os.Exit(code)
}

// fullRun is false when -run or -skip selects a subset of the tests, as `make conformance` does.
func fullRun() bool {
	for _, name := range []string{"test.run", "test.skip"} {
		if f := flag.Lookup(name); f != nil && f.Value.String() != "" {
			return false
		}
	}
	return true
}

// mainTB reports R06 failures from TestMain.
type mainTB struct{ failed bool }

func (m *mainTB) Helper() {}
func (m *mainTB) Errorf(format string, args ...any) {
	m.failed = true
	fmt.Fprintf(os.Stderr, "FAIL: "+format+"\n", args...)
}
func (m *mainTB) Fatalf(format string, args ...any) { m.Errorf(format, args...) }
func (m *mainTB) Logf(string, ...any)               {}
```

套件对每个写命令运行写入门控检查（缺少样例的写命令会让它失败），校验每个信封，并检查 `reference`、命令树形状、用法错误、`context`、`doctor` 和 `operation`（VER-001、VER-002）。只要有命令没被你的任何测试执行过，`TestMain` 里的 `CheckCoverage` 就会失败（R06、VER-003）。它只在完整运行之后执行，所以 `make conformance`（`go test -run Conformance`）和其他带过滤的运行会跳过它，而不是让它失败；`template/go/example/notes_test.go` 的接法相同。`Scope` 为 nil 时，正向检查会被授予所有效果；有 `deliver`、`artifact` 或 `persist` 命令的工具要设置 `Scope`，写明其样例用到的 `destinations` 或 `outputs`（SEC-012）。每个命令的行为矩阵由你自己补上（VER-004），并在这些测试里把 `App.StateDir` 设为 `t.TempDir()`，这样没有测试会碰到用户的密钥、操作记录或授权范围文件（REPO-073）。

然后是 Skill：

```bash
mkdir -p skills/notes-cli
mv skills/SKILL.md.tmpl skills/notes-cli/SKILL.md
mv skills/test-prompts.json.tmpl skills/notes-cli/test-prompts.json
```

根据用户实际会问的问题填写 Skill 的占位符（SKILL-SPEC）。Skill 和 README 的 Capabilities 一节在 `<!-- aicli:command-map:start -->` 和 `<!-- aicli:command-map:end -->` 之间放着同一份命令地图：两个标记之间的文本与 `app.CommandMap()` 的输出完全一致，下面的测试让每一份副本保持最新。不要手工把命令地图粘贴到 `{{COMMAND_MAP}}` 的位置（渲染器的输出末尾已经带换行，粘贴的副本会多出一个空行，测试会拒绝）：用 `UPDATE_SKILL_MAP=1` 运行一次测试，由它写入每一处命令地图。

```go
// cmd/commandmap_test.go
func TestSkillCommandMap(t *testing.T) {
	const start, end = "<!-- aicli:command-map:start -->\n", "<!-- aicli:command-map:end -->"
	want := NewApp(newFakeStore()).CommandMap()
	// README_zh.md carries the same block once it is translated; a missing file is skipped.
	for _, path := range []string{"../README.md", "../README_zh.md", "../skills/notes-cli/SKILL.md"} {
		raw, err := os.ReadFile(path)
		if os.IsNotExist(err) && strings.HasSuffix(path, "_zh.md") {
			continue
		}
		if err != nil {
			t.Fatal(err)
		}
		text := strings.ReplaceAll(string(raw), "\r\n", "\n")
		i, j := strings.Index(text, start), strings.Index(text, end)
		if i < 0 || j < i {
			t.Fatalf("%s: command-map markers missing", path)
		}
		if got := text[i+len(start) : j]; got != want {
			if os.Getenv("UPDATE_SKILL_MAP") == "1" {
				if err := os.WriteFile(path, []byte(text[:i+len(start)]+want+text[j:]), 0o644); err != nil {
					t.Fatal(err)
				}
				continue
			}
			t.Errorf("%s: command map is stale; run UPDATE_SKILL_MAP=1 go test ./cmd -run TestSkillCommandMap", path)
		}
	}
}
```

在 `test-prompts.json` 中为每个场景写一条具体的提示词（VER-006）。每个条目只保留 `id`、`scenario`、`prompt`、`expected` 和可选的 `grader_notes`（SKL-011）；模板把每次试验的准备工作和失败条件放在 `grader_notes` 里。如果工具没有设置 `Update`，删除 `update` 那一条。

### 9. Python 的差异

库放在 `<pkg>/_aicli/`，用相对导入（`from ._aicli import App, Meta, WriteSpec`）。API 与 Go 版相同，只是改用 snake_case（`app.register`、`app.write`、`set_resolved`、`run_conformance`、`check_coverage`、`app.command_map()`），构建在 click 命令树上；`UpdateConfig` 和 `Setting` 的字段相同，只是改用 snake_case（`App(update=UpdateConfig(repo="OWNER/notes-cli", npm_package="@OWNER/notes-cli"))`）。`template/python/example/` 是完整的参考。测试放在 `tests/`：`run_conformance` 在一个测试里运行，`NOTES_CLI_COVERAGE_FILE` 对整个测试会话生效，`check_coverage` 在所有测试跑完后运行一次，且只在完整运行时执行。`ruff.toml` 已经排除了 `_aicli`。

### 10. CI 和最后的检查

工作流除了占位符之外不需要修改。每次 push 都会运行：格式检查、vet 和 lint（lint 不检查内置的库副本）、在三种操作系统上运行带符合性测试套件的 `go test ./...`、`govulncheck`、`npm audit`、`check-version.js` 和 `check-spec.js`；Python 运行 ruff、pytest、pip-audit 和同样的 Node 检查。`release.yml` 在构建和签名之前把每一道关卡重跑一遍（VER-008）。Go 工作流还会报告 npm 启动器的开销（REPO-041）；超过 100 ms 时，在 README 的 Agent Install 一节写明原生二进制的路径。

```bash
grep -rnE '\{\{[A-Z_0-9]+\}\}' . --exclude-dir=node_modules --exclude-dir=.git || echo "all placeholders filled"
node scripts/check-spec.js && node scripts/check-version.js && go vet ./... && go test ./...
```

然后在第一次公开 push 之前，逐项过一遍 `docs/OPEN_SOURCE_CHECKLIST.md`。

## 轻量契约

- `.agent/` 下的规范、契约和 schema 都与语言无关，绝不按语言分叉。
- 共享行为放在库里，以副本方式引入并有漂移检查；工具绝不重新实现信封、注册表或写入门控。
- 新增一门语言，就是新增一个 `template/<lang>/`：CI 和打包叠加层、一个行为相同的 `aicli` 库，以及一个能通过其符合性测试套件的示例。`common/` 里什么都不用改。
