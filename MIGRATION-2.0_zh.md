# 把 1.6.x 工具迁移到 ai-native-cli-spec 2.0.0

**English → [MIGRATION-2.0.md](MIGRATION-2.0.md)**

本文写给固定在规范 1.6.x 上的工具的维护者。请按顺序完成各步。示例用的是 gitlab-cli（Go、cobra）。Python 工具按同样的步骤做，库从 `<pkg>/_aicli` 导入，名称与 Go 版相同，只是函数和方法改用 snake_case（`app.write`、`WriteSpec`、`run_conformance`、`parse_version`）；typer 应用则把 `typer.main.get_command(app)` 传给库。迁移结果会破坏面向 Agent 的契约（`schema_version` 2.0、键名改名、写入需要授权范围），所以要作为新的 MAJOR 版本发布（REPO-030）。

## 1. 固定到 2.0.0 并同步

你仓库里 1.6 版的 `sync-spec.js` 读的是 1.6 的文件清单，也不知道有这个库，所以先运行一次 2.0 版的副本：

```bash
# from the tool's repo root
git clone --depth 1 --branch v2.0.0 https://github.com/fatecannotbealtered/ai-native-cli-spec ../ai-native-cli-spec-2.0.0
node ../ai-native-cli-spec-2.0.0/template/common/scripts/sync-spec.js --from ../ai-native-cli-spec-2.0.0 --version v2.0.0
node scripts/check-spec.js
```

这会把 `v2.0.0` 写入 `.agent/SPEC_VERSION`，并以副本方式引入五份规范（`VERIFY-SPEC.md` 是新增的）、`contract/contract.json`、`contract/schemas/`、带 `MANIFEST.json` 的库（`internal/aicli/` 或 `<pkg>/_aicli/`）以及同步脚本（REPO-061）。以后的同步用 `node scripts/sync-spec.js`。Go 库需要 cobra、pflag 和 sigstore-go（运行 sync-spec 打印出的 `go get` 那一行，再运行 `go mod tidy`）；Python 库需要 Python ≥ 3.10、click 和 sigstore。绝不要编辑内置副本文件（REPO-063）。`contract/contract-ext.json` 中工具仍可能返回的错误码要保留，按 CLI-018 检查，并在 `App.ExtCodes` 中向库声明它们；2.0 中不可能再出现的错误码要删掉（gitlab-cli 唯一的一个 `E_CANCELLED` 来自交互式提示），一个都不剩时连文件一起删掉。

## 2. 把信封和错误交给 aicli

符合性测试套件为每项检查构建一个全新的 App 和命令树，所以要在函数里构建命令树：包级的命令变量、`init()` 注册和全局 flag 变量（`jsonMode`、`dryRun`、`confirmFlag`、`dangerousMode`）都要去掉。全局 flag 归 App 管。

```go
func NewApp(env func(string) string) *aicli.App {
	app := &aicli.App{Name: "gitlab-cli", Version: version, RiskTier: "T2", EnvPrefix: "GITLAB_CLI",
		Root: newRootCmd(), Changelog: gitlabcli.ChangelogMarkdown, Env: env,
		Identity:     gitlabIdentity,     // redacted account and host of the resolved credentials
		DoctorChecks: gitlabDoctorChecks, // credentials, connectivity
		Shortcuts:    map[string][]string{"branch": {"repo", "branch"}, "member": {"project", "members"}},
		ShortFlags:   map[string]string{"p": "project", "R": "project"},
		FlagAliases:  map[string]string{"repo": "project"},                  // accepted
		FlagSynonyms: map[string][]string{"all": {"limit"}}}                 // suggested
	registerCommands(app) // step 3
	return app
}

func main() { os.Exit(cmd.NewApp(os.Getenv).Execute(os.Args[1:])) }
```

| 1.6.x (gitlab-cli) | 2.0 |
|---|---|
| `output.PrintJSON(v)` | `return app.OK(cmd, v)` |
| `printListJSON(cmd, rows, pag, limit, all)` | `return app.List(cmd, aicli.Page{Items: rows, Count: n, HasMore: more, NextOffset: next, Scope: scope, Sort: "updated_at_desc"})` |
| `failArg("--project is required")` | `return aicli.Usage("missing_parameter", "--project", "--project is required; pass the project path, for example --project acme/web.")` |
| 读命令里的 `handleAPIError(err, jsonMode)` | 一个 `readError(err)` 辅助函数，返回 `aicli.FromHTTP(apiErr.StatusCode, message, apiErr.Error(), 0)`，其中的 `message` 是写给模型的一句话（CLI-017）；状态码 `0`（没有建立连接）得到 `E_NETWORK`（CLI-016） |
| `failWithCode`、`emitError`、退出码常量、`exitCodeForStatus` | `aicli.NewError(code, message, details)` 及各个构造函数；库会补上退出码、`retryable`，以及默认的 `dispatch_state` 和 `next_action`（CLI-010、CLI-011） |
| `--format`、`--json`、`--compact`、`--fields` 的处理；`cmd/agent.go` 中的 Agent 安全开关、手工维护的别名和建议 | App 和上面那些字段（CLI-003、CLI-022）；ref 和 URL 的解析仍留在工具里（CLI-048） |

## 3. 用 Meta 注册每个命令

```go
app.Group(mrCmd, "Merge requests")
app.Register(mrUpdateCmd, aicli.Meta{
	Summary: "Update a merge request's title, description, assignee, labels or target branch",
	Effects: []aicli.Effect{aicli.Change}, Maturity: aicli.Verified, Evidence: []string{"simulated:cmd/mr_test.go", "live:docs/E2E.md#merge-requests"},
	Output: "merge_request", ErrorCodes: []string{"E_USAGE", "E_NOT_FOUND", "E_FORBIDDEN", "E_CONFLICT", "E_OUTCOME_UNKNOWN"},
	Examples: []string{"gitlab-cli mr update 5 --project acme/web --assignee carol --dry-run",
		"gitlab-cli mr update 5 --project acme/web --assignee carol --confirm <confirm_token>"},
	Positional: "iid", FileInputs: map[string]string{"description-file": "description"},
})
app.Schema("merge_request", aicli.SchemaDef{Shape: "object",
	Fields:        []string{"ref", "title", "state", "assignees", "labels", "pipeline_status", "updated_at", "web_url", "description"},
	DefaultFields: []string{"ref", "title", "state", "assignees", "labels", "pipeline_status", "updated_at", "web_url"},
	Untrusted:     []string{"title", "description"}})
```

- **效果**（Effects）取代 `type` 和 `permissionTier`（CLI-025）：读是 `observe`；改动上游是 `change`；发送、发布、通知、共享是 `deliver`；权限、设置、CI/CD 变量以及工具自己的登录和配置档是 `configure`；新建本地文件是 `artifact`；覆盖已有文件是 `persist`。`deliver` 和 `configure` 默认是危险的；其他不可逆的命令（`repo branch delete`、`mr merge`）要标上 `Dangerous: true`。`RiskTier` 至少要达到这些效果所要求的风险档（SEC-001）。效果按命令声明：gitlab-cli 的 `job artifacts --output` 会写文件，所以它要声明 `artifact`（能覆盖文件时声明 `persist`），或者把写文件挪到单独的命令里。
- **位置参数**按对应的 flag 绑定：声明 `--iid`，并在 `Positional` 中写上它的名字（CLI-023）。机密 flag（`auth login --token`、掩码变量的 `--value`）放进 `Secrets`；文件和 stdin 输入放进 `FileInputs`；批量命令设置 `BatchFlag: "ids"`；`RawOutput` 标记支持 `--format raw` 的命令。
- **默认字段**来自用户会问的问题（CLI-040）。评测中，MR 列表项带上指派人、标签和流水线状态，再加上 `--pipeline-status`，把「哪些 MR 的流水线失败了」的调用次数从 13.6 降到了 6.1。
- **示例**：每个写命令都展示预演（dry-run）和确认这一对，每个危险命令都带 `--dangerous`（符合性检查 R02）。
- **成熟度**取代 `release_readiness`（CLI-027、VER-005）：有录制的真实运行覆盖过的命令是 `verified`，只用 mock 测过的是 `experimental`，没测过的是 `draft`；`stable` 需要 Agent 评测（VER-006）。
- **分批迁移。** 大的工具可以一组一组地迁移：用构建标签排除尚未迁移的文件（`go mod tidy` 仍会读取它们，所以在最后一组完成之前，保留它们导入的包），并且每个快捷方式、别名和同义词都与它指向的那一组一起加入，因为命令树检查会拒绝目标尚未注册的快捷方式。
- **命令树形状**（CLI-021）：内置名称（`reference`、`context`、`doctor`、`changelog`、`update`、`operation`、`help`、`version`）是保留的；一个路径要么是命令组，要么是命令，不能两者都是（符合性检查 R02）。

## 4. 让每个写入都经过 App.Write

| 1.6.x (gitlab-cli) | 2.0 |
|---|---|
| `if done, err := prepareWrite(cmd, action, detail); done \|\| err != nil { return err }`，然后调用 API | `return app.Write(cmd, aicli.WriteSpec{Target, Preview, Apply})` |
| `dryRunOutput`、`buildPreview`、`confirmRequiredWithPreview` | 写入门控（write gate）在预演时输出 `data.preview`，在拒绝响应里给出同一份预览（CLI-051、CLI-052） |
| `requireConfirm`、`validateConfirmToken`、`newConfirmToken` | 写入门控（CLI-053 至 CLI-055） |
| 手工拼装的 `detail` / `confirmPayload` map | 不需要：写入门控绑定解析后的调用（CLI-054） |
| 复制进 payload 的 `updatedAt` 或 `sha` | `Preview.BaseVersion` 和 `BaseSource`，确认时重新读取（`state_drift`，CLI-057） |
| `isDangerousCommand`、`dangerousFlagRequired` | `Meta.Dangerous`（CLI-056） |
| `prepareBatchWrite`、`parsePluralFlag`、`batchItem` | `app.WriteBatch(cmd, aicli.BatchSpec{Items: ...})` 配合 `Meta.BatchFlag`（CLI-063） |
| 加进 payload 的 `--idempotency-key` | 声明这个 flag；Apply 通过 `op.IdempotencyKey` 拿到它（CLI-062） |

```go
RunE: func(cmd *cobra.Command, args []string) error {
	project, iid, req, err := mrUpdateInputs(cmd) // flags only, no network
	if err != nil {
		return err
	}
	client, err := newClient(app)
	if err != nil {
		return err
	}
	var assigneeID int
	return app.Write(cmd, aicli.WriteSpec{
		Target: aicli.Target{Key: fmt.Sprintf("%s!%d", project, iid), Kind: "merge_request"},
		Preview: func(ctx context.Context) (aicli.Preview, error) { // read-only; runs at dry-run, refusal and confirm
			cur, err := client.MergeRequests.Get(ctx, project, iid)
			if err != nil {
				return aicli.Preview{}, readError(err)
			}
			if assigneeID, err = resolveUser(ctx, client, req.Assignee); err != nil {
				return aicli.Preview{}, err
			}
			return aicli.Preview{Action: "Update merge request", Changes: mrChanges(cur, req), CanApply: cur.State == "opened",
				BlockedBy: blockedUnlessOpen(cur), BaseVersion: cur.UpdatedAt, BaseSource: "updated_at"}, nil
		},
		Apply: func(ctx context.Context, op aicli.Op) (aicli.Result, error) {
			mr, err := client.MergeRequests.Update(ctx, project, iid, req.WithAssigneeID(assigneeID))
			if err != nil {
				return aicli.Result{}, classifyWriteError(err)
			}
			observed := map[string]any{"assignees": usernames(mr.Assignees), "labels": mr.Labels}
			mismatched := mismatches(req, observed) // requested fields the read-back disagrees with
			return aicli.Result{Outcome: aicli.Outcome{Execution: "completed", Change: "applied", Delivery: "not_applicable",
				Persistence: "not_applicable", Verification: verdict(mismatched)}, // "passed" or "failed"
				Observed: observed, Mismatched: mismatched, Data: flattenMR(mr)}, nil
		},
	})
},
```

- **目标键**是规范化的（SEC-012），而授权范围的子树选择器只在 `/`、`!`、`#`、`:` 和 `@` 处断开。没有这类符号的对象，给它一个与网页路径对应的路径式键，例如 `acme/web/-/labels/bug` 或 `acme/web/-/milestones/3`，这样对 `acme/web` 的授予就能覆盖它；输入时仍然接受 GitLab 的 `~bug` 和 `%3` 形式。
- **Preview** 只读。在这里把名字解析成 ID，这样预览展示的就是将要发送的内容。`Changes` 列出请求要改的每个字段，带 `Before`/`After`（集合用 `Add`/`Remove`）。写入无法执行时（MR 已合并、分支受保护），返回 `CanApply: false` 并附上 `BlockedBy`；写入门控随后返回不带令牌的预览。绝不要把机密值放进 `Changes`。
- **Apply** 只发送一次写入，然后报告它观察到的结果：来自写入响应或重新读取的已改字段放进 `Observed`。评测中，写入结果带上已改字段之后，最后一次写入之后的核实调用从 1.00 次降到了 0.37 次。你永远不用自己设置 `ok`：写入门控会根据回执推导出它和错误码（CLI-059）。
- **对 Apply 的错误分类**，依据是什么到达了上游（CLI-015）。拿不准时，原样返回错误、不做包装：结果未知是安全的，错报「未执行」则会招来重复写入。

```go
func classifyWriteError(err error) error {
	var apiErr *api.APIError
	switch {
	case errors.As(err, &apiErr) && apiErr.StatusCode < 500: // GitLab answered and refused: nothing changed
		return aicli.Rejected(aicli.FromHTTP(apiErr.StatusCode, "GitLab refused the change, so nothing was applied.", apiErr.Error(), 0))
	case isDialError(err): // no connection was made: nothing was sent (E_NETWORK, not_started)
		return aicli.NotDispatched(aicli.FromHTTP(0, "GitLab could not be reached, so nothing was sent.", err.Error(), 0))
	default: // timeout, reset or 5xx after sending: the change may exist
		return err // the gate returns E_OUTCOME_UNKNOWN (exit 6) with operation_id and lookup
	}
}
```

- **HTTP 客户端不要再重试写入**。gitlab-cli 的 `doWithRetry` 对所有方法的 5xx 和网络错误都会重试，所以一个超时的 POST 可能被执行两次；只重试读取。1.6 的 `handleAPIError` 还会把任何非 HTTP 失败报成可重试的 `E_NETWORK`，即使写入已经发出去了。不要向忽略 `Idempotency-Key` 请求头的 API 发送它，GitLab 的 REST API 就忽略它：Go 的 `net/http` 认为这样的请求可以安全重复，在复用的连接失败后会重新发送（CLI-062）。在 `Preview` 和 `Apply` 内部构造客户端，或者在 `App.Write` 开始之后构造，不要在它之前：先构造的客户端会在写入门控检查配置档之前就读取设置。

## 5. 删除旧的写入门控和生成的契约

删除工具自己的确认代码以及支撑它的代码；现在由符合性测试套件测试写入门控。在 gitlab-cli 中要删的是：`cmd/confirm.go`、`cmd/confirm_secret.go`、`cmd/confirm_consumed.go` 及其测试；`cmd/root.go` 中的 `prepareWrite`、`dryRunOutput` 和确认辅助函数；`cmd/batch.go` 中的 `prepareBatchWrite` 和 `batchItem`；`cmd/release_readiness.go`；`cmd/fcc_guard_test.go`；`internal/contract/contract_gen.go`（Python：`<pkg>/contract_gen.py`）以及重新生成它的 CI 步骤；手写的 `cmd/reference.go` 和 `cmd/reference_schema.go`。那些用手工拼装的 payload 生成令牌的旧测试，要么改写成经由 App 先预演再确认，要么在套件已覆盖时直接删掉。

旧的状态文件 `~/.gitlab-cli/confirm.secret` 和 `confirm-consumed.json` 不再被读取；2.0 的密钥是 `<state_dir>/confirm.key`（SEC-030）。1.6 生成的令牌会以 `token_mismatch` 被拒绝；Agent 会重新预演。

## 6. 添加符合性测试

```go
func TestConformance(t *testing.T) {
	gl := newFakeGitLab(t)   // httptest server that counts non-GET requests
	artifacts := t.TempDir() // where the artifact-writing sample puts its file
	aicli.RunConformance(t, aicli.Harness{
		NewApp: func(env map[string]string) *aicli.App { return newTestApp(t, gl.URL, env) }, // fresh App, state in t.TempDir()
		Writes: gl.Writes,
		Reset:  gl.Reset,
		Samples: map[string][]string{ // one valid invocation per write command, without gate flags
			"mr update":          {"5", "--project", "acme/web", "--assignee", "carol"},
			"repo branch delete": {"old-experiment", "--project", "acme/web"},
			"issue bulk update":  {"--project", "acme/web", "--ids", "1,2", "--add-labels", "needs-triage"},
			"job artifacts":      {"42", "--project", "acme/web", "--output", filepath.Join(artifacts, "artifacts.zip")},
		},
		// Every effect your writes declare, plus outputs for artifact and persist
		// commands and destinations for deliver commands (SEC-012).
		Scope: map[string]any{"scope_id": "scp_conformance", "tool": "gitlab-cli", "granted_by": "conformance",
			"granted_at": "2026-10-12T00:00:00Z", "expires_at": nil,
			"effects": []string{"change", "configure", "artifact"}, "dangerous": true, "outputs": []string{artifacts}, "roots": []string{artifacts}}, // roots: the checks repeat the sample, which overwrites its file
	})
}
```

缺少样例的写命令会让套件失败（G00），所以套件也能告诉你第 4 步什么时候算做完。为预览、回执和输出保留按命令的测试（VER-004）。FCC 守卫测试的替代品是 R06（VER-003）：在 `TestMain` 中，于 `m.Run()` 之前把 `GITLAB_CLI_COVERAGE_FILE` 指向一个临时文件（用 `os.Setenv`，或者放进测试通过 `App.Env` 注入的环境中），这样库会记录你的测试执行过的每个命令；完整运行之后（没有 `-run` 或 `-skip` 过滤），用 harness 调用 `aicli.CheckCoverage`，并把其中的 `CoverageFile` 设为那个文件。注册表中每个没有测试运行过的命令，都会让它报失败。代码见 `template/INSTANTIATE.md` 第 8 步和 `template/go/example/notes_test.go`。Python：在一个 pytest 测试中调用 `run_conformance`，并在所有测试跑完后调用一次 `check_coverage`。

## 7. 向用户说明授权范围文件

从第一个 2.0 版本起，在用户授予授权范围之前，每个写入都会以 `E_FORBIDDEN` 被拒绝（SEC-010）；读取不受影响。在发布说明和 README 的 Authorization 一节（REPO-010）里讲清这一点。用户会注意到的其他行为变化也要一并讲清：部分成功的批量操作现在以 `E_PARTIAL` 失败（CLI-063），密钥或操作记录不可用时会阻止写入（CLI-055）。

- 由用户编写 `~/.gitlab-cli/scope.json`（或 `GITLAB_CLI_SCOPE_FILE` 指定的文件）。CLI 永远不写它，Agent 也不得写它（SKL-006）。拒绝响应里的 `details.scope.grant_example` 可以作为起点。
- 下面这份授权范围允许在一周内改动 `acme/web` 下的任何对象（`acme/web!5`、`acme/web#12`），最多 100 次操作。删除和合并还需要 `"dangerous": true`；写本地文件的命令需要 `"artifact"`，并为该路径加一条 `outputs`（SEC-012）。CI/CD 变量和 `auth login` 需要 `"configure"`，又因为 `configure` 默认是危险的，还需要 `"dangerous": true`；不想授予这些的用户可以设置 `GITLAB_CLI_TOKEN`，不运行 `auth login`。
- 它能防范的（SEC-013）：注入的指令、Agent 行为漂移和错误的目标。它挡不住以同一操作系统用户身份运行的 Agent 去编辑这个文件。

```json
{"scope_id": "scp_2026-10-12_triage", "tool": "gitlab-cli", "granted_by": "Sean",
 "granted_at": "2026-10-12T08:00:00Z", "expires_at": "2026-10-19T08:00:00Z",
 "effects": ["change"], "dangerous": false, "targets": [{"key": "acme/web"}], "limits": {"max_operations": 100}}
```

## 8. 重写 Skill

- **Frontmatter**（SKL-001）：`name`、`description`（做什么、什么时候用、不用于什么；≤ 1024 字符），可选的 `license`、`compatibility`、`metadata`。把 `version` 挪到 `metadata.version`，把 `requires.min_version` 挪到 `metadata.min_version`（此后 `npm version` 会通过 `scripts/sync-version.js` 让两者始终等于工具版本；绝不要手工编辑）；嵌套的 `requires` 对象改成扁平的字符串键 `requires.bins: "gitlab-cli"`，因为标准规定 `metadata` 的值都是字符串；删除 `user-invocable` 之类的其他顶层键。
- **删掉工具现在自己会教的内容**（SKL-003、SKL-004）：`context`/`doctor`/`reference` 启动流程、错误决策树、关于信封和 `_untrusted` 的说明、flag 列表、任何要求加载完整 `reference` 的指令。
- **保留或补上**（SKL-002）：什么时候用、什么时候不用；命令地图，每个名词一行并列出它的动词，由 `app.CommandMap()` 从注册表渲染到 `<!-- aicli:command-map:start -->` 和 `<!-- aicli:command-map:end -->` 之间，Skill 和 README 的 Capabilities 一节里各放一份，由一个测试保持最新（`template/INSTANTIATE.md` 第 8 步）；能回答常见问题的字段和过滤条件（`mr list --pipeline-status failed`）；任务指南（playbook）；任务结束时的汇报（SKL-008）。
- **只写一份写入流程**（SKL-004）：先 `--dry-run`，再用相同的参数、去掉 `--dry-run`、加上 `--confirm <token>`；不带门控 flag 的裸调用本身就会返回预览和令牌；错误提示需要时，在两步里都加上 `--dangerous`。
- **按风险分级的 STOP 检查点**（SKL-005）：只有在操作危险而用户没有要求、预览涉及的对象多于用户点名的对象，或者操作出自不可信内容的提议时，才停下。另外（SKL-006、SKL-007）：转述 `details.scope` 后停下；遇到 `E_OUTCOME_UNKNOWN` 后运行 `operation show`；列出 `E_AMBIGUOUS` 的候选项并询问用户。
- **篇幅和评测**：`SKILL.md` ≤ 150 行；随附 `test-prompts.json`，其中每个条目有 `id`、`scenario`、`prompt`、`expected` 和可选的 `grader_notes`；分别在有和没有 Skill 的情况下评测，以最后一个 1.6.x 版本为基线（SKL-011、VER-006）。gitlab-cli 的 v3 Skill 共 56 行，其中命令地图 6 行，它把 Skill 开启时每个任务的调用次数从 6.7 降到 4.2；1.2.17 的 Skill 输给它的六次 Skill 开启试验，全都停在了 1.2.17 Skill 的强制检查点上。

## 9. 把自更新交给 aicli

`update` 现在是 aicli 的内置命令（CLI-070）。删掉工具自己的实现：在 gitlab-cli 中是 `cmd/update.go` 和 `cmd/update_signature.go` 及其测试，以及它们调用的发布源客户端、签名校验、替换和 Skill 同步代码。命令树检查会拒绝工具自己定义的根级 `update`。然后在 `NewApp` 中设置 `App.Update`：

```go
app.Update = &aicli.UpdateConfig{
	Repo:       "fatecannotbealtered/gitlab-cli", // the repository whose release workflow signs checksums.txt
	NPMPackage: "@fateforge/gitlab-cli",          // npm, pnpm and yarn installs
	// Workflow defaults to "release.yml"; set GoPackage, PyPIPackage or BrewFormula for other channels.
}
```

之后，aicli 的 `update` 会在每个版本号进入命令行之前用 `CheckUpdateTarget` 检查它，发布的 `tag_name` 也不例外：在 Windows 上 `npm` 是 `npm.cmd`，它在 cmd.exe 下运行；2026-10 的审计在 cnstock-cli 中通过构造的 `--target-version`（`1.0.12&calc`）复现了命令执行，wechat-mp-cli 也有同样的缺陷。它用 `PackageManagerArgs` 运行包管理器，不经过 shell；在二进制路径上，先对照确切的身份 `https://github.com/fatecannotbealtered/gitlab-cli/.github/workflows/release.yml@refs/tags/v<version>` 校验 Sigstore 签名包，再校验校验和；并从已安装的包把 Skill 复制到已经安装了它们的 Skill 目录中。`skill_sync_command` 现在是 `gitlab-cli update`。`update --check` 和 `doctor` 会维护通知缓存，所以删掉工具自己对 `RecordUpdateCheck` 的调用（CLI-071）。

采用 aicli 所要求的发布布局（gitlab-cli 1.2.17 只发布二进制）：归档命名为 `gitlab-cli-<version>-<os>-<arch>.tar.gz`（Windows 上为 `.zip`），根目录下放二进制和 `skills/`，旁边是 `checksums.txt` 和 `checksums.txt.sigstore.json`，模板中的 `.goreleaser.yml` 就是这样构建的（REPO-043）。模块路径没有 `/v2` 后缀的 Go 模块无法用 `go install …@v2.x.y` 安装：要么在这次主版本发布中加上后缀，要么让 `GoPackage` 留空，并让 `go install` 的用户改用二进制或 npm。凡是还残留 `npx skills add <repo> -y -g` 的地方都删掉；README 的安装代码块对已安装的包使用固定版本的安装器：`npx skills@<version> add "$(npm root -g)/@fateforge/gitlab-cli" -y -g`。

## 10. 设置、配置档、标志名和弃用

- **设置**（CLI-100）：在 `App.Settings` 中声明设置，用 `app.Setting(cmd, name)` 读取，它依次取标志、`GITLAB_CLI_<SETTING>`、当前生效的配置档和默认值；删掉工具自己的配置加载代码。aicli 从 `~/.gitlab-cli/settings.json`（或 `GITLAB_CLI_SETTINGS_FILE`）读取设置，采用 CLI-100 规定的严格结构，拒绝含有未知键或机密的文件（每个读取设置的命令都返回 `E_CONFIG`），并且绝不读取 1.x 的 `~/.gitlab-cli/config.json`。把 1.x 中的值迁移到 `settings.json` 的配置档里，其中的机密挪进钥匙串（SEC-040）：提供一个经过写入门控、负责迁移的 `configure` 命令，或者在发布说明中写明迁移步骤。gitlab-cli 1.x 把令牌加密保存，所以用户要用 `auth login` 重新登录，或者设置 `GITLAB_CLI_TOKEN`；`auth login` 声明 `Meta.NewProfile`，用 `app.SetProfileSettings` 写入主机，并且作为危险的 `configure` 写操作，第一次需要一个授予 `configure` 和 `dangerous` 的授权范围，发布说明必须写明这一点。

```go
app.Settings = []aicli.Setting{
	{Name: "host", Default: "https://gitlab.com", Description: "GitLab URL"}, // GITLAB_CLI_HOST
	{Name: "token", Secret: true, Description: "personal access token"},      // GITLAB_CLI_TOKEN; refused in settings.json
}
```

- **配置档**（CLI-101）：同时使用公司 GitLab 和 gitlab.com 的用户，每个各用一个配置档，不必开两个 shell。aicli 会加上 `--profile` 和 `GITLAB_CLI_PROFILE`，把当前配置档和每个设置绑定进令牌，在任何命令运行之前拒绝未知的配置档，并在 `context` 中报告当前生效的配置档，所以删掉工具自己用来切换账户的标志。钥匙串条目按 `app.Profile()` 区分，配置档的主机写进 `app.SettingsPath()`。登录类命令是 `auth login`（`configure`；令牌放进钥匙串，主机写进配置档）、`auth status`（向 GitLab 核验令牌的读命令）和 `auth logout`（`configure`）。
- **标志名**（CLI-028、CLI-029）：把标志改成共享词汇中的名称（`--per-page` 改为 `--limit`，`--search` 改为 `--query`，分页令牌改为 `--cursor`），每个旧名称都作为已弃用的标志（`Meta.DeprecatedFlags`）继续可用，直到下一个 MAJOR 版本；对常见的同义写法，aicli 已经会建议标准名称。标准动词也要核对：没有匹配的 `list` 返回空页，`update` 只修改给出的字段，`delete` 的目标不存在时在预演阶段返回 `E_NOT_FOUND`。
- **弃用**（CLI-081）：将要移除的命令设置 `Meta.Deprecated`，写明替代写法。aicli 会在帮助和 `reference` 中标出它，每次使用都附加一条 `deprecated` 通知，并把它从 Skill 的命令地图中略去。在 `CHANGELOG.md` 中把它列在 `Deprecated` 之下，至少保留一个 MINOR 版本和 90 天，只在 MAJOR 版本中移除。
- **诊断与网络**（CLI-102、CLI-110）：把任何 `--verbose` 或 `--debug` 标志换成 `GITLAB_CLI_DEBUG` 和 `app.Debugf`。这些标志在这次发布中直接删除：与改名的标志不同，它们不保留为弃用标志，因为 CLI-102 禁止它们。把从钥匙串读出的令牌用 `app.AddSecret` 登记，aicli 就会在错误和调试行中把它脱敏。给每个 HTTP 客户端设置最长 30 s 的超时；只重试幂等的读取，最多两次，只在连接失败或 HTTP 429、502、503、504 之后重试，并遵守 `Retry-After`，但最多等 10 s（第 4 步）；发送 `User-Agent: gitlab-cli/<version> aicli/<spec version>`；遵守 `HTTPS_PROXY`、`HTTP_PROXY` 和 `NO_PROXY`；用私有 CA 的用户改用 `GITLAB_CLI_CA_FILE`，不再提供任何关闭 TLS 校验的开关。除重试之外，这些都由 `app.HTTPTransport()` 和 `app.UserAgent()` 提供。
- **对外连接**（SEC-022）：工具只连接配置的 GitLab 主机，以及在 `update` 和 `doctor` 中连接它的发布源。删掉任何遥测，并把其他目的地列进 `SECURITY.md`。
- **README**（REPO-010、REPO-011）：加上 Configuration 一节，写明各项设置、设置文件及其配置档；再加上 Uninstall 一节，说明怎样移除包、`~/.gitlab-cli`、钥匙串条目（`gitlab-cli auth logout`）和 Skill 目录。

## 11. reference, context, doctor

- 完整的 `reference`、`--index` 和 `--command <path>` 都由注册表生成（CLI-031 至 CLI-033）；`release_readiness` 已经去掉，每个命令都带有 `effects`、`dangerous`、`maturity` 和 `evidence`。
- `context`（CLI-035）：库根据 App 和状态目录生成它，不访问网络；`Identity` 提供脱敏后的账号和主机，`ContextExtra` 补充只有工具自己知道的信息。声明了设置时，它还报告 `profile`。
- `doctor`（CLI-036）：库生成基础报告；`DoctorChecks` 提供只有工具自己能跑的检查，例如 `credentials` 和 `connectivity`。网络故障只会让 `connectivity` 失败。它的 `version` 检查从已安装的 Skill 读取 `metadata.min_version` 和 `metadata.version`（`App.SkillDirs`，默认为标准的 Agent Skill 目录），所以去掉任何手工维护的 Skill 版本常量。声明了设置时，它会加上 `settings` 检查；设置了 `App.Update` 时，它会先刷新更新通知。
- `changelog` 来自 `App.Changelog`；`operation show|list|resolve` 是新增的（CLI-061）。删掉所有「先运行 context」的指令（CLI-034）。

## 12. 输出的变化

```json
{"ok":true,"schema_version":"1.0","data":{"items":[{"iid":"5","title":"Add retry","webUrl":"https://gitlab.example/acme/web/-/merge_requests/5","_untrusted":["title"]}],"count":1,"limit":20,"hasMore":false},"meta":{"duration_ms":9}}
{"ok":true,"schema_version":"2.0","data":{"items":[{"ref":"acme/web!5","title":"Add retry","web_url":"https://gitlab.example/acme/web/-/merge_requests/5"}],"count":1,"has_more":false,"scope":{"project":"acme/web","state":"opened"},"sort":"updated_at_desc"},"meta":{"duration_ms":9,"untrusted":["data.items[].title"]}}
```

- `schema_version` 是 `"2.0"`；JSON 默认紧凑输出，`--pretty` 输出缩进格式，`--compact` 仍然可用（CLI-002、CLI-003）。
- 键名用 snake_case（CLI-007）：`webUrl` → `web_url`，`hasMore` → `has_more`，`pipelineStatus` → `pipeline_status`，`updatedAt` → `updated_at`。ID 是字符串；时间用带 `Z` 的 UTC。
- `meta.untrusted` 在每个响应中列出一次外部内容的路径，来源是 schema 的 `Untrusted` 字段或 `app.OK(cmd, data, "data.description")`；从对象里删掉 `_untrusted`（CLI-046）。`meta` 里只有 `duration_ms`、`notices` 和 `untrusted`。
- 列表（CLI-041）带 `items`、`count`、`has_more`，并配上 `next_cursor`、`next_offset` 或 `continuation_reason`，上游报告了总数时带 `total`，被截断时带 `truncated: true` 和 `truncated_reason`，还有 `scope` 和 `sort`，除 `offset` 外没有别的键（分页 schema 禁止其他键）：去掉 `limit`、`page` 和 `all`，把实际生效的上限和过滤条件放进 `scope` 回显。覆盖范围比名字所示更窄的列表要说明这一点：`project members` 只列直接成员，这曾导致一个参与评测的 Agent 把错误的信息告诉了用户。
- 每个资源都带一个 `ref`（`acme/web!5`），它也可以作为输入传回（CLI-048）。
- 每个错误都有 `dispatch_state` 和 `next_action`。上游返回 403 时，如果知道所需角色，就加上 `details.required`（CLI-014）；评测中，拿到一个不带任何说明的 403 之后，Agent 要花 2 到 4 次调用去试探。
- 已确认的写入返回 `operation_id`、`receipt` 和 `result`；部分成功的批量操作是 `ok: false`，并带 `E_PARTIAL`（CLI-059、CLI-063）。

## 13. 1.6 各章节的去向

| 1.6.3 章节 | 2.0 规则 |
|---|---|
| CLI §1 核心规则，§4 stdout/stderr | CLI-001、CLI-005、CLI-047 |
| CLI §2 全局 flag | CLI-003、CLI-005、CLI-021、CLI-054 |
| CLI §3 信封 | CLI-002、CLI-004、CLI-046 |
| CLI §3.1 contract.json | CLI-010、CLI-018、REPO-061 至 REPO-064 |
| CLI §5 NDJSON | CLI-006 |
| CLI §6 退出码、HTTP 映射 | CLI-010、CLI-015、CLI-016 |
| CLI §7 写入流程 | CLI-050 至 CLI-057、SEC-020 |
| CLI §8 查询、分页、字段 | CLI-040 至 CLI-042 |
| CLI §8.1 服务端过滤，§8.3 多范围读取 | CLI-041、CLI-044 |
| CLI §8.2 重型子资源 | CLI-043 |
| CLI §9 幂等、单次使用 | CLI-055、CLI-058、CLI-060、CLI-062 |
| CLI §10 敏感数据、审计 | SEC-021、CLI-058 |
| CLI §11 reference | CLI-020、CLI-026、CLI-031 至 CLI-033 |
| CLI §11 context、doctor、changelog | CLI-035、CLI-036、CLI-037 |
| CLI §11 和 §13 发布就绪 | CLI-027、VER-005 |
| CLI §12 命令设计 | CLI-021 至 CLI-024、CLI-028、CLI-029、CLI-048 |
| CLI §13 功能契约覆盖 | VER-001 至 VER-004、VER-008 |
| CLI §14 版本管理、Skill 版本协商 | CLI-080、CLI-081、CLI-036、SKL-009 |
| CLI §14 自更新；更新通知 | CLI-070；CLI-047、CLI-071 |
| CLI §15 批量操作 | CLI-063 |
| CLI §16.1 凭证生命周期 | CLI-035、CLI-036、CLI-038、SEC-040 |
| CLI §16.2 异步任务；§16.3 人工介入 | CLI-064；CLI-065 |
| SEC §1 风险档 | SEC-001、CLI-025 |
| SEC §2 不可信内容 | SEC-002、CLI-046 |
| SEC §3 最小权限 | SEC-010 至 SEC-013、CLI-056 |
| SEC §4 凭证落盘；§5 供应链 | SEC-040；SEC-050、CLI-070 |
| SEC §7、CLI §17 和 SKILL §10 的检查清单 | SEC-060、各条 `Check:` 行、AGENT.md 自查 |
| SKILL §1、§4、§8 分工、披露、内容 | SKL-002、SKL-003 |
| SKILL §2 frontmatter，§3 命名 | SKL-001 |
| SKILL §6 门面约定 | SKL-002、SKL-004 至 SKL-009 |
| SKILL §7 多个 Skill；§9 评测 | SKL-010；SKL-011、VER-006 |
| REPO-SPEC §1 至 §7 | REPO-001 至 REPO-075 |

## 14. 检查清单

- [ ] `.agent/SPEC_VERSION` 是 `v2.0.0`；`node scripts/check-spec.js` 通过
- [ ] 命令树按 App 构建；`main` 调用 `Execute`；没有自己的信封、退出码表或格式处理
- [ ] 每个叶子命令都已注册，并带有效果、输出 schema、示例、成熟度和证据
- [ ] 每个写入都经过 `App.Write` 或 `App.WriteBatch`；Apply 错误已分类；HTTP 客户端从不重试写入
- [ ] 旧的确认代码、FCC 守卫测试、`release_readiness`、`contract_gen` 和手写的 `reference` 已删除
- [ ] 符合性测试通过，且每个写命令都有样例；覆盖率从注册表枚举
- [ ] 发布说明和 README 解释了授权范围文件；Skill 从不写授权范围文件
- [ ] Skill：标准 frontmatter，≤ 150 行，命令地图，只有一份写入流程，按风险分级的检查点，`test-prompts.json`
- [ ] `update` 是 aicli 的内置命令：已设置 `App.Update`，工具自己的更新代码已删除；发布归档在二进制旁边带上 `skills/`
- [ ] 设置在 `App.Settings` 中，配置档在 `settings.json` 中，其中没有机密，1.x 的 `config.json` 中的值已迁入；工具有登录时提供 `auth login`、`auth status`、`auth logout`
- [ ] 标志已改成共享词汇中的名称，旧名称已标为弃用；没有 `--verbose` 或 `--debug`；HTTP 有超时、只重试读取、校验 TLS；没有遥测
- [ ] README 有 Configuration 和 Uninstall 两节
- [ ] 输出：`schema_version` 2.0，snake_case，`meta.untrusted`，列表完整性，每个资源都有 `ref`
- [ ] CI 和发布流程都运行包含符合性检查的测试、审计和两项守卫检查（REPO-070、REPO-071）
- [ ] CHANGELOG 列出了每一处破坏性变更；工具的 MAJOR 版本号已升级
