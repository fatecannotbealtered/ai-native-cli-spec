# ai-native-cli-spec

[English](README.md) · [中文](README_zh.md) · [MIT licence](LICENSE)

One core specification for command-line tools whose main caller is an AI agent, plus the template and the shared libraries that implement it. A conforming tool answers in one JSON envelope, says in every error whether anything was sent and what to do next, refuses writes until a person has granted a scope, and reports only what it observed.

Version 2.0.0 is not released yet; see [CHANGELOG.md](CHANGELOG.md).

## What is in 2.0

| Part | Where | What it gives a tool |
|---|---|---|
| Five specs | `template/common/.agent/` | `AGENT.md` (playbook), `CLI-SPEC.md` (machine contract, `CLI-xxx`), `SEC-SPEC.md` (security, `SEC-xxx`), `SKILL-SPEC.md` (Agent Skills, `SKL-xxx`), `VERIFY-SPEC.md` (verification and agent eval, `VER-xxx`). Every normative rule has a stable ID and a `Check:` line. |
| Contract and schemas | `template/common/contract/` | `contract.json` (error codes, exit codes, enums, key sets, receipt rules), JSON Schemas (draft 2020-12) for every shape, and a corpus of valid and invalid examples. |
| aicli libraries | `template/go/aicli/` (Go, cobra), `template/python/aicli/` (Python ≥ 3.10, click) | The envelope, errors and HTTP mapping; `--help`, `reference` and the Skill command map generated from one registry; agent UX (root shortcuts, accepted guesses, whole-tree suggestions); the write gate (binding, keyed single-use tokens, operation journal, scope checks, receipts, batches); the built-ins `reference`, `context`, `doctor`, `changelog`, `operation` and `update`, the whole self-update with signature verification and the Skill sync; settings and profiles. |
| Conformance suite | inside each library (`RunConformance`, `run_conformance`) | Checks every write command against the gate (`G00`–`G10`) and the whole tool against the contract (`R01`–`R05`). `R06` (every registry command ran in the tool's own tests) runs separately, through `CheckCoverage` (`check_coverage`) once those tests finish. Every tool runs both in CI. |
| Example tools | `template/go/example/`, `template/python/example/` | A small "notes" CLI built on the library, with its conformance test. The reference implementation until a fleet tool ships on 2.0. |
| Repo standard | [`REPO-SPEC.md`](REPO-SPEC.md) | Files, layout, versioning, distribution, vendoring and CI for a tool repository (`REPO-xxx`). |
| Migration guide | [`MIGRATION-2.0.md`](MIGRATION-2.0.md) | Step-by-step move of a 1.6.x tool to 2.0. |

Tools vendor the specs, the contract, the schemas and the library by copy, pinned to one spec tag and checked for drift in CI. The library is not published as a package.

Out of scope for 2.0: the driver layer for desktop applications and devices (sessions, physical effects, unsaved state), which stays in tool-driver-cli-spec on top of this core, and MCP server generation (future work).

## Why 2.0

2.0 rests on two measurements made in October 2026. In an agent eval of gitlab-cli (720 trials, three Claude models, Skill on and off), the 2.0 design used 31% fewer CLI calls, 87% fewer invalid calls and 24% less wall time than gitlab-cli 1.2.17 run in the same batch, at the same task success. A read-only audit of the fleet found that confirm tokens did not bind the whole request in any of the 10 tools with a confirm gate, and could be forged in 9 of them; so in 2.0 the write gate is shared code with a conformance suite instead of a rule each tool implements. The method is in `VERIFY-SPEC.md`: VER-006 (agent eval) and VER-007 (gate audit).

## Quick start: a new tool

```bash
# A checkout of the spec at the release you will pin.
git clone --depth 1 --branch v2.0.0 https://github.com/fatecannotbealtered/ai-native-cli-spec spec
mkdir my-cli && cd my-cli
# The common layer, then only the CI and build files of one overlay (never its aicli/ or example/).
cp -R ../spec/template/common/. .
cp -R ../spec/template/go/.github ../spec/template/go/.golangci.yml ../spec/template/go/.goreleaser.yml ../spec/template/go/Makefile .
cat ../spec/template/go/.gitignore >> .gitignore
# Python instead: cp -R ../spec/template/python/.github ../spec/template/python/ruff.toml . && cat ../spec/template/python/.gitignore >> .gitignore
# Files that belong to the spec repository only.
rm -f scripts/gen-contract.js scripts/build-manifest.js scripts/check-contract.py && rm -rf contract/examples
```

These are step 1 of [`template/INSTANTIATE.md`](template/INSTANTIATE.md); follow the rest of it: fill the placeholders, vendor the library with `scripts/sync-spec.js` (it writes `internal/aicli/` or `<pkg>/_aicli/`; never copy `aicli/` by hand), and tell your agent "Read AGENTS.md, then build this tool following workflow A in .agent/AGENT.md". The example tools in the spec repo (`template/go/example/`, `template/python/example/`) show the finished shape; read them there.

## Quick start: migrating a 1.6.x tool

Pin `v2.0.0` in `.agent/SPEC_VERSION`, vendor the 2.0 files with the 2.0 copy of `scripts/sync-spec.js`, and work through [`MIGRATION-2.0.md`](MIGRATION-2.0.md) in order. The conformance suite tells you when the write paths are done.

## Repository layout

```text
README.md, MIGRATION-2.0.md, REPO-SPEC.md   (+ _zh translations)
CHANGELOG.md                                (English only)
template/
├── INSTANTIATE.md          how to create a tool repo from the template (+ _zh)
├── common/                 copied into every tool
│   ├── .agent/             AGENT, CLI-SPEC, SEC-SPEC, SKILL-SPEC, VERIFY-SPEC (+ _zh)
│   ├── contract/           contract.json, schemas/, examples/{valid,invalid}/
│   ├── scripts/            sync-spec, check-spec, spec-files, gen-contract, build-manifest, check-contract.py, version tooling, npm launcher
│   ├── skills/             SKILL.md and test-prompts.json templates
│   └── README, SECURITY, CONTRIBUTING, AGENTS.md (+ _zh), package.json, .github/ ...
├── go/                     Go overlay: CI, release, Makefile, linters
│   ├── aicli/              the Go library, its tests and MANIFEST.json
│   └── example/            example tool with a conformance test
└── python/                 Python overlay: CI, release, ruff config
    ├── aicli/              the Python library, its tests and MANIFEST.json
    └── example/            example tool with a conformance test
tools/                         agent-eval/ (VER-006 harness), gate-audit/ (VER-007 workflows); not vendored
skills/review-ai-native-cli/   Skill that reviews a tool repo against this spec
.github/workflows/             this repo's CI: library tests, examples, contract check, tools
```

## Versioning

- Releases are git tags `vMAJOR.MINOR.PATCH`. One tag versions everything in the table above; a tool pins one tag in `.agent/SPEC_VERSION` and gets specs, contract, schemas and library that were tested together.
- `contract.json` carries `spec_version` (`2.0.0`) and `contract_version` (`2.0`). The envelope's `schema_version` is `"2.0"` and changes major only when the envelope breaks (CLI-080).
- A change that breaks conforming tools or their agents bumps MAJOR. Rule IDs never change meaning and are never reused.
- History: [CHANGELOG.md](CHANGELOG.md). English is the source text; `*_zh.md` files are translations of it.

## Licence

[MIT](LICENSE). Copyright (c) 2026 Sean Guo.
