# AI-Native Open-Source Repo Spec


This document defines a unified repo standard for all your open-source projects: required docs, template files, directory layout, release conventions. The goal: ten projects look the same, and anyone (including an AI agent) entering any repo finds the same things in the same places.

Companion specs for consuming tool repositories live under `.agent/`, with
`AGENT.md` as the entry point. This repo skeleton standard stays here, at the
root of `ai-native-cli-spec`, so consuming repositories do not carry forked
copies.

- `AGENT.md` — entry / playbook: how to build & extend a tool here, navigates to the local specs and this shared repo standard.
- `CLI-SPEC.md` — the CLI machine contract (how the tool speaks).
- `SKILL-SPEC.md` — Skill authoring spec (how the agent uses it).
- `SEC-SPEC.md` — security baseline (how not to get burned, or burn others).
- This doc — repo skeleton (what the project looks like).

## 1. File manifest (required matrix)

Levels: **MUST** required; **SHOULD** strongly recommended; **optional** as needed.

| File | Level | Role |
|------|-------|------|
| `README.md` | MUST | project front door: what it is, how to install, how to use |
| `README_zh.md` | SHOULD | Chinese version (i18n convention in §3) |
| `LICENSE` | MUST | open-source license (default MIT) |
| `CHANGELOG.md` | MUST | version change log (Keep a Changelog + SemVer) |
| `CONTRIBUTING.md` | MUST | how to contribute: env, branch, commit, test, PR flow |
| `SECURITY.md` | MUST | vulnerability disclosure channel and supported versions |
| `CODE_OF_CONDUCT.md` | SHOULD | code of conduct (Contributor Covenant) |
| `NOTICE.md` | MUST† | third-party trademark / attribution notice (†required when wrapping a third-party product) |
| `docs/COMPATIBILITY.md` | MUST† | verified backend version matrix (†required when integrating an external system) |
| `docs/OPEN_SOURCE_CHECKLIST.md` | SHOULD | security gate checklist before the first public push |
| `docs/E2E.md` | SHOULD† | E2E / integration test environment notes (†recommended when calling external APIs) |
| `.gitignore` | MUST | ignore build artifacts, venvs, IDE, caches by language |
| `.github/workflows/ci.yml` | MUST | on push / PR: lint + test |
| `.github/workflows/release.yml` | SHOULD | on tag: build + publish |
| `.github/ISSUE_TEMPLATE/` | SHOULD | bug / feature templates |
| `.github/PULL_REQUEST_TEMPLATE.md` | SHOULD | PR self-check checklist |
| `.github/dependabot.yml` | optional | dependency updates |
| `AGENTS.md` | MUST* | cross-tool entry hook, points to `.agent/AGENT.md` |
| `.agent/AGENT.md` | MUST* | AI-native tool entry / playbook, navigates to the local specs and this shared repo standard |
| `.agent/CLI-SPEC.md` | MUST* | the CLI machine contract (*CLI projects only) |
| `.agent/SKILL-SPEC.md` | MUST* | Skill authoring spec (*projects with a Skill) |
| `.agent/SEC-SPEC.md` | MUST* | security baseline (risk tiers + injection/privilege/credential/supply-chain) |
| `skills/<name>/SKILL.md` | MUST* | Agent Skill (*required for AI-native tools) |
| `skills/<name>/test-prompts.json` | SHOULD* | Skill regression prompts for review / Darwin-style validation |

`*` items are specific to "AI-native tools" — a plain library may omit them, but any agent-facing CLI / tool must have them. `†` items are scenario-triggered: wrapping a third-party product (Outlook/Exchange, GitLab, Jira, Kibana, etc.) requires the trademark notice and compatibility matrix.

## 2. Required README skeleton

Every README follows a fixed order, for easy cross-comparison and direct Agent onboarding:

1. **Title + language switch + badges** — badge order is CI, language quality badge when applicable, npm version, license.
2. **One-line positioning** — Agent-native tool description. Do not position these tools as primarily interactive human CLIs.
3. **Agent Install** — one copy-paste block an AI Agent can run: npm wrapper install, Skill install, required env placeholders, `context`, `doctor`, and `reference`.
4. **What It Does** — one paragraph, risk tier, and any third-party/disclaimer note.
5. **Capabilities** — compact table of command groups; README is a map, not the full manual.
6. **Agent Workflow** — install, configure, preflight, discover via `reference`, reduce tokens with `--compact`/`--fields`, write with `--dry-run` -> `--confirm`, refresh via `changelog`.
7. **Machine Contract** — JSON default, envelope shape, stdout/stderr rules, error codes, `_untrusted`, and `--json` compatibility note.
8. **Configuration** — config path, env vars, credential precedence and storage.
9. **Project Structure** — top-level tree so every repo looks navigable at a glance.
10. **Development** — local build/test/lint commands.
11. **Links** — `AGENTS.md`, Skill, `.agent/CLI-SPEC.md`, security, compatibility, E2E, changelog, contributing, notice, license.

Conventions:

- Badge sets are intentionally small. Do not add vanity badges that do not affect Agent trust or release quality.
- Install blocks must be copy-paste-runnable by an Agent, with secret placeholders instead of real credentials.
- Command detail belongs in the live `reference` output. README command tables stay high-level to avoid stale manual pages.
- Example commands use real runnable forms, time as ISO 8601, ID as placeholder `<message-id>`.

## 3. Internationalization (i18n) convention

- Primary doc in English `README.md`, Chinese `README_zh.md`, filename gets a `_zh` suffix.
- The two link to each other at the top.
- Other governance docs (CONTRIBUTING / SECURITY / code of conduct) are English-only by default, unless the project is primarily for a Chinese community.
- Bilingual docs are **kept in sync**: change one, change the other; CI may add a link / section consistency check.

## 4. Versioning and release convention

- **SemVer**: `MAJOR.MINOR.PATCH`. Breaking changes bump MAJOR, backward-compatible additions bump MINOR, fixes bump PATCH.
- The tool version (package version) is decoupled from the CLI's output `schema_version` — the latter bumps only when the JSON contract breaks (see `CLI-SPEC.md`).
- **CHANGELOG uses Keep a Changelog format**: `Unreleased` on top, split into `Added/Changed/Fixed/Deprecated/Removed/Security`.
- A release means tagging git `vX.Y.Z`, triggering build & publish via `release.yml`.
- Single source of truth for the version number (e.g. `package.json` / `setup.py`); everything else references it, no hand-copying.

### CHANGELOG single source of truth

`CHANGELOG.md` is the only human-maintained change source; everything else is **derived**, must not be separately maintained, and derived artifacts must not be committed:

```
CHANGELOG.md (human-maintained, single source of truth)
   ├─ release.yml slices the "## [version]" section → GitHub Release body (generated at build)
   └─ embedded into the binary at build → runtime changelog command (see CLI-SPEC.md §11)
```

- release-notes are generated by `release.yml` at release time (`sed -n "/^## \[VERSION\]/,/^## \[/p"`), **don't keep a hand-maintained `release-notes.md` in the repo**.
- The runtime `changelog` command reuses the same CHANGELOG, embedded at build — zero new source, just one more outlet.

**Runtime changelog file naming.** The source file that embeds `CHANGELOG.md` for the runtime `changelog` command is named `changelog.go` (Go) / `changelog.py` (Python) — **singular, with NO `_embed` suffix**. In Go it holds the `//go:embed` directive and exposes the embedded contents as a package var named `ChangelogMarkdown`.

**Go version pinning.** Go projects pin a single current Go version in `go.mod` (`go 1.XX`) and **do not** add a `toolchain` line; CI reads that one source via `setup-go`'s `go-version-file: go.mod` rather than hard-coding a version in the workflow.

## 4b. Distribution convention (npm wrapper)

Cross-language tools distribute via a uniform npm wrapper, so Go binaries and Python packages both `npm install -g` and are called consistently by agents:

- `package.json` declares the scoped package name, `bin` entry, and OS/CPU-specific optional platform packages. It is the **single source of truth for the npm wrapper's scope/name and version** — `scripts/run.js`, the platform-package helper, and CI read them from `package.json`, never hard-copying the scope or version elsewhere.
- `scripts/run.js`: a thin forwarding layer that resolves the installed npm platform package and `execFileSync` invokes its binary, passing through `argv` and stdio, with an actionable reinstall hint if the platform package is missing.
- `scripts/prepare-npm-platform-packages.js`: release-time helper that packages already-built CI binaries into npm optional platform packages. Runtime npm installation must not download binaries from GitHub.
- The binary itself (`bin/`, `*.exe`, `dist/`) goes into `.gitignore`, produced by CI, not committed.
- Release artifacts ship `checksums.txt`; the release pipeline signs that file with
  Sigstore/Cosign keyless signing from the tagged GitHub Actions release workflow
  using `cosign sign-blob --new-bundle-format` (a Sigstore protobuf bundle), and
  publishes the bundle alongside the checksum file.
- npm distribution publishes the main package and all platform packages with npm
  provenance. npm registry tarball integrity and provenance cover the npm install
  path. The standalone GitHub binary self-update path verifies that signature
  **in-process** (Go via `sigstore-go`, Python inside the frozen binary via
  `sigstore` — no external cosign, no user-environment dependency), fail-closed
  with no skip path; integrity failures return the non-retryable `E_INTEGRITY`
  code. Python tools are frozen to a self-contained binary so this path is
  identical across languages (see CLI-SPEC §14 / SEC-SPEC §5).
- Version notifications are an Agent-facing structured contract, not an
  ambient banner. `update --check` refreshes update state and may write a short
  cache under the tool's user config directory. `doctor` may actively refresh
  with a short timeout and degrade silently on network failure. `context` and
  `--help` only read the cache. Business commands must not phone home just to
  advertise updates.
- When an update is available, maintenance command JSON adds
  `data.notices[]` with `type: "update_available"`, current/latest versions,
  install method, recommended command, release URL when known, checked-at
  timestamp, and next steps. Text/help output may append one concise line.
- `update --confirm` owns the full lifecycle: binary/package update plus whole
  `skills/<name>/` directory sync, with the same end state as
  `npx skills add <repo> -y -g`.

## 5. Directory layout convention

```text
project/
├── README.md / README_zh.md
├── LICENSE / CHANGELOG.md / CONTRIBUTING.md / SECURITY.md
├── .gitignore
├── AGENTS.md                   # cross-tool entry hook, points to .agent/AGENT.md
├── .github/
│   ├── workflows/{ci,release}.yml
│   ├── ISSUE_TEMPLATE/
│   └── PULL_REQUEST_TEMPLATE.md
├── .agent/                     # AI-native specs
│   ├── AGENT.md                # entry / playbook
│   ├── CLI-SPEC.md             # CLI machine contract
│   ├── SKILL-SPEC.md           # Skill authoring spec
│   └── SEC-SPEC.md             # security baseline
├── skills/<name>/              # bundled Agent Skill
│   ├── SKILL.md                # executable Skill contract
│   └── test-prompts.json       # regression prompts for Skill review
├── <package>/                  # source (package name)
├── tests/                      # tests, structure mirrors source
├── scripts/                    # dev / build helper scripts
└── <build manifest>            # language-specific: package.json / setup.py / pyproject.toml ...
```

Conventions:

- Source, tests, scripts, docs each in their place, not dumped in the root.
- Test directory mirrors the source structure, for easy locating.
- Build artifacts, caches, venvs, IDE config all go into `.gitignore`, not committed.

### 5b. Template model: `common/` + `<lang>/`

The seed in this repo's `template/` is split so that the skeleton above is assembled from two layers, and so adding a language stays cheap:

```text
template/
├── common/        # language-agnostic, copied into EVERY repo: AGENTS.md, the four
│                  #   .agent/ specs, all governance docs, .github/, package.json,
│                  #   scripts/{run,prepare-npm-platform-packages}.js, docs/, skills/SKILL.md.tmpl, .gitignore
├── go/            # overlay: ci.yml, release.yml, .goreleaser.yml, Makefile,
│                  #   .golangci.yml, .gitignore
└── python/        # overlay: ci.yml, release.yml, ruff.toml, .gitignore
```

- Instantiate by copying `common/.` then overlaying **exactly one** `<lang>/.`; concatenate the `<lang>` `.gitignore` onto the common one. See `template/INSTANTIATE.md`.
- **Adding a language = add `template/<lang>/`** (~6 files of CI / release / formatter / build plumbing), reusing 100% of `common/`.
- The `.agent/` behavioral specs (`AGENT`, `CLI-SPEC`, `SKILL-SPEC`, `SEC-SPEC`) live in `common/` and **never fork per language** — the machine contract is identical across languages; only build/CI plumbing differs.

## 6. Quality gate convention

- CI must run lint + tests; red means no merge.
- AI-native CLI releases require **Functional Contract Coverage = 100%**: every public behavior documented in README, Skill, `reference`, `--help`, `context`, `doctor`, `changelog`, or `update` has command-level tests. Numeric line coverage can ratchet upward separately, but cannot replace missing contract tests.
- Release readiness is part of the machine contract: `reference` reports `release_readiness.level` as `stable`, `beta`, or `unpublishable`, and `doctor` includes a `release_readiness` check. `Stable` requires FCC 100%, mock upstream/contract tests with failure-path coverage, and recorded live smoke/E2E evidence. `Beta` is allowed only when FCC and mock contracts are in place but live smoke evidence is missing or declared unavailable. Missing command-level coverage is `unpublishable`.
- A unified formatter (by language: Python ruff, JS prettier, etc.), config committed, no manual alignment.
- The PR template has built-in self-check items: tests pass, docs synced, CHANGELOG updated, bilingual synced.

## 7. New-project checklist

- [ ] `README.md` (+ `README_zh.md` as needed) follows the §2 skeleton
- [ ] `LICENSE` chosen
- [ ] `CHANGELOG.md` initialized, with `Unreleased`
- [ ] `CONTRIBUTING.md` / `SECURITY.md` in place
- [ ] `.gitignore` set up by language
- [ ] `.github/workflows/ci.yml` (lint + test) in place
- [ ] Single source of truth for version; CHANGELOG is the only change source, derived artifacts not committed
- [ ] Source / tests / scripts separated, tests mirror source
- [ ] (AI-native CLI) Functional Contract Coverage release gate documented and wired into contribution/checklist docs
- [ ] (AI-native CLI) `reference.release_readiness` and `doctor.checks[].check == "release_readiness"` implemented
- [ ] Formatter config committed, enforced in CI
- [ ] (npm wrapper) `package.json` + `scripts/run.js` + `scripts/prepare-npm-platform-packages.js`, binary not committed
- [ ] (wrapping a third-party product) `NOTICE.md` + `docs/COMPATIBILITY.md` in place
- [ ] (calling external APIs) `docs/E2E.md` + integration tests in place
- [ ] `docs/OPEN_SOURCE_CHECKLIST.md` in place, run through before the first push
- [ ] (AI-native tool) root `AGENTS.md` + `.agent/{AGENT,CLI-SPEC,SKILL-SPEC,SEC-SPEC}.md` + `skills/<name>/{SKILL.md,test-prompts.json}` complete
- [ ] PR / Issue templates in place
