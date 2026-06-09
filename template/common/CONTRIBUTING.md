# Contributing to {{TOOL_NAME}}

*English | [中文](CONTRIBUTING_zh.md)*

Thanks for improving **{{TOOL_NAME}}** — {{DESCRIPTION}}. This document covers building, testing, and submitting changes.

> This is a side project shared for AI-tooling experimentation; maintainers do not provide commercial support or production guarantees — see the README disclaimer.

## The build playbook lives in the specs

This repo is an **AI-native CLI tool**, designed for AI agents first. Before implementing or changing any feature, read the playbook:

- **[AGENTS.md](AGENTS.md)** — entry point; routes you to the local specs and the shared repo skeleton standard.
- **[`.agent/AGENT.md`](.agent/AGENT.md)** — the project playbook.
- **[`.agent/CLI-SPEC.md`](.agent/CLI-SPEC.md)** — the CLI output / error / write-loop contract.
- **[`.agent/SKILL-SPEC.md`](.agent/SKILL-SPEC.md)** — the AI Skill bundle spec.
- **[`.agent/SEC-SPEC.md`](.agent/SEC-SPEC.md)** — the security baseline (risk tiers, untrusted content, credentials, supply chain).

These specs are authoritative and take priority over default habits. Code that violates the CLI contract (stdout is the contract, uniform envelope, error triple, write loop) will not be merged.

## Development setup

<!--
language toolchain — keep ONE block below, delete the others:
  - Go 1.25+        : compiled binary + npm wrapper
  - Python 3.10+    : PyInstaller binary + npm wrapper
  - Node 16+        : required for the npm wrapper / platform-package scripts in all variants
The shape is always: install deps -> build -> test -> run `--help` smoke test.
-->

```bash
# Clone
git clone https://github.com/{{REPO_SLUG}}.git
cd {{TOOL_NAME}}

# --- Go variant ---
go mod download
make build                      # or: go build -o bin/{{TOOL_NAME}} {{CMD_PATH}}
go test -race ./...
./bin/{{TOOL_NAME}} --help

# --- Python variant ---
# pip install -e ".[dev]"
# python build.py
# pytest tests/ -v
# {{TOOL_NAME}} --help

# Optional: Node.js 16+ if you touch npm wrapper or platform-package scripts
```

If dependency download is slow, use a regional proxy (e.g. Go: `GOPROXY=https://goproxy.cn,direct`; pip: a mirror index).

## Commands

| Goal | ▶ Command |
|------|-----------|
| Build | `make build` (Go) / `python build.py` (Python) |
| Test | `make test` → `go test -race ./...` / `pytest tests/ -v` |
| Lint | `make lint` → `golangci-lint run ./...` / `ruff check .` |
| Format | `make fmt` → `gofmt -w .` / `ruff format .` |

`make` targets are variable-driven; on Windows fall back to the underlying tool command. New contributors should run **lint + test** locally before pushing.

## Branch & commit convention

- Branch from the default branch: `git checkout -b feat/your-feature`.
- Keep one logical change per branch; rebase the default branch before requesting review.
- Commits follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>: <description>

<optional body>
```

| Type | Use for |
|------|---------|
| `feat` | New features |
| `fix` | Bug fixes |
| `refactor` | Code restructuring without behavior change |
| `docs` | Documentation changes |
| `test` | Adding or updating tests |
| `chore` | Build, CI, dependency, or tooling changes |
| `perf` | Performance improvements |
| `ci` | CI/CD pipeline changes |

Examples: `feat: add export command`, `fix: handle nil pointer in status check`, `docs: sync README_zh`.

## CI mirror

CI in `.github/workflows/ci.yml` mirrors the local checks: it runs **lint + test** (plus a `--help` smoke test and dependency audit) across the supported OS / runtime matrix. **Both lint and test must pass** before a PR can merge — run them locally first to avoid round-trips.

## Functional contract coverage

Release standard: **Functional Contract Coverage = 100%**. Every public behavior documented in `README`, `SKILL`, reference pages, `{{TOOL_NAME}} reference`, `--help`, `context`, `doctor`, `changelog`, or `update` must have automated command-level tests.

For each new or changed command, cover success, invalid arguments, config/auth/permission failure where applicable, upstream failure or timeout where applicable, JSON envelope shape, output schema, exit code, stdout/stderr boundary, and non-interactive behavior. Every bug fix that changes observable behavior needs a regression test.

Numeric line coverage is tracked separately and may ratchet upward by repo, but it does not replace missing contract tests.

## Adding a new command / domain

The tool is sliced by domain (one domain ≈ one area of the wrapped product). To add a new domain, touch each layer:

1. **DTO** — define request/response types next to the API methods for that domain.
2. **Client** — add the API client methods; reuse the shared HTTP/auth helpers and parameterised URL building (never concatenate user input into URLs).
3. **Command** — add the Cobra/argparse command and subcommands; register flags; call the write-marker (`markWrite` / equivalent) on every mutating command so it enters the audit log and the `--dry-run → --confirm` loop.
4. **Tests** — API-level tests (against an HTTP test server) **and** command-level behavior tests.
5. **SKILL** — add a `skills/{{TOOL_NAME}}/reference/<domain>.md` page and link it from `skills/{{TOOL_NAME}}/SKILL.md` (keep SKILL.md a short progressive-disclosure index).
6. **Docs** — update `README.md` / `README_zh.md` command lists and add a line under `## [Unreleased]` in `CHANGELOG.md`.

`reference` walks the command tree automatically, so new commands appear in `{{TOOL_NAME}} reference` without extra wiring.

## Pull request guidelines

1. **One logical change per PR** when possible.
2. **Tests**: add or update tests for behavior changes.
3. **Docs**: update user-facing docs when flags/flows change.
4. **Commits**: Conventional Commits; no secrets or real tokens anywhere.

### PR checklist

- [ ] One logical change, focused diff
- [ ] Tests added/updated and passing (`make test`)
- [ ] Functional Contract Coverage remains 100% for public behavior
- [ ] Lint passing (`make lint`)
- [ ] Docs synced with behavior (`README` and any affected `SKILL`/reference pages)
- [ ] `CHANGELOG.md` updated under `## [Unreleased]`
- [ ] **Bilingual docs synced** — every `*.md` change mirrored in its `*_zh.md` counterpart (and vice versa)
- [ ] No secrets, tokens, or real credentials in code, tests, fixtures, or commit history
- [ ] Commit messages follow Conventional Commits

## Security

Do not open public issues for undisclosed security vulnerabilities. See [SECURITY.md](SECURITY.md).
