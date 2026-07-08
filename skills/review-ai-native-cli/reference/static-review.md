# Static review — file-by-file checks

Contents: 1. Spec integrity · 2. Repo skeleton · 3. Contract single-sourcing ·
4. Version single-sourcing · 5. Skill compliance · 6. Security posture ·
7. Tests & FCC evidence · 8. Docs & i18n

Grade against the target repo's vendored specs at the pinned tag. Cite spec
sections in every finding. Read files; do not assume.

**Conditional rules.** Not every target is a public GitHub repo with npm-shell
releases. Checks below marked "if <channel>" apply only when the repo actually
uses that channel (public releases, npm shell, GitHub Actions, …). An internal
or source-only tool that never distributes binaries simply has fewer applicable
checks — record those as N/A, not as findings. The machine contract itself
(envelope, exit codes, confirm flow, self-describing commands, `_untrusted`) is
unconditional.

**Overlay precedence.** The spec text at the pinned tag (e.g. REPO-SPEC's file
matrix) was written with public open-source distribution in mind and marks some
channel-bound items as unconditional MUSTs (`LICENSE`,
`.github/workflows/ci.yml`). Where that text and the conditional rules here
disagree, **this skill's conditional rules win** — they are an authoritative
overlay for non-public/non-GitHub targets. Cite the spec section as usual and
note "waived by channel overlay" instead of filing a finding.

## 1. Spec integrity (Blocker unless noted)

- `.agent/SPEC_VERSION` exists and holds a tag (e.g. `v1.5.0`). Missing pin in an internal fork → Minor + grade against the vendored files as-is (per Phase 0).
- All registered spec files exist: if `scripts/spec-files.js` is present, read it (it exports the registry; it prints nothing when run bare — `node -p "require('./scripts/spec-files.js')"` dumps it), else check `.agent/{AGENT,CLI-SPEC,SKILL-SPEC,SEC-SPEC}.md` (+ `_zh` copies) and `contract/contract.json`.
- Specs are unmodified vendored copies. Heuristic without network: `git log --oneline -- .agent/ contract/contract.json` — expect only commits that bump the pin and re-vendor (typically touching `SPEC_VERSION` together with spec files; message conventions vary). Commits editing spec files *without* a pin bump suggest local edits — verify by diffing before filing; a confirmed hand-edit is a Blocker (REPO-SPEC §5c: tools never edit specs locally). Squashed or shallow history limits this heuristic — say so rather than guessing. Exception: an internal fork may legitimately vendor from its own mirror of the spec — judge "unmodified" against whatever source the repo declares, and flag only local drift.
- If the repo has `scripts/check-spec.js` and the spec source is reachable, run it — it is the authoritative byte-for-byte guard. Unreachable (air-gapped, internal network) → note as N/A, fall back to the git-history check above.
- Generated contract modules (`contract_gen.go` / `contract_gen.py`) exist and CI runs the offline gen-consistency check.

## 2. Repo skeleton (REPO-SPEC §1, §5)

MUST files: `README.md`, `CHANGELOG.md` (Keep a Changelog with `Unreleased`), `CONTRIBUTING.md`, `SECURITY.md`, `.gitignore`, CI config (`.github/workflows/ci.yml`, `.gitlab-ci.yml`, or the platform's equivalent — what matters is lint + test on push/PR, not the filename), `AGENTS.md`, `.agent/*`, `skills/<tool>/SKILL.md`.

Conditional MUSTs:

- Published as open source → `LICENSE` (+ open-source checklist); internal/proprietary tools may omit it — N/A, not a finding.
- Wraps a third-party product → `NOTICE.md` + `docs/COMPATIBILITY.md`.
- Talks to an external API → `docs/E2E.md` expected (SHOULD).
- npm-shell distribution → `package.json` + `scripts/run.js` + `scripts/prepare-npm-platform-packages.js`; binaries (`bin/`, `*.exe`, `dist/`) in `.gitignore`, never committed. Tools distributed another way (internal registry, `go install`, source-only) skip the npm-shell checks; version single-sourcing (§4) still applies to whatever manifest is the declared version source.

Layout: source / `tests/` (mirroring source) / `scripts/` / docs separated; no build artifacts, venvs, or IDE config committed. Exactly one language overlay (never both `.golangci.yml` and `ruff.toml`).

README skeleton order (REPO-SPEC §2, Minor if off): title+lang switch+badges → one-line agent-native positioning → Agent Install block → What It Does → Capabilities → Agent Workflow → Machine Contract → Configuration → Project Structure → Development → Links. Install block must be copy-runnable with placeholder secrets; command tables stay high-level (details belong to live `reference`).

## 3. Contract single-sourcing (CLI-SPEC §3.1)

- `contract/contract.json` present and untouched; tool-specific error codes live in `contract-ext.json` only — ext codes must be `E_*` with `{exit, retryable}`, must not override core codes; exit 9 reserved for human-action codes.
- A runtime conformance test exists that checks real command output against `contract.json`.

## 4. Version single-sourcing (REPO-SPEC §4)

Identify the repo's single version source (`package.json` for npm-shell repos;
otherwise whatever manifest the repo declares — `pyproject.toml`, VERSION file,
…) and compare it against every derived location:

- If npm shell: `package.json` `optionalDependencies` pins and `package-lock.json` (top version + platform pins), plus `.npmrc` with `git-tag-version=false`.
- Every `skills/*/SKILL.md`: `version:` frontmatter and `metadata.requires.min_version`.
- Python: `<pkg>/__init__.py` `__version__`. Go: version read from the embedded copy of the declared version source, not hardcoded.
- Any mismatch → Major (check-version should have caught it; also verify the version-consistency check is wired into CI and, if a release pipeline exists, into it too).
- Tests must not hardcode version numbers (grep tests for the current version string).
- Runtime changelog source named `changelog.go` / `changelog.py` (singular, no `_embed` suffix), embedding `CHANGELOG.md`.

## 5. Skill compliance (SKILL-SPEC §10 checklist)

Apply the full checklist from the target's `.agent/SKILL-SPEC.md` §10. Highest-weight items:

- Frontmatter: `name` kebab-case ≤64, no reserved words; `description` third-person, what+when, ≤1024; `version` = tool version = `metadata.requires.min_version` (three equal numbers); `metadata.requires.bins` is a string array.
- Body < 500 lines; references one level deep; forward slashes only.
- No drift-prone duplication: flags, schemas, error lists must point to `reference`, not be copied in (Major).
- Install block on top uses `npx skills add ...`; CLI has no `install-skill` command.
- Required AI-native sections present: triggers incl. "do not use", First Step (context/doctor/reference + min_version check), write recipe (dry-run → confirm) or explicit read-only boundary, STOP CHECKPOINTs, error decision tree, security boundary, `_untrusted` rule, self-update recipe (if the tool self-updates), 3–6 playbooks, `## Eval Scenarios`.
- `test-prompts.json` exists, prompts are tool-specific (not template stubs), covering: fresh-agent read, write safety or read-only boundary, permission boundary, `_untrusted`, self-update.
- Leftover `{{...}}` placeholders anywhere in the repo → Blocker: `grep -rnE '\{\{[A-Z_0-9]+\}\}' --exclude-dir=node_modules --exclude-dir=.git .` (pattern excludes legitimate `${{ ... }}` in workflows and `{{ .Var }}` in goreleaser).

## 6. Security posture (SEC-SPEC §7, tier-gated)

- Risk tier declared in `SECURITY.md` and in `reference` output (cross-check in dynamic phase).
- T0+: `_untrusted` annotation implemented — grep source for `_untrusted`; commands returning external content (mail bodies, comments, scraped text, upstream records) must emit it (Blocker if absent where external content flows).
- T1+: default read-only posture; credential storage follows keyring three-tier (secrets in OS keychain; config file has zero secrets — grep config-writing code for token/password fields); encrypted-file fallback visible via `context.data.credentials` backend field.
- T1+ supply chain: lockfile committed; CI runs `npm audit` / `pip-audit`. If the tool publishes binary releases with self-update: the release pipeline signs `checksums.txt` (cosign `--new-bundle-format` on GitHub; an equivalent signing step on other platforms) and self-update verifies in-process (imports `sigstore-go` / `sigstore`, no cosign exec). No binary distribution / no self-update → these are N/A.
- T2: dangerous ops gated behind `--dangerous` (or documented equivalent) on top of confirm; blast radius documented in `reference` / `SECURITY.md`.
- Secrets hygiene: grep for obvious leaks — `--password` flags promoted as the recommended path, tokens interpolated into log lines, `Authorization` headers in error details.
- Source-level injection surface (concretizes SEC-SPEC §2/§3 — agents pass hallucinated or attacker-influenced values into every parameter):
  - Shell execution: grep for `shell=True` (Python) / `sh -c` string concatenation (Go `exec.Command("sh", "-c", ...)`) — subprocess arguments must be argv arrays, never interpolated shell strings (Blocker if user/agent input reaches a shell string).
  - Path handling: file-path parameters that reach the filesystem should be canonicalized (`filepath.Clean`+`Abs` / `os.path.realpath`) before use; output paths that accept `../` traversal out of the intended directory → Major.
  - Enum-like parameters forwarded to a subprocess or query language (codec names, format selectors, sort fields) should be validated against an allowlist, not passed through raw.
  - Content embedded into generated scripts/markup (SQL, XML, script snippets) must be escaped at the embedding point — spot-check one embedding site if the tool generates any.

## 7. Tests & FCC evidence (CLI-SPEC §13)

- FCC guard test exists (Go: `cmd/fcc_guard_test.go`; Python: `tests/test_fcc_guard.py`) and enumerates leaf commands from live `reference` output.
- Command-level tests exist per public command covering: success, bad args, auth/config failure, upstream failure (mock), envelope shape, exit codes, stdout/stderr boundary, non-interactive behavior.
- Mock-upstream tests cover failure paths, not just happy path (spot-check 2–3 command test files).
- CI runs lint + tests on push/PR; if a release pipeline exists, it gates on version consistency.

## 8. Docs & i18n (REPO-SPEC §3)

- `README_zh.md` is SHOULD-level (REPO-SPEC §1): absent → Minor at most. When both READMEs exist, they carry mutual switch links; spot-check that sections match (bilingual content sync — Minor if drifted).
- CHANGELOG has no hand-maintained release-notes duplicate committed.
