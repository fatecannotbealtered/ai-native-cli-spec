# Migrating a 1.6.x tool to ai-native-cli-spec 2.0.0

**中文版 → [MIGRATION-2.0_zh.md](MIGRATION-2.0_zh.md)**

For maintainers of a tool pinned to spec 1.6.x. Work through the steps in order. Examples use gitlab-cli (Go, cobra). A Python tool follows the same steps with the Python library imported from `<pkg>/_aicli`, which uses the same names, snake_case for functions and methods (`app.write`, `WriteSpec`, `run_conformance`, `parse_version`); a typer app passes `typer.main.get_command(app)` to the library. The result breaks the agent contract (`schema_version` 2.0, renamed keys, scopes for writes), so release it as a new MAJOR version (REPO-030).

## 1. Pin 2.0.0 and sync

The 1.6 copy of `sync-spec.js` in your repo reads the 1.6 file list and does not know the library, so run the 2.0 copy once:

```bash
# from the tool's repo root
git clone --depth 1 --branch v2.0.0 https://github.com/fatecannotbealtered/ai-native-cli-spec ../ai-native-cli-spec-2.0.0
node ../ai-native-cli-spec-2.0.0/template/common/scripts/sync-spec.js --from ../ai-native-cli-spec-2.0.0 --version v2.0.0
node scripts/check-spec.js
```

This writes `v2.0.0` to `.agent/SPEC_VERSION` and vendors the five specs (`VERIFY-SPEC.md` is new), `contract/contract.json`, `contract/schemas/`, the library with its `MANIFEST.json` (`internal/aicli/` or `<pkg>/_aicli/`) and the sync scripts (REPO-061). Later syncs use `node scripts/sync-spec.js`. The Go library needs cobra, pflag and sigstore-go (run the `go get` line sync-spec prints, then `go mod tidy`); the Python library needs Python ≥ 3.10, click and sigstore. Never edit vendored files (REPO-063). Keep the codes in `contract/contract-ext.json` that the tool can still raise, check them against CLI-018 and declare them to the library in `App.ExtCodes`; drop codes 2.0 makes impossible (gitlab-cli's only one, `E_CANCELLED`, came from interactive prompts), and the file when none is left.

## 2. Hand the envelope and errors to aicli

The conformance suite builds a fresh App and command tree for every check, so build the tree in a function: package-level command variables, `init()` registration and global flag variables (`jsonMode`, `dryRun`, `confirmFlag`, `dangerousMode`) go. The App owns the global flags.

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
| `handleAPIError(err, jsonMode)` on a read | a `readError(err)` helper returning `aicli.FromHTTP(apiErr.StatusCode, message, apiErr.Error(), 0)`, with a one-sentence `message` for the model (CLI-017); status `0` (no connection made) gives `E_NETWORK` (CLI-016) |
| `failWithCode`, `emitError`, exit-code constants, `exitCodeForStatus` | `aicli.NewError(code, message, details)` and the constructors; the library adds the exit code, `retryable` and default `dispatch_state` and `next_action` (CLI-010, CLI-011) |
| `--format`, `--json`, `--compact`, `--fields` handling; the agent-safe switches in `cmd/agent.go`, hand-kept aliases and suggestions | the App and the fields above (CLI-003, CLI-022); ref and URL parsing stays in the tool (CLI-048) |

## 3. Register every command with Meta

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

- **Effects** replace `type` and `permissionTier` (CLI-025): reads are `observe`; upstream changes `change`; send, publish, notify, share `deliver`; permissions, settings, CI/CD variables and the tool's own login and profiles `configure`; a new local file `artifact`; an overwritten one `persist`. `deliver` and `configure` are dangerous by default; mark other irreversible commands (`repo branch delete`, `mr merge`) `Dangerous: true`. `RiskTier` must be at least what the effects require (SEC-001). Effects are per command: gitlab-cli's `job artifacts --output` writes a file, so it declares `artifact` (`persist` if it can overwrite), or the file write moves to its own command.
- **Positional values** bind as their flag: declare `--iid` and name it in `Positional` (CLI-023). Secret flags (`auth login --token`, masked variable `--value`) go in `Secrets`; file and stdin inputs in `FileInputs`; batch commands set `BatchFlag: "ids"`; `RawOutput` marks commands that support `--format raw`.
- **Default fields** come from the questions users ask (CLI-040). In the eval, MR items with assignees, labels and pipeline status plus `--pipeline-status` cut "which MRs have failed pipelines" from 13.6 to 6.1 calls.
- **Examples**: every write shows the dry-run and confirm pair, every dangerous command includes `--dangerous` (conformance R02).
- **Maturity** replaces `release_readiness` (CLI-027, VER-005): commands a recorded live run exercised are `verified`, mock-only ones `experimental`, untested ones `draft`; `stable` needs an agent eval (VER-006).
- **Staged migration.** A large tool can move group by group: exclude the unmigrated files with a build tag (`go mod tidy` still reads them, so keep the packages they import until the last group is done), and add each shortcut, alias and synonym together with the group it points to, because the tree check rejects one whose target is not registered.
- **Tree shape** (CLI-021): built-in names (`reference`, `context`, `doctor`, `changelog`, `update`, `operation`, `help`, `version`) are reserved, and a path is a group or a command, never both (conformance R02).

## 4. Route every write through App.Write

| 1.6.x (gitlab-cli) | 2.0 |
|---|---|
| `if done, err := prepareWrite(cmd, action, detail); done \|\| err != nil { return err }` then the API call | `return app.Write(cmd, aicli.WriteSpec{Target, Preview, Apply})` |
| `dryRunOutput`, `buildPreview`, `confirmRequiredWithPreview` | the gate emits `data.preview` on dry-run and the same preview in the refusal (CLI-051, CLI-052) |
| `requireConfirm`, `validateConfirmToken`, `newConfirmToken` | the gate (CLI-053 to CLI-055) |
| hand-built `detail` / `confirmPayload` maps | nothing: the gate binds the parsed invocation (CLI-054) |
| `updatedAt` or `sha` copied into the payload | `Preview.BaseVersion` and `BaseSource`, re-read at confirm (`state_drift`, CLI-057) |
| `isDangerousCommand`, `dangerousFlagRequired` | `Meta.Dangerous` (CLI-056) |
| `prepareBatchWrite`, `parsePluralFlag`, `batchItem` | `app.WriteBatch(cmd, aicli.BatchSpec{Items: ...})` with `Meta.BatchFlag` (CLI-063) |
| `--idempotency-key` added to the payload | declare the flag; Apply gets it as `op.IdempotencyKey` (CLI-062) |

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

- **Target keys** are canonical (SEC-012), and a scope's subtree selectors break only at `/`, `!`, `#`, `:` and `@`. Give a kind without such a sigil a path-style key that mirrors its web path, `acme/web/-/labels/bug` or `acme/web/-/milestones/3`, so a grant for `acme/web` covers it; keep accepting GitLab's `~bug` and `%3` forms as input.
- **Preview** only reads. Resolve names to IDs there, so the preview shows what will be sent. `Changes` lists each field the request changes with `Before`/`After` (or `Add`/`Remove` for sets). When the write cannot apply (merged MR, protected branch), return `CanApply: false` with `BlockedBy`; the gate then returns the preview without a token. Never put secret values in `Changes`.
- **Apply** sends the write once, then reports what it observed: the changed fields from the write response or a re-read go in `Observed`. In the eval, write results that carried the changed fields cut verification calls after the last write from 1.00 to 0.37. You never set `ok`: the gate derives it and the error code from the receipt (CLI-059).
- **Classify Apply errors** by what reached the upstream (CLI-015). When unsure, return the error unwrapped: an unknown outcome is safe, a wrong "not applied" invites a duplicate write.

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

- **Stop retrying writes in the HTTP client.** gitlab-cli's `doWithRetry` retries 5xx and network errors for every method, so a timed-out POST can apply twice; retry reads only. 1.6's `handleAPIError` also reported any non-HTTP failure as retryable `E_NETWORK`, even after a write was sent. Do not send an `Idempotency-Key` header to an API that ignores it, as GitLab's REST API does: Go's `net/http` treats such a request as safe to repeat and re-sends it after a failure on a reused connection (CLI-062). Build the client inside `Preview` and `Apply`, or after `App.Write` starts, rather than before it: a client built first reads settings before the gate has checked the profile.

## 5. Delete the old gate and generated contract

Delete the tool's own confirm code and what supported it; the conformance suite now tests the gate. In gitlab-cli: `cmd/confirm.go`, `cmd/confirm_secret.go`, `cmd/confirm_consumed.go` and their tests; `prepareWrite`, `dryRunOutput` and the confirm helpers in `cmd/root.go`; `prepareBatchWrite` and `batchItem` in `cmd/batch.go`; `cmd/release_readiness.go`; `cmd/fcc_guard_test.go`; `internal/contract/contract_gen.go` (Python: `<pkg>/contract_gen.py`) and the CI steps that regenerated it; the hand-written `cmd/reference.go` and `cmd/reference_schema.go`. Rewrite the legacy tests that minted tokens from hand-built payloads as dry-run then confirm through the App, or drop those the suite covers.

The old state files `~/.gitlab-cli/confirm.secret` and `confirm-consumed.json` are no longer read; the 2.0 key is `<state_dir>/confirm.key` (SEC-030). Tokens minted by 1.6 are refused as `token_mismatch`; agents re-run the dry-run.

## 6. Add the conformance test

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

A write command without a sample fails the suite (G00), so the suite also tells you when step 4 is complete. Keep per-command tests for previews, receipts and outputs (VER-004). The FCC guard's replacement is R06 (VER-003): in `TestMain`, point `GITLAB_CLI_COVERAGE_FILE` at a temporary file before `m.Run()` (with `os.Setenv`, or in the environment the tests inject through `App.Env`), so the library records every command your tests execute; after a full run (no `-run` or `-skip` filter), call `aicli.CheckCoverage` with the harness and `CoverageFile` set to that file. It fails for every registry command no test ran. `template/INSTANTIATE.md` step 8 and `template/go/example/notes_test.go` show the code. Python: `run_conformance` in a pytest test, and `check_coverage` once every test has run.

## 7. Tell users about scope files

From the first 2.0 release, every write is refused with `E_FORBIDDEN` until the user grants a scope (SEC-010); reads are unaffected. Say so in the release notes and in the README's Authorization section (REPO-010), together with the other behaviour users will notice: a partial batch now fails with `E_PARTIAL` (CLI-063), and an unusable key or journal blocks writes (CLI-055).

- The user writes `~/.gitlab-cli/scope.json` (or the file named by `GITLAB_CLI_SCOPE_FILE`). The CLI never writes it, and agents must not (SKL-006). The refusal's `details.scope.grant_example` is a starting point.
- The grant below allows changes to anything under `acme/web` (`acme/web!5`, `acme/web#12`) for a week, at most 100 operations. Deletes and merges also need `"dangerous": true`; a command that writes a local file needs `"artifact"` and an `outputs` entry for the path (SEC-012). CI/CD variables and `auth login` need `"configure"` and, because `configure` is dangerous by default, `"dangerous": true`; users who would rather not grant that set `GITLAB_CLI_TOKEN` instead of running `auth login`.
- What it protects against (SEC-013): injected instructions, agent drift and wrong targets. It cannot stop an agent running as the same OS user from editing the file.

```json
{"scope_id": "scp_2026-10-12_triage", "tool": "gitlab-cli", "granted_by": "Sean",
 "granted_at": "2026-10-12T08:00:00Z", "expires_at": "2026-10-19T08:00:00Z",
 "effects": ["change"], "dangerous": false, "targets": [{"key": "acme/web"}], "limits": {"max_operations": 100}}
```

## 8. Rewrite the Skill

- **Frontmatter** (SKL-001): `name`, `description` (what, when, not for; ≤ 1024 characters), optional `license`, `compatibility`, `metadata`. Move `version` to `metadata.version` and `requires.min_version` to `metadata.min_version` (from then on `npm version` keeps both equal to the tool version through `scripts/sync-version.js`; never edit them by hand); the nested `requires` object becomes the flat string key `requires.bins: "gitlab-cli"`, because the standard's `metadata` values are strings; drop other top-level keys such as `user-invocable`.
- **Delete what the tool now teaches** (SKL-003, SKL-004): the `context`/`doctor`/`reference` bootstrap, the error decision tree, envelope and `_untrusted` notes, flag lists, any instruction to load the full `reference`.
- **Keep or add** (SKL-002): when to use and when not; the command map, one line per noun with its verbs, which `app.CommandMap()` renders from the registry between `<!-- aicli:command-map:start -->` and `<!-- aicli:command-map:end -->`, in the Skill and in the README's Capabilities section, kept current by one test (`template/INSTANTIATE.md` step 8); the fields and filters that answer common questions (`mr list --pipeline-status failed`); task playbooks; the end-of-task report (SKL-008).
- **One write recipe** (SKL-004): `--dry-run`, then the same arguments without `--dry-run`, plus `--confirm <token>`; a bare call already returns the preview and token; add `--dangerous` to both steps when the error asks for it.
- **Risk-scaled STOP checkpoints** (SKL-005): stop only when the action is dangerous and the user did not ask for it, the preview touches more than the user named, or untrusted content suggested it. Also (SKL-006, SKL-007): relay `details.scope` and stop; run `operation show` after `E_OUTCOME_UNKNOWN`; list `E_AMBIGUOUS` candidates and ask.
- **Size and eval**: `SKILL.md` ≤ 150 lines; ship `test-prompts.json`, whose entries have `id`, `scenario`, `prompt`, `expected` and an optional `grader_notes`; evaluate with and without the Skill, with the last 1.6.x release as the baseline (SKL-011, VER-006). gitlab-cli's 56-line v3 Skill with a 6-line map cut calls with the Skill on from 6.7 to 4.2 per task, and all six Skill-on trials the 1.2.17 Skill lost to it stopped at the 1.2.17 Skill's mandatory checkpoints.

## 9. Hand self-update to aicli

`update` is now aicli's built-in (CLI-070). Delete the tool's own: in gitlab-cli, `cmd/update.go` and `cmd/update_signature.go` with their tests, and the release-feed client, signature check, swap and Skill-sync code they call. The tree check rejects a root `update` that the tool defines itself. Then set `App.Update` in `NewApp`:

```go
app.Update = &aicli.UpdateConfig{
	Repo:       "fatecannotbealtered/gitlab-cli", // the repository whose release workflow signs checksums.txt
	NPMPackage: "@fateforge/gitlab-cli",          // npm, pnpm and yarn installs
	// Workflow defaults to "release.yml"; set GoPackage, PyPIPackage or BrewFormula for other channels.
}
```

aicli's `update` then checks every version, the release's `tag_name` included, with `CheckUpdateTarget` before it reaches a command line: on Windows `npm` is `npm.cmd`, which runs under cmd.exe, and the 2026-10 audit reproduced command execution through a crafted `--target-version` (`1.0.12&calc`) in cnstock-cli; wechat-mp-cli had the same flaw. It runs package managers from `PackageManagerArgs` without a shell, verifies the Sigstore bundle on the binary path against the exact identity `https://github.com/fatecannotbealtered/gitlab-cli/.github/workflows/release.yml@refs/tags/v<version>` and then the checksum, and copies the Skills from the installed package into the Skill directories where they are installed. `skill_sync_command` is now `gitlab-cli update`. `update --check` and `doctor` keep the notice cache, so drop the tool's own `RecordUpdateCheck` calls (CLI-071).

Adopt the release layout aicli expects (gitlab-cli 1.2.17 shipped the binary only): archives named `gitlab-cli-<version>-<os>-<arch>.tar.gz` (`.zip` on Windows) with the binary and `skills/` at their root, and `checksums.txt` and `checksums.txt.sigstore.json` beside them, as the template's `.goreleaser.yml` builds them (REPO-043). A Go module whose path has no `/v2` suffix cannot be installed with `go install …@v2.x.y`: add the suffix in the MAJOR release, or leave `GoPackage` empty and point `go install` users at the binary or npm. Remove `npx skills add <repo> -y -g` wherever it remains; the README's install block uses the pinned installer on the installed package, `npx skills@<version> add "$(npm root -g)/@fateforge/gitlab-cli" -y -g`.

## 10. Settings, profiles, flag names and deprecation

- **Settings** (CLI-100): declare the settings in `App.Settings` and read them with `app.Setting(cmd, name)`, which takes the flag, then `GITLAB_CLI_<SETTING>`, then the active profile, then the default; delete the tool's own config loader. aicli reads them from `~/.gitlab-cli/settings.json` (or `GITLAB_CLI_SETTINGS_FILE`) in the strict shape of CLI-100, refuses a file with unknown keys or a secret in it (`E_CONFIG` on every command that reads a setting), and never reads the 1.x `~/.gitlab-cli/config.json`. Move the 1.x values into profiles in `settings.json`, and any secret into the keyring (SEC-040): offer a gated `configure` command that does it, or give the steps in the release notes. gitlab-cli 1.x kept the token encrypted, so users sign in again with `auth login` or set `GITLAB_CLI_TOKEN`; `auth login` declares `Meta.NewProfile`, writes the host with `app.SetProfileSettings`, and, as a dangerous `configure` write, needs a scope granting `configure` and `dangerous` the first time, which the release notes must say.

```go
app.Settings = []aicli.Setting{
	{Name: "host", Default: "https://gitlab.com", Description: "GitLab URL"}, // GITLAB_CLI_HOST
	{Name: "token", Secret: true, Description: "personal access token"},      // GITLAB_CLI_TOKEN; refused in settings.json
}
```

- **Profiles** (CLI-101): a user with a company GitLab and gitlab.com gets one profile for each instead of two shells. aicli adds `--profile` and `GITLAB_CLI_PROFILE`, binds the active profile and every setting into tokens, refuses an unknown profile before any command runs and reports the active profile in `context`, so remove any account-switching flag of the tool's own. Key keyring entries by `app.Profile()`, and write the profile's host into `app.SettingsPath()`. The sign-in commands are `auth login` (`configure`; the token goes to the keyring, the host to the profile), `auth status` (a read that checks the token with GitLab) and `auth logout` (`configure`).
- **Flag names** (CLI-028, CLI-029): rename flags to the shared vocabulary (`--per-page` to `--limit`, `--search` to `--query`, a page token to `--cursor`) and keep each old name working as a deprecated flag (`Meta.DeprecatedFlags`) until the next MAJOR release; aicli already suggests the standard name for common synonyms. Check the standard verbs too: a `list` with no match returns an empty page, `update` changes only the fields given, and a `delete` of a missing target is `E_NOT_FOUND` at the dry-run.
- **Deprecation** (CLI-081): a command that is going away gets `Meta.Deprecated` with its replacement. aicli marks it in help and `reference`, adds a `deprecated` notice to every use and leaves it out of the Skill's command map. List it under `Deprecated` in `CHANGELOG.md`, keep it for at least one MINOR release and 90 days, and remove it only in a MAJOR release.
- **Diagnostics and network** (CLI-102, CLI-110): replace any `--verbose` or `--debug` flag with `GITLAB_CLI_DEBUG` and `app.Debugf`. Remove those flags in this release: unlike renamed flags, they are not kept as deprecated, because CLI-102 forbids them. Register the token read from the keyring with `app.AddSecret`, so aicli redacts it in errors and debug lines. Give every HTTP client a timeout of at most 30 s; retry only idempotent reads, at most twice, after a connection failure or HTTP 429, 502, 503 or 504, honouring `Retry-After` up to 10 s (step 4); send `User-Agent: gitlab-cli/<version> aicli/<spec version>`; honour `HTTPS_PROXY`, `HTTP_PROXY` and `NO_PROXY`; and point users with a private CA at `GITLAB_CLI_CA_FILE` instead of any switch that turns TLS verification off. `app.HTTPTransport()` and `app.UserAgent()` provide all of this but the retries.
- **Outbound contact** (SEC-022): the tool talks only to the configured GitLab host and, in `update` and `doctor`, to its release source. Remove any telemetry, and list any other destination in `SECURITY.md`.
- **README** (REPO-010, REPO-011): a Configuration section with the settings, the settings file and its profiles, and an Uninstall section that removes the package, `~/.gitlab-cli`, the keyring entry (`gitlab-cli auth logout`) and the Skill directories.

## 11. reference, context, doctor

- `reference` full, `--index` and `--command <path>` are built from the registry (CLI-031 to CLI-033); `release_readiness` is gone, and each command carries `effects`, `dangerous`, `maturity` and `evidence`.
- `context` (CLI-035): the library builds it from the App and the state directory, with no network; `Identity` supplies the redacted account and host, and `ContextExtra` adds what only the tool knows. With settings declared, it also reports `profile`.
- `doctor` (CLI-036): the library builds the base report; `DoctorChecks` supplies the checks only the tool can run, such as `credentials` and `connectivity`. A network failure fails only `connectivity`. Its `version` check reads `metadata.min_version` and `metadata.version` from the installed Skill (`App.SkillDirs`, default the standard agent Skill directories), so drop any hand-kept Skill version constant. With settings declared it adds a `settings` check, and with `App.Update` set it refreshes the update notice first.
- `changelog` comes from `App.Changelog`; `operation show|list|resolve` are new (CLI-061). Remove every "run context first" instruction (CLI-034).

## 12. Output changes

```json
{"ok":true,"schema_version":"1.0","data":{"items":[{"iid":"5","title":"Add retry","webUrl":"https://gitlab.example/acme/web/-/merge_requests/5","_untrusted":["title"]}],"count":1,"limit":20,"hasMore":false},"meta":{"duration_ms":9}}
{"ok":true,"schema_version":"2.0","data":{"items":[{"ref":"acme/web!5","title":"Add retry","web_url":"https://gitlab.example/acme/web/-/merge_requests/5"}],"count":1,"has_more":false,"scope":{"project":"acme/web","state":"opened"},"sort":"updated_at_desc"},"meta":{"duration_ms":9,"untrusted":["data.items[].title"]}}
```

- `schema_version` is `"2.0"`; JSON is compact by default, `--pretty` indents, `--compact` is still accepted (CLI-002, CLI-003).
- Keys are snake_case (CLI-007): `webUrl` → `web_url`, `hasMore` → `has_more`, `pipelineStatus` → `pipeline_status`, `updatedAt` → `updated_at`. IDs are strings; times are UTC with `Z`.
- `meta.untrusted` lists external-content paths once per response, from the schema's `Untrusted` fields or `app.OK(cmd, data, "data.description")`; remove `_untrusted` from objects (CLI-046). `meta` holds nothing but `duration_ms`, `notices` and `untrusted`.
- Lists (CLI-041) carry `items`, `count`, `has_more` with `next_cursor`, `next_offset` or `continuation_reason`, `total` when the upstream reports it, `truncated: true` with `truncated_reason` when capped, `scope` and `sort`, and no other keys but `offset` (the page schema forbids them): drop `limit`, `page` and `all`, and echo the applied limit and filters in `scope`. A list that covers less than its name says so: `project members` lists direct members only, which led an eval agent to tell the user something false.
- Every resource carries one `ref` (`acme/web!5`), accepted back as input (CLI-048).
- Every error has `dispatch_state` and `next_action`. On an upstream 403, add `details.required` when you know the role (CLI-014); in the eval, agents spent 2 to 4 calls probing after a bare 403.
- A confirmed write returns `operation_id`, `receipt` and `result`; a partial batch is `ok: false` with `E_PARTIAL` (CLI-059, CLI-063).

## 13. Where 1.6 sections went

| 1.6.3 section | 2.0 rules |
|---|---|
| CLI §1 core rules, §4 stdout/stderr | CLI-001, CLI-005, CLI-047 |
| CLI §2 global flags | CLI-003, CLI-005, CLI-021, CLI-054 |
| CLI §3 envelope | CLI-002, CLI-004, CLI-046 |
| CLI §3.1 contract.json | CLI-010, CLI-018, REPO-061 to REPO-064 |
| CLI §5 NDJSON | CLI-006 |
| CLI §6 exit codes, HTTP mapping | CLI-010, CLI-015, CLI-016 |
| CLI §7 write flow | CLI-050 to CLI-057, SEC-020 |
| CLI §8 query, pagination, fields | CLI-040 to CLI-042 |
| CLI §8.1 server-side filters, §8.3 multi-scope reads | CLI-041, CLI-044 |
| CLI §8.2 heavy sub-resources | CLI-043 |
| CLI §9 idempotency, single use | CLI-055, CLI-058, CLI-060, CLI-062 |
| CLI §10 sensitive data, audit | SEC-021, CLI-058 |
| CLI §11 reference | CLI-020, CLI-026, CLI-031 to CLI-033 |
| CLI §11 context, doctor, changelog | CLI-035, CLI-036, CLI-037 |
| CLI §11 and §13 release readiness | CLI-027, VER-005 |
| CLI §12 command design | CLI-021 to CLI-024, CLI-028, CLI-029, CLI-048 |
| CLI §13 functional contract coverage | VER-001 to VER-004, VER-008 |
| CLI §14 versioning, Skill version negotiation | CLI-080, CLI-081, CLI-036, SKL-009 |
| CLI §14 self-update; update notices | CLI-070; CLI-047, CLI-071 |
| CLI §15 batch operations | CLI-063 |
| CLI §16.1 credential lifecycle | CLI-035, CLI-036, CLI-038, SEC-040 |
| CLI §16.2 async jobs; §16.3 human in the loop | CLI-064; CLI-065 |
| SEC §1 risk tiers | SEC-001, CLI-025 |
| SEC §2 untrusted content | SEC-002, CLI-046 |
| SEC §3 least privilege | SEC-010 to SEC-013, CLI-056 |
| SEC §4 credentials at rest; §5 supply chain | SEC-040; SEC-050, CLI-070 |
| SEC §7, CLI §17 and SKILL §10 checklists | SEC-060, the `Check:` lines, AGENT.md self-check |
| SKILL §1, §4, §8 division of labour, disclosure, content | SKL-002, SKL-003 |
| SKILL §2 frontmatter, §3 naming | SKL-001 |
| SKILL §6 front-door conventions | SKL-002, SKL-004 to SKL-009 |
| SKILL §7 several Skills; §9 evaluation | SKL-010; SKL-011, VER-006 |
| REPO-SPEC §1 to §7 | REPO-001 to REPO-075 |

## 14. Checklist

- [ ] `.agent/SPEC_VERSION` is `v2.0.0`; `node scripts/check-spec.js` passes
- [ ] The tree is built per App; `main` calls `Execute`; no own envelope, exit table or format handling
- [ ] Every leaf registered with effects, output schema, examples, maturity and evidence
- [ ] Every write goes through `App.Write` or `App.WriteBatch`; Apply errors classified; the HTTP client never retries writes
- [ ] Old confirm code, FCC guard, `release_readiness`, `contract_gen` and hand-written `reference` deleted
- [ ] Conformance test passes with a sample for every write command; coverage enumerated from the registry
- [ ] Release notes and README explain scope files; the Skill never writes one
- [ ] Skill: standard frontmatter, ≤ 150 lines, command map, one write recipe, risk-scaled checkpoints, `test-prompts.json`
- [ ] `update` is aicli's built-in: `App.Update` set, the tool's own update code deleted; release archives carry `skills/` beside the binary
- [ ] Settings in `App.Settings`, profiles in `settings.json` with no secret in it, the 1.x `config.json` values moved there; `auth login`, `auth status`, `auth logout` where the tool signs in
- [ ] Flags renamed to the shared vocabulary, old names deprecated; no `--verbose` or `--debug`; HTTP timeouts, read-only retries, verified TLS; no telemetry
- [ ] README has Configuration and Uninstall sections
- [ ] Output: `schema_version` 2.0, snake_case, `meta.untrusted`, list completeness, `ref` on every resource
- [ ] CI and release run tests with conformance, audits and both guards (REPO-070, REPO-071)
- [ ] CHANGELOG lists every break; the tool's MAJOR version is bumped
