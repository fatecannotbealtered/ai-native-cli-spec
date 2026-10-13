# Repo spec for AI-native CLI tools

**中文版 → [REPO-SPEC_zh.md](REPO-SPEC_zh.md)**

This is the repository standard for tools built on ai-native-cli-spec 2.0: which files a tool repo has and where, how versions, releases and distribution work, how the specs and the aicli library are vendored, and what CI runs. The goal is that every tool repo looks the same, so a person or an agent finds the same thing in the same place in any of them.

This file lives at the root of the spec repo and is not vendored into tools; `.agent/AGENT.md` links to it. The vendored companions are:

- `.agent/AGENT.md`: the playbook and entry point.
- `.agent/CLI-SPEC.md`: the machine contract (`CLI-xxx`).
- `.agent/SEC-SPEC.md`: security (`SEC-xxx`).
- `.agent/SKILL-SPEC.md`: Skills (`SKL-xxx`).
- `.agent/VERIFY-SPEC.md`: verification and agent eval (`VER-xxx`).

The spec repository also holds `tools/`: the VER-006 agent-eval harness (`tools/agent-eval/`) and the VER-007 gate-audit workflows (`tools/gate-audit/`). Tools never vendor them; they are how the evidence those rules ask for is produced.

Rules here are `REPO-xxx`. MUST, MUST NOT, SHOULD and MAY have their RFC 2119 meaning; an entry marked † applies only when its condition holds. IDs are stable and never renumbered.

## 1. Files

### REPO-001 Required files

A tool repo MUST contain the files marked MUST and SHOULD contain those marked SHOULD. "Vendored" files are copied from the spec at the pinned tag (REPO-061) and never edited (REPO-063).

| File | Level | Role |
|---|---|---|
| `README.md` | MUST | front door, skeleton in REPO-010 |
| `README_zh.md` | SHOULD | translation (REPO-020) |
| `LICENSE` | MUST | default MIT |
| `CHANGELOG.md` | MUST | Keep a Changelog; source of the `changelog` command (REPO-032) |
| `CONTRIBUTING.md`, `SECURITY.md` | MUST | contribution flow; disclosure channel, supported versions, risk tier (SEC-001) |
| `CODE_OF_CONDUCT.md` | SHOULD | Contributor Covenant |
| `NOTICE.md` | MUST† | trademark notice (†wraps a third-party product) |
| `docs/COMPATIBILITY.md` | MUST† | verified upstream versions (†integrates an external system) |
| `docs/E2E.md` | SHOULD† | live test environment and recorded runs, the evidence behind `verified` maturity (VER-005) (†calls external APIs) |
| `docs/OPEN_SOURCE_CHECKLIST.md` | SHOULD | checklist before the first public push |
| `.gitignore`, `.gitattributes` | MUST | build output, caches, IDE files; line endings |
| `.github/workflows/ci.yml`, `release.yml` | MUST | merge gate (REPO-070), release gate (REPO-071) |
| `.github/ISSUE_TEMPLATE/`, `PULL_REQUEST_TEMPLATE.md` | SHOULD | issue forms, PR self-check (REPO-074) |
| `AGENTS.md` | MUST | entry hook for every agent; points to `.agent/AGENT.md` |
| `.agent/{AGENT,CLI-SPEC,SEC-SPEC,SKILL-SPEC,VERIFY-SPEC}.md` (+ `_zh`) | MUST, vendored | the five specs |
| `.agent/SPEC_VERSION` | MUST | the spec pin (REPO-060) |
| `contract/contract.json`, `contract/schemas/*.schema.json` | MUST, vendored | machine contract and JSON Schemas |
| `contract/contract-ext.json` | MUST† | tool-specific error codes (†the tool has any) (REPO-064) |
| Go: `internal/aicli/*.go`, `internal/aicli/LICENSE`, `internal/aicli/MANIFEST.json` | MUST, vendored | the aicli library, its licence and its file hashes |
| Python: `<pkg>/_aicli/**`, `<pkg>/_aicli/LICENSE`, `<pkg>/_aicli/MANIFEST.json` | MUST, vendored | the aicli library, its licence and its file hashes |
| `scripts/{spec-files,sync-spec,check-spec}.js` | MUST, vendored | sync and drift guard |
| `scripts/{version-files,sync-version,check-version}.js`, `.npmrc` | MUST | one version source (REPO-031) |
| `package.json`, `scripts/{run,prepare-npm-platform-packages}.js` | MUST† | npm wrapper (†distributed through npm, REPO-040) |
| a conformance test, e.g. `cmd/conformance_test.go`, `tests/test_conformance.py` | MUST | runs the suite with a sample for every write command (VER-001); R06 runs after the full test run (Go: `TestMain` in `cmd/main_test.go`, VER-003) |
| `skills/<name>/SKILL.md`, `skills/<name>/test-prompts.json` | MUST | the Skill and its eval prompts (SKL-011) |

Retired in 2.0; a 2.0 tool has none of them: the FCC source-scan guards `cmd/fcc_guard_test.go` and `tests/test_fcc_guard.py` (replaced by VER-003); the per-tool contract modules `internal/contract/contract_gen.go` and `<pkg>/contract_gen.py` (the library carries its generated contract); the tool's own confirm-token, key and consumed-token code (CLI-050); the tool's own `update` code, which aicli's built-in replaces (CLI-070); `release_readiness` in code and docs (CLI-027).

Why: one manifest lets anyone find the same file in every repo and lets the review Skill grade a repo mechanically.
Check: `review-ai-native-cli` static review; `check-spec.js` fails when a vendored file is missing.

## 2. README

### REPO-010 README skeleton

A tool's README MUST have these sections in this order:

1. **Title**, language switch, badges: CI, a language quality badge where one applies, npm version, licence. No other badges.
2. **One line** that positions the tool as agent-facing.
3. **Agent Install**: one copy-paste block that installs the CLI, installs the Skill of the same version from the installed package with a pinned installer (`npx skills@<version> add "$(npm root -g)/<pkg>" -y -g`, SKL-009) and sets credential placeholders. It does not run `context`, `doctor` or `reference` first (CLI-034).
4. **What It Does**: one paragraph with the risk tier (SEC-001), the effects the tool can have (CLI-025) and any third-party disclaimer.
5. **Authorization**: writes are refused until a person grants a scope file (SEC-010); the file's path (`<PREFIX>_SCOPE_FILE`, else `<state_dir>/scope.json`), a minimal grant, and the protection statement of SEC-013.
6. **Capabilities**: the command map, one line per noun with its verbs: `CommandMap()` output between the same `aicli:command-map` markers the Skill uses, checked by the same test (SKL-002).
7. **Agent Workflow**: run task commands directly; discover with `--help`, `reference --index` and `reference --command <path>`; narrow output with `--fields`; write with `--dry-run`, then the same command without it plus `--confirm <token>` (a bare call returns the same preview and token), plus `--dangerous` where required; after a lost reply, `operation show --id <operation_id>`; after `update`, `changelog --since <previous_version>`.
8. **Machine Contract**: the pinned spec version, `schema_version` `"2.0"`, the envelope, error details (`dispatch_state`, `next_action`), `meta.untrusted`, and a link to `.agent/CLI-SPEC.md`.
9. **Configuration**: the settings and their environment variables (credentials, `<PREFIX>_STATE_DIR`, `<PREFIX>_SCOPE_FILE`, `<PREFIX>_SETTINGS_FILE`, `<PREFIX>_PROFILE`, `<PREFIX>_CA_FILE`), the settings file with its profiles (CLI-100, CLI-101), and credential storage (SEC-040).
10. **Uninstall**: how to remove everything the tool installs or leaves behind (REPO-011).
11. **Project Structure**: the top-level tree (REPO-050).
12. **Development**: build, test, conformance and lint commands.
13. **Links**: `AGENTS.md`, the Skill, `.agent/CLI-SPEC.md`, `SECURITY.md`, `docs/COMPATIBILITY.md`, `docs/E2E.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `NOTICE.md`, `LICENSE`.

Why: the install block is what an agent runs first; in the 2026-10 gitlab-cli eval, a Skill-mandated `context`/`doctor`/`reference` bootstrap cost 1.6 calls per task with no accuracy gain.
Check: review: headings present and in this order; the install block runs in a clean shell.

### REPO-011 README content

- Install blocks MUST run as pasted, with placeholders for secrets and never real credentials.
- The README is a map. Flags, schemas and error tables live in `reference` and `--help`; the README MUST NOT copy them.
- Examples use runnable forms, ISO 8601 UTC times and placeholder IDs (`<merge-request-ref>`).
- The README tells the user how to grant a scope. It MUST NOT tell an agent to create or edit the scope file (SKL-006).
- The Uninstall section MUST cover everything the tool leaves behind: the package or binary; the state directory (`~/.<tool>`, or `<PREFIX>_STATE_DIR`) with its key, journal, scope file and settings file; the credentials in the OS keyring (`<tool> auth logout` for each profile, or the keyring's own manager); a settings or scope file kept elsewhere (`<PREFIX>_SETTINGS_FILE`, `<PREFIX>_SCOPE_FILE`); and the installed Skill directories (`~/.agents/skills/<name>`, `~/.claude/skills/<name>`).

Why: copied flag lists go stale; a README that tells agents to write their own scope undoes SEC-010; an uninstall that leaves a credential in the keyring or a Skill in the agent's directories leaves access and instructions behind for a tool that is gone.
Check: review.

## 3. Languages

### REPO-020 English source, Chinese translation

English `X.md` is the source. A Chinese `X_zh.md` is translated from the final English text, and the two link to each other at the top. README, `AGENTS.md` and the vendored `.agent` specs are bilingual; governance docs (CONTRIBUTING, SECURITY, CODE_OF_CONDUCT) MAY be. A release that changes a bilingual English file MUST ship the updated translation.

Why: one source cannot disagree with itself; translating the final text avoids translating churn.
Check: both files exist and link to each other; CI MAY compare their heading structure.

## 4. Versions and releases

### REPO-030 Semantic versioning

Tool versions follow SemVer. A change that breaks what agents rely on (envelope, keys, error codes, command paths or parameters, the write flow) bumps MAJOR; moving a tool from spec 1.x to 2.0 is such a change. The tool version, the spec pin (`.agent/SPEC_VERSION`) and `schema_version` are three separate numbers (CLI-080).

Why: Skills and `doctor` key their expectations on the tool version (`metadata.min_version`).
Check: breaking entries in `CHANGELOG.md` sit under a new MAJOR version.

### REPO-031 One version source

`package.json` `version` is the only hand-edited version. `npm version <patch|minor|major|x.y.z>` runs `scripts/sync-version.js`, which rewrites every location listed in `scripts/version-files.js`: the `optionalDependencies` pins, the version fields of `package-lock.json` including platform subentries, each Skill's `metadata.version` and `metadata.min_version` (SKL-001), the Python `__version__`, and the roll of `## [Unreleased]` into a dated heading.

- `.npmrc` sets `git-tag-version=false`: the bump edits files only; a person tags after CI is green.
- `scripts/check-version.js` runs in `ci.yml` and `release.yml` and fails on any disagreement.
- Tests read the version at run time and never contain it as a literal.
- A version bump rewrites the lockfile field by field, never with `npm install --package-lock-only`, which writes the developer's registry mirror into a public file. The first lockfile is created once with `--registry=https://registry.npmjs.org/`, and every `resolved` URL in it is checked before it is committed (`template/INSTANTIATE.md`, step 3).

Why: a hand-copied version drifts; the guard makes drift unmergeable and unpublishable.
Check: `node scripts/check-version.js` in both workflows.

### REPO-032 CHANGELOG is the only change source

`CHANGELOG.md` follows Keep a Changelog: `## [Unreleased]` on top, then `## [x.y.z] - YYYY-MM-DD` sections with `Added`, `Changed`, `Fixed`, `Deprecated`, `Removed`, `Security`. Everything else is derived and not committed: the release workflow slices the version's section into the release body, and the build embeds the file for the `changelog` command (CLI-037). Go embeds it in `changelog.go` as `ChangelogMarkdown` and passes it to `App.Changelog`; Python does the same in `changelog.py`. There is no `release-notes.md` in the repo.

Why: agents read the same text after `update` that people read on the release page.
Check: no committed release-notes file; a test runs `changelog --since <previous>` against the embedded file.

### REPO-033 Tags and toolchain

A release is a git tag `vX.Y.Z` pushed after CI is green on the tagged commit; `release.yml` builds, signs and publishes from the tag only. Go tools pin one Go version in `go.mod` (`go 1.XX`, no `toolchain` line) and workflows read it with `go-version-file: go.mod`.

Why: traceable builds from tagged source (SEC-050); one toolchain source that `govulncheck` findings can move.
Check: `release.yml` triggers on `v*` tags; no Go version is hard-coded in a workflow.

## 5. Distribution

### REPO-040 npm wrapper

Tools SHOULD distribute through the npm wrapper, so Go and Python tools install the same way. A tool that does MUST follow these rules:

- `package.json` is the single source for the package scope and name, the `bin` name and the version; scripts and CI read them from it.
- Binaries ship as per-platform packages in `optionalDependencies`, built by CI and packed at release by `scripts/prepare-npm-platform-packages.js`. Installing never downloads a binary from anywhere else, and no install script fetches code (SEC-050).
- `scripts/run.js` resolves the platform package and runs its binary with `execFileSync`, passing argv and stdio through; a missing platform package prints a reinstall command.
- Binaries (`bin/`, `dist/`, `*.exe`) are build output and are never committed.

Why: one install path across the fleet, with registry integrity and provenance on it.
Check: `package.json` `bin` and `optionalDependencies`; `.gitignore` covers build output.

### REPO-041 Launcher time budget

The command an agent runs from PATH SHOULD be the native binary, or a launcher that adds at most 100 ms to it (CLI-090). CI SHOULD measure this on every release platform: the median of at least 10 runs of `<tool> --version` through the installed command, minus the same median for the binary run directly; the job fails above 100 ms. Where the npm launcher misses the budget, the README's Agent Install SHOULD put the native binary on PATH for agents, for example the signed standalone binary, or an install step that links the binary from the already-installed platform package without network access.

Why: every agent call is a round trip; on the fleet's Windows test machine a call through the npm launcher took 1.1–2.3 s against 0.4–0.7 s for the binary alone.
Check: the launcher-budget job in CI; the README names the native path where the launcher misses.

### REPO-042 Signed release material

Release archives ship `checksums.txt`, signed in the tagged release workflow with Sigstore keyless signing (`cosign sign-blob --new-bundle-format`), with the bundle (`checksums.txt.sigstore.json`) published beside it; npm packages are published with provenance. The signing is unchanged from 1.x. On the binary path, aicli's `update` verifies the signature in-process against the exact workflow named in `UpdateConfig.Workflow` (default `release.yml`) and fails closed with `E_INTEGRITY` (CLI-070, SEC-050).

Why: a checksum proves the bytes match a file; only the signature proves the file came from the publisher.
Check: `release.yml` signs before publishing; aicli's `update` tests cover a missing and a bad signature.

### REPO-043 Skills ship with the tool

Every distribution channel MUST carry the Skills of its own version: the npm package lists `skills/` in `files`; a release archive, named `<tool>-<version>-<os>-<arch>.tar.gz` (`.zip` on Windows), holds the binary and `skills/` at its root. Install and `update` take the Skill from there, never from a branch fetched over the network: aicli's `update` replaces `<exe dir>/skills` with the new archive's `skills/`, then copies the Skills from there, or from the npm package, into the Skill directories where they are installed (SKL-009, CLI-070).

Why: 1.x prescribed `npx skills add <repo> -y -g` on every update, which installs whatever the default branch holds, not the Skill written for the installed binary.
Check: `npm pack --dry-run` lists `skills/`; a release archive lists the binary and `skills/` at its root; aicli's `update` tests show the Skill sync makes no network request.

## 6. Layout

### REPO-050 Directory layout

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

- Source, tests, scripts and docs each have their own directory; tests mirror the source layout.
- The library exists only at its vendored path. A tool MUST NOT keep a second copy (for example a top-level `aicli/` left from copying the overlay) or import the library from anywhere else.
- Runtime state (confirm key, operation journal, scope file, notice cache) lives in the state directory (SEC-030), never in the repo.

Why: the same places in every repo, and exactly one library copy for the drift guard to check.
Check: review; `check-spec.js` checks the vendored path only, so a second copy is a review finding.

### REPO-051 Template model

The spec repo's `template/` has a language-agnostic `common/` and one overlay per language (`go/`, `python/`). A tool copies `common/` and exactly one overlay, appends the overlay's `.gitignore` to the common one, and vendors the library with `scripts/sync-spec.js` (`template/INSTANTIATE.md`). The overlay's `aicli/` and `example/` directories are sources inside the spec repo, not tool files. Behavioural specs never fork per language; an overlay carries only build, CI and packaging files. Adding a language means adding an overlay plus a library port that passes the same conformance suite.

Why: the contract an agent sees must not depend on the language a tool is written in.
Check: `.agent/*` is byte-identical in a Go and a Python tool at the same pin.

## 7. Single-sourcing the spec and the library

### REPO-060 Pin

`.agent/SPEC_VERSION` MUST hold the spec tag the tool follows, for example `v2.0.0`. One pin covers the specs, the contract and schemas, the library and the sync tooling; they are never pinned separately.

Why: these files are tested together in the spec repo; mixing versions builds a combination nobody tested.
Check: `check-spec.js` reads the pin and fails without it.

### REPO-061 Sync

`scripts/sync-spec.js` copies every file listed in `scripts/spec-files.js` from the spec at the pinned tag, byte for byte:

| From the spec repo | To the tool |
|---|---|
| `template/common/.agent/*.md` | `.agent/` |
| `template/common/contract/contract.json`, `contract/schemas/*.schema.json` | `contract/` |
| `template/go/aicli/*.go` except `*_test.go`, plus `LICENSE` and `MANIFEST.json` | `internal/aicli/` (Go) |
| the `aicli` package files from `template/python/aicli/` except tests, plus `LICENSE` and `MANIFEST.json` | `<pkg>/_aicli/` (Python; `<pkg>` is the detected package directory) |
| `template/common/scripts/{spec-files,sync-spec,check-spec}.js` | `scripts/` |

Tools no longer generate contract code: `scripts/gen-contract.js` runs in the spec repo and writes `contract_gen.go` and `contract_gen.py` into the libraries, so the vendored library carries its own constants. The Go library is one flat package and the Python library uses relative imports only, so copying needs no import rewriting.

Why: a copy runs exactly the code the spec repo tested, with no package registry in between.
Check: running `node scripts/sync-spec.js` on a conforming tool leaves `git diff` empty.

### REPO-062 Manifest and drift guard

Each library ships a `MANIFEST.json` (`{spec_version, files: {path: sha256}}`), written by `scripts/build-manifest.js` in the spec repo. Hashes are taken over content with CRLF normalised to LF, so a Windows checkout with `core.autocrlf` hashes the same as Linux. `scripts/check-spec.js` MUST run in `ci.yml` and `release.yml` and fail closed:

- offline and strict: every library file matches its manifest hash, no listed file is missing, no unlisted file sits in the library directory, the manifest's `spec_version` matches the pin and `contract.json`, and every vendored spec file is present;
- online: every vendored file equals the spec at the pinned tag and no stale file remains. A failed download degrades this layer to a warning, because the offline layer still holds; with `--from <checkout>` the comparison is strict.

Why: an edited copy of the gate is a per-tool implementation again, and the fleet audit found per-tool gates broken in every one of the 10 tools that had one.
Check: the `check-spec.js` step in both workflows.

### REPO-063 Vendored files are read-only

A tool MUST NOT edit, add or delete files under its vendored paths: `.agent/`, `contract/` except `contract-ext.json`, `internal/aicli/` or `<pkg>/_aicli/`, and the vendored scripts. Every library file starts with a header saying so. To change one, change it in the spec repo, release a tag, bump `.agent/SPEC_VERSION` in the tool and run `sync-spec.js`; the diff is mechanical and `check-spec.js` proves it. A tool that needs a library change opens an issue or pull request on the spec repo.

Why: one implementation, conformance-tested once, is the point of 2.0 (CLI-050).
Check: `check-spec.js`; review of any diff under the vendored paths that is not a sync.

### REPO-064 Tool-owned extensions

Tool-specific error codes live in `contract/contract-ext.json`, which the tool owns and the sync never touches, and are declared to the library (Go: `App.ExtCodes`). Each code follows CLI-018: an `E_*` name that is not a core code, a declared `exit` and `retryable`, and exit 9 only for human-action codes.

Why: tools need codes the core lacks (archery-cli's `E_2FA_REQUIRED`) without forking the core table.
Check: the extension rules in `contract.json` `error_codes.extension`.

## 8. Quality gate

### REPO-070 Merge gate

CI MUST run on every push and pull request, and a failed step blocks the merge:

- format and lint: Go `gofmt`, `go vet`, golangci-lint; Python ruff;
- tests, including the conformance suite with a sample for every write command (VER-001), validation of every envelope the tests produce (VER-002), and coverage enumerated from the registry (VER-003); Go Makefile targets are `test` (includes conformance), `conformance` and `lint`;
- a dependency audit for every ecosystem the tool ships in (SEC-050): Go `govulncheck ./...` and `npm audit`; Python `pip-audit` and `npm audit`;
- `node scripts/check-version.js` and `node scripts/check-spec.js`.

Why: 9 of 11 fleet Go repos audited only the npm wrapper, and four reachable Go standard-library CVEs sat unnoticed until the two repos that ran `govulncheck` went red (spec 1.6.0).
Check: the steps in `ci.yml`.

### REPO-071 Release gate equals merge gate

`release.yml` MUST run the same tests, audits and drift guard as REPO-070 before anything is signed or published (VER-008).

Why: if an advisory is published after the last green merge, the vulnerable dependency would otherwise ship in a signed release that nothing examined; in 2026-08 four Go standard-library CVEs were published days after the fleet's last green run, and a tag pushed then would have shipped them (spec 1.6.2, 1.6.3).
Check: `release.yml` runs the REPO-070 steps ahead of signing and publishing.

### REPO-072 Evidence before release

Before a release that changes commands, outputs or the Skill, a tool SHOULD run the agent eval (VER-006); it MUST before any command declares `stable`. Each command's maturity and evidence references live in the registry (VER-005), and `docs/E2E.md` records the live runs that `verified` cites.

Why: the eval found effects no review predicted; the 2026-10 gitlab-cli Skill without a command map made rare commands more expensive.
Check: every evidence reference resolves; `stable` commands cite an eval run.

### REPO-073 Tests never touch the user's state

Tests MUST point the state directory at a temporary directory (`App.StateDir`, or `<PREFIX>_STATE_DIR`), so no test, compiled test binary included (`*.test.exe` on Windows), reads or writes the user's confirm key, operation journal, scope file or notice cache.

Why: a test on real state can consume a real operation, or pass only because a real scope file grants it.
Check: the test suite passes with `HOME` (or `USERPROFILE`) set to a read-only directory.

### REPO-074 Formatter and pull request self-check

Each language has one formatter with committed config (Go `gofmt`, Python ruff, Prettier for JavaScript where used); nothing is aligned by hand. The PR template asks: tests and the conformance suite pass with a sample for every write command and R06 on; each changed command's maturity matches its evidence; new writes go through the gate with truthful effects; scope and authorization behaviour unchanged or `SECURITY.md` updated; nothing vendored edited; `CHANGELOG.md` updated; README and Skill command maps regenerated; translations updated.

Why: formatting noise hides real diffs; the self-check catches the updates reviewers forget.
Check: a format check in CI; the PR template.

### REPO-075 The spec repo's own gate

This repo's CI MUST run: the Go library and example tests (`go test ./...` in `template/go/aicli` and `template/go/example`), the Python library and example tests (pytest), `check-contract.py` (schemas valid, valid examples pass, invalid examples fail, receipt rules hold), manifest freshness (`build-manifest.js` output equals the committed `MANIFEST.json` files), generated-contract freshness (`gen-contract.js` output equals `contract_gen.go` and `contract_gen.py`), and the reference tools: `python -m py_compile` on every `.py` under `tools/`, and `go vet ./...` and `go test ./...` in `tools/agent-eval/harness/recorder`. Each spec release also re-runs the confirm-gate audit across the fleet (VER-007).

Why: tools trust a tag; the tag must have passed everything tools rely on.
Check: this repo's `.github/workflows/`.

## 9. New-project checklist

- [ ] REPO-001 files present; none of the retired files
- [ ] README follows REPO-010 and REPO-011, with Authorization, Configuration and Uninstall sections and no bootstrap
- [ ] `.agent/SPEC_VERSION` pinned; `sync-spec.js` run; `check-spec.js` green
- [ ] Library only at `internal/aicli/` or `<pkg>/_aicli/`, unedited
- [ ] Conformance test with a sample for every write command; coverage enumeration on (VER-003)
- [ ] `contract/contract-ext.json` for any tool-specific error code
- [ ] One version source; Skill `metadata.version` and `metadata.min_version` derived; `check-version.js` green
- [ ] `CHANGELOG.md` with `Unreleased`, embedded for `changelog`
- [ ] CI: format, lint, tests with conformance, audits per shipped ecosystem, version and spec guards; the same on the release path
- [ ] Launcher budget measured; native install path documented where the launcher misses it
- [ ] Release signs `checksums.txt`; npm publishes with provenance; `App.Update` names the repository and the packages
- [ ] Skills ship in every channel, beside the binary in release archives; `test-prompts.json` present
- [ ] Tests use a temporary state directory
- [ ] `SECURITY.md` states the risk tier; `NOTICE.md`, `docs/COMPATIBILITY.md`, `docs/E2E.md` where they apply
- [ ] Bilingual docs link to each other
- [ ] `docs/OPEN_SOURCE_CHECKLIST.md` walked before the first public push
