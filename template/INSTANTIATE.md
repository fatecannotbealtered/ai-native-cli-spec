# Instantiating a tool from this template

**中文版 → [INSTANTIATE_zh.md](INSTANTIATE_zh.md)**

A new tool is the language-agnostic `common/` layer, one language overlay, and the shared `aicli` library, which `scripts/sync-spec.js` vendors at a pinned spec tag. The library gives you the envelope, errors, registry, `--help`, `reference`, the write gate, scope files, the operation journal, the built-in commands and the conformance suite; you write the commands, the Skill and the tests.

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

## Placeholder dictionary

Use these literal tokens; never bake real values into the template.

| Token | Meaning | Where |
|---|---|---|
| `{{TOOL_NAME}}` | kebab-case binary, repo and Skill name, e.g. `notes-cli` | everywhere |
| `{{ENV_PREFIX}}` | UPPER_SNAKE env prefix, e.g. `NOTES_CLI`; also `App.EnvPrefix` | docs, Skill, test prompts |
| `{{DESCRIPTION}}` | one sentence on what the tool does, third person | package.json, README, Skill |
| `{{MODULE}}` / `{{CMD_PATH}}` | Go module path / main package, e.g. `github.com/OWNER/notes-cli`, `./cmd/notes-cli` | Go overlay |
| `{{TOOL_PKG}}` | Python import package, e.g. `notes_cli` | Python overlay |
| `{{NPM_PKG}}` / `{{REPO_SLUG}}` | npm package incl. scope / GitHub `OWNER/repo` | package.json, docs, `UpdateConfig` (step 5) |
| `{{SKILLS_CLI_VERSION}}` | the version of the `skills` installer the Agent Install block pins, the one you ran the block with (`npm view skills version` shows the latest); never `latest` | README |
| `{{VERSION}}` | first release, e.g. `0.1.0`; afterwards derived from package.json | package.json, CHANGELOG, Skill |
| `{{COPYRIGHT_YEAR}}` / `{{COPYRIGHT_HOLDER}}` / `{{CONTACT_EMAIL}}` | license and disclosure contact | LICENSE, docs |
| `{{RISK_TIER}}` / `{{RISK_TIER_DESC}}` | `T0`, `T1` or `T2` (SEC-001) and why | README, SECURITY.md |
| `{{THIRD_PARTY}}` | the product or service the tool drives, e.g. GitLab | NOTICE, Skill |
| `{{PRIMARY_TRIGGER}}` / `{{SECONDARY_TRIGGER}}` / `{{OUT_OF_SCOPE_CASE}}` | what users ask for, in their words; what the tool is not for | Skill |
| `{{REF_EXAMPLE}}` / `{{FILTER_EXAMPLE}}` / `{{DEFAULT_FIELDS}}` / `{{UNTRUSTED_EXAMPLES}}` | a `ref` (`ops/n-42`); a filtered list command; the default item fields; which fields hold other people's text | Skill |
| `{{PLAYBOOK_1}}` / `{{PLAYBOOK_2}}` | frequent multi-step tasks, one line each with real commands | Skill |
| `{{COMMAND_MAP}}` | replaced by the map test with `app.CommandMap()` (step 8); never filled by hand | README (Capabilities), Skill |
| `{{PROMPT_*}}` / `{{INJECTED_ACTION}}` | one concrete user request per VER-006 scenario; the action the injected text asks for | test-prompts.json |

## Steps

The commands use a Go tool named `notes-cli`; Python differences are in step 9. Run them from the new repository's root.

### 1. Copy common and one overlay

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

Copy only the overlay files named above: the overlay's `aicli/` and `example/` directories are not copied (sync-spec vendors the library; the example is a reference). Delete `NOTICE.md` and `NOTICE_zh.md` if the tool wraps no third-party product.

### 2. Fill the placeholders

Replace the tokens in the dictionary across the copied files, starting with the name and the env prefix; leave the Skill tokens for step 8 (GNU sed; on macOS use `gsed`).

```bash
grep -rlE '\{\{(TOOL_NAME|ENV_PREFIX|MODULE|CMD_PATH|NPM_PKG|REPO_SLUG)\}\}' . --exclude-dir=node_modules --exclude-dir=.git |
  xargs sed -i 's#{{TOOL_NAME}}#notes-cli#g; s#{{ENV_PREFIX}}#NOTES_CLI#g;
    s#{{MODULE}}#github.com/OWNER/notes-cli#g; s#{{CMD_PATH}}#./cmd/notes-cli#g;
    s#{{NPM_PKG}}#@OWNER/notes-cli#g; s#{{REPO_SLUG}}#OWNER/notes-cli#g'
```

### 3. Create the build manifest

- Go: `go mod init github.com/OWNER/notes-cli`. CI reads the Go version from `go.mod`.
- Python: `pyproject.toml` with `[project] name` (sync-spec uses it to find the package) and `dynamic = ["version"]`, with `[tool.setuptools.dynamic] version = {attr = "notes_cli.__version__"}`: the version lives in `notes_cli/__init__.py`, which `npm version` rewrites, and a static `version` would drift unseen by `check-version.js`. Add a `[dev]` extra (pytest, ruff, pip-audit, pyinstaller), `click` and `sigstore` in the dependencies (aicli's `update` verifies releases with `sigstore`), `requirements.txt` for `pip-audit`, `build.py` for the release binary, and the package directory with its `__init__.py`.
- npm wrapper: create the lockfile once, against the public registry, and check it before you commit it, so no private mirror ends up in a public file (REPO-031). Later version bumps rewrite it field by field (`npm version`).

  ```bash
  npm install --package-lock-only --ignore-scripts --registry=https://registry.npmjs.org/
  grep '"resolved"' package-lock.json | grep -v '"https://registry.npmjs.org/' && echo "fix the URLs above" || echo "lockfile OK"
  ```

### 4. Pin the spec and vendor the library

```bash
echo v2.0.1 > .agent/SPEC_VERSION
node scripts/sync-spec.js        # or: --from "$SPEC" to copy from the local checkout
node scripts/check-spec.js
# Go: run the "go get ... && go mod tidy" line sync-spec printed (every module the library requires, indirect ones included, at the library's versions)
```

`sync-spec.js` writes `.agent/`, `contract/`, the sync tooling and the library (`internal/aicli/`, or `<pkg>/_aicli/` for Python), checking every library file against the spec's `MANIFEST.json`. Never edit vendored files: `check-spec.js` fails CI when they differ from the manifest or from the pinned tag. To upgrade later, change the pin and run `sync-spec.js` again; it re-runs itself when the sync tooling changed.

### 5. Wire main to aicli.App

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

`Execute` adds `reference`, `context`, `doctor`, `changelog` and `operation`, and `update` when `Update` is set; it writes exactly one envelope and returns the exit code. `Update` turns on aicli's built-in `update`: `Repo` is the `{{REPO_SLUG}}` value and `NPMPackage` the `{{NPM_PKG}}` value from step 2; set `GoPackage`, `PyPIPackage` or `BrewFormula` for other channels, and `Workflow` when the release workflow is not `release.yml`. Never write an `update` command of your own (CLI-070, CLI-071). `Settings` declares the tool's settings, which `app.Setting(cmd, name)` resolves from the flag, `NOTES_CLI_<SETTING>`, the active profile in `settings.json` and the default; with settings declared, aicli also adds `--profile` (CLI-100, CLI-101). Optional hooks: `Identity` (account and host, bound into tokens), `DoctorChecks`, `ContextExtra`, `SkillDirs` (where `doctor`'s `version` check finds the installed Skill, and where `update` syncs it; the default is the standard agent Skill directories), `ShortFlags`, `FlagAliases`, `FlagSynonyms` and `VerbSynonyms` (the guesses agents make, CLI-022; root shortcuts come from `app.AddShortcut`, one per noun nested under exactly one parent), `ExtCodes` (extension codes, CLI-018).

### 6. Register commands and write the first read command

Every leaf gets `app.Register` with its effects and output schema; groups get `app.Group`. Map upstream failures with `aicli.FromHTTP`. A list's output schema has the `page` shape; a single object's has the `object` shape. `Maturity` never exceeds `Evidence`: `experimental` cites the tests that run the command against a fake upstream (`simulated:`), `verified` adds a recorded live run (`live:`, VER-005).

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

### 7. Write the first write command

A write declares a non-`observe` effect and goes through `app.Write`: the library runs the dry-run, the refusal, the token, the scope check, the journal and the receipt. You supply a read-only `Preview` and the `Apply`, and the target's canonical key: resolve what the agent typed (a ref, a web URL, a name) to one spelling per object, and bind that form with `aicli.SetResolved`, so `note update <url>` and `note update <ref>` are the same request (CLI-054) and the same scope target (SEC-012). When a ref carries several inputs (a project and an iid), bind each one's own part under its own flag. `cmd/notes.go` now also imports `context`.

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

An error from `Apply` means the outcome is unknown (`E_OUTCOME_UNKNOWN`) unless you wrap it: `aicli.NotDispatched(err)` when nothing was sent, `aicli.Rejected(err)` when the upstream refused and changed nothing. A dangerous command sets `Dangerous: true` (deleting, sending, sharing; `deliver` and `configure` effects are dangerous by default) and its examples carry `--dangerous`. Never build your own token, key or journal (CLI-050). A lookup before `app.Write`, like `store.Resolve` above, runs again at every confirm ahead of the gate's own checks; when it can fail once the write is done (the ref of a note that `note delete` removed), set `WriteSpec.ResolveTarget` instead, which the gate calls after the token checks, so a repeated confirm still answers `token_used` (CLI-057).

### 8. Add the conformance test and the Skill

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
	if code == 0 && fullRun() && os.Getenv("NOTES_CLI_CHECK_FULL_COVERAGE") == "1" {
		tb := &mainTB{}
		h := conformanceHarness(newFakeStore())
		h.CoverageFile = f.Name()
		aicli.CheckCoverage(tb, h)
		if tb.failed {
			code = 1
		}
	} else if code == 0 && fullRun() {
		fmt.Fprintln(os.Stderr, "R06 not run; full coverage has not been checked")
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

The suite runs the gate checks on every write command (a write without a sample fails it), validates every envelope, and checks `reference`, the tree shape, usage errors, `context`, `doctor` and `operation` (VER-001, VER-002). Run the example's R06 separately with `NOTES_CLI_CHECK_FULL_COVERAGE=1`; it fails with any untested commands. Record its result or explicitly record that it did not run. It is not a package-support prerequisite (VER-003), and the separate switch does not disable any write-gate check. A nil `Scope` grants every effect for the positive checks; a tool with `deliver`, `artifact` or `persist` commands sets `Scope` with its sample destinations and roots (SEC-012, VER-001). Add behaviour tests as commands mature and isolate their state (VER-004, REPO-073).

Then the Skill:

```bash
mkdir -p skills/notes-cli
mv skills/SKILL.md.tmpl skills/notes-cli/SKILL.md
mv skills/test-prompts.json.tmpl skills/notes-cli/test-prompts.json
```

Fill the Skill tokens from what users actually ask (SKILL-SPEC). The Skill and the README's Capabilities section carry the same command map between `<!-- aicli:command-map:start -->` and `<!-- aicli:command-map:end -->`: the text between the markers is exactly `app.CommandMap()`, and the test below keeps every copy current. Do not paste the map over `{{COMMAND_MAP}}` by hand (the renderer's output already ends with a newline, so a pasted copy leaves a blank line the test rejects): run the test once with `UPDATE_SKILL_MAP=1`, which writes each block.

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

Write one concrete prompt per scenario in `test-prompts.json` (VER-006). Each entry keeps exactly `id`, `scenario`, `prompt`, `expected` and an optional `grader_notes` (SKL-011); the template puts each trial's setup and failure conditions in `grader_notes`. Delete the `update` entry if the tool does not set `Update`.

### 9. Python differences

The library lands in `<pkg>/_aicli/` and is imported relatively (`from ._aicli import App, Meta, WriteSpec`). The API mirrors Go in snake_case (`app.register`, `app.write`, `set_resolved`, `run_conformance`, `check_coverage`, `app.command_map()`) on a click tree; `UpdateConfig` and `Setting` take the same fields in snake_case (`App(update=UpdateConfig(repo="OWNER/notes-cli", npm_package="@OWNER/notes-cli"))`). `template/python/example/` is the complete-coverage reference. Tests live in `tests/`: always run `run_conformance`, and record command coverage for the whole session. If complete coverage is not yet available, isolate `check_coverage` as an explicitly skipped test unless `NOTES_CLI_CHECK_FULL_COVERAGE=1`; run it separately on a full test run to retain its real failure and missing-command list. Never skip the conformance suite or conceal other failures. `ruff.toml` already excludes `_aicli`.

### 10. CI and the final check

The default release matrix retains Windows/amd64, macOS/amd64 and arm64, and Linux/amd64 and arm64. The Windows ARM64 npm alias is a Windows amd64 binary using x64 emulation, not a native ARM64 build; test that path before claiming it. The specification minimum is one architecture per OS and does not require every new product to support all five. Replace `scripts/smoke-product.js` with a real representative key-operation check on an authorized disposable target: the placeholder fails deliberately. For each target the workflow installs and starts the actual archive, runs the check and creates one versioned `smoke-<os>-<arch>.json` record. The release checker consumes those records with an explicit expected source SHA and verifies the actual archive and executable identity, architecture and digest. Provide minimum OS, CPU, external applications and system libraries in the smoke result, and state direct-binary dependencies separately from the npm wrapper's Node.js prerequisite. Add architectures with matching artifacts, npm declarations and real evidence; never silently remove previously supported combinations. If the original application is host-limited, test and document the remote host and protocol rather than claiming native availability.

CI retains the conformance suite, dependency audits, version and spec checks. The release repeats the security and merge checks (VER-008); record incomplete coverage or evaluations honestly. Measure launcher overhead as in REPO-041 and document the native path where it exceeds the budget.

```bash
grep -rnE '\{\{[A-Z_0-9]+\}\}' . --exclude-dir=node_modules --exclude-dir=.git || echo "all placeholders filled"
node scripts/check-spec.js && node scripts/check-version.js && go vet ./... && go test ./...
```

Then walk `docs/OPEN_SOURCE_CHECKLIST.md` before the first public push.

## The lightness contract

- The `.agent/` specs, the contract and the schemas are language-neutral and never fork per language.
- Shared behaviour lives in the library, vendored by copy and drift-guarded; a tool never re-implements the envelope, the registry or the write gate.
- Adding a language means adding `template/<lang>/`: the CI and packaging overlay, an `aicli` library with the same behaviour, and an example that passes its conformance suite. Nothing in `common/` changes.

## License scope

The MIT seed in `common/LICENSE` is only the starting license for original product code; it does not relicense the Apache-2.0 specification or SDKs. Retain the SDK LICENSE files and copyright/SPDX headers, distribute their license texts, and retain the CC-BY-4.0 attribution in both Code of Conduct files. If you choose another product license, update its README, package and Skill license metadata consistently.
