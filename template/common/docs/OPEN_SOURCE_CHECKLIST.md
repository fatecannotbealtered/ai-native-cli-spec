# Open-Source Checklist

[English](OPEN_SOURCE_CHECKLIST.md) | [中文](OPEN_SOURCE_CHECKLIST_zh.md)

Run through this gate **before the first public push** of `{{TOOL_NAME}}`. It is a security and quality checkpoint, not documentation — every box must be ticked (or consciously waived with a note) before the repo goes public. Once public, secrets in history cannot be un-leaked.

## Secrets

- [ ] No credentials, tokens, API keys, or passwords anywhere in the working tree.
- [ ] No secrets in **git history** — checked with a scanner (e.g. `gitleaks detect`, `git log -p | grep`); if found, the history is rewritten or the repo re-created, not just deleted from `HEAD`.
- [ ] No internal hostnames, private IPs, internal URLs, or employer-internal identifiers in code, config, or comments.
- [ ] Test fixtures and recorded responses contain only synthetic / redacted data — no real account data, no real tokens.
- [ ] `.env`, `*.local`, credential files, and `~/.{{TOOL_NAME}}/` artifacts are listed in `.gitignore` and confirmed untracked (`git status --ignored`).
- [ ] Credentials are stored encrypted at rest (OS keyring or encrypted envelope, `0600`), never plaintext in the config file.

## Docs

- [ ] `README.md` follows the REPO-SPEC §2 skeleton (Title/badges → Agent Install → What It Does → Capabilities → Agent Workflow → Machine Contract → Configuration → Project Structure → Development → Links).
- [ ] `README.md` and `README_zh.md` are **content-synced** — same sections, same commands, same placeholders resolved to the same real values.
- [ ] `CHANGELOG.md` exists, uses Keep a Changelog format, and has an `## [Unreleased]` section on top.
- [ ] `LICENSE` is present, the license is chosen deliberately (default MIT), and `{{COPYRIGHT_YEAR}}` / `{{COPYRIGHT_HOLDER}}` are filled in.
- [ ] Install blocks are copy-paste-runnable and use the real published `{{NPM_PKG}}` / `{{REPO_SLUG}}`.

## Governance

- [ ] `SECURITY.md` is present with a working disclosure channel (`{{CONTACT_EMAIL}}`) and a supported-versions table.
- [ ] `CONTRIBUTING.md` is present (env setup, branch/commit, test, PR flow).
- [ ] `CODE_OF_CONDUCT.md` is present (Contributor Covenant) if the project accepts external contributions.
- [ ] If `{{TOOL_NAME}}` wraps a third-party product ({{THIRD_PARTY}}), `NOTICE.md` carries the trademark / non-affiliation notice and `docs/COMPATIBILITY.md` lists the verified backend version matrix.

## Build / CI

- [ ] CI (`.github/workflows/ci.yml`) is **green** on the commit being pushed.
- [ ] CI **enforces** lint and tests — a red lint or failing test blocks merge (not advisory-only).
- [ ] Functional Contract Coverage is 100%: every public behavior documented in README, Skill, `reference`, `--help`, `context`, `doctor`, `changelog`, or `update` has automated command-level tests.
- [ ] `reference.release_readiness.level` is accurate: `stable` has FCC 100%, mock upstream/contract tests, and recorded live smoke/E2E evidence; missing live evidence is `beta`; missing command-level coverage is `unpublishable`.
- [ ] `doctor` includes a `release_readiness` check whose status matches the declared release level.
- [ ] The formatter config is committed (ruff / golangci-lint / prettier, by language) and CI runs the format check.
- [ ] No build artifacts, caches, venvs, or IDE config are committed (covered by `.gitignore`).

## Distribution

- [ ] `package.json` `version` matches the git tag being released (`vX.Y.Z` ↔ `X.Y.Z`); `release.yml` guards this and fails on mismatch.
- [ ] The binary itself (`bin/`, `*.exe`, `dist/`) is **not committed** — it is produced by CI and gitignored.
- [ ] GitHub Release artifacts ship a `checksums.txt`; standalone binary install/update paths verify the checksum and **fail closed** on mismatch or missing entries.
- [ ] Release pipeline signs `checksums.txt` with Sigstore/Cosign keyless signing, publishes the bundle, and standalone install/update paths report signature verification status separately from checksum verification.
- [ ] npm distribution publishes the main wrapper package plus every supported OS/CPU platform package from CI-built artifacts, with `npm publish --provenance`.
- [ ] The version number has a single source of truth; the runtime `changelog` command and the GitHub Release body are derived from `CHANGELOG.md`, not hand-copied.

## AI-native

- [ ] Root `AGENTS.md` is present and points to `.agent/AGENT.md`.
- [ ] `.agent/{AGENT,CLI-SPEC,SKILL-SPEC,SEC-SPEC}.md` specs are present; the shared repo skeleton standard is referenced from `ai-native-cli-spec/REPO-SPEC.md`.
- [ ] `skills/{{TOOL_NAME}}/SKILL.md` is present; frontmatter includes `version`, `license: MIT`, `user-invocable: true`, and `metadata.requires.min_version` matching the CLI version.
- [ ] `SKILL.md` includes `When to use`, `Do not use`, `First Step`, agent defaults, JSON contract, write recipe or explicit read-only boundary, `STOP CHECKPOINT`, error decision tree, security boundary, self-update, and eval scenarios.
- [ ] `skills/{{TOOL_NAME}}/test-prompts.json` is present, valid JSON, and covers fresh-agent read, write safety or read-only boundary, permission boundary, `_untrusted` handling, and self-update.
- [ ] A bare `update` (single command, no confirm token) syncs the whole `skills/{{TOOL_NAME}}/` directory or returns a `skill_sync_command` equivalent to `npx skills add {{REPO_SLUG}} -y -g`; failures/interruptions report `stage` + `current_version` + `binary_replaced` + `skill_sync_status`.
- [ ] `{{TOOL_NAME}} reference`, `{{TOOL_NAME}} context`, and `{{TOOL_NAME}} doctor` run and emit valid JSON envelopes — an agent can self-onboard from a clean checkout.
- [ ] `{{TOOL_NAME}} reference` exposes `release_readiness`, and `{{TOOL_NAME}} doctor` reports the matching check.
- [ ] The risk tier in `SECURITY.md` matches the tier declared in `.agent/SEC-SPEC.md` (`{{RISK_TIER}}`).
