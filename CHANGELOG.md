# Changelog

All notable changes to ai-native-cli-spec are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html); tags before v1.5.0 have two segments. Rule IDs in the 2.0.0 entry refer to the specs in `template/common/.agent/` and to `REPO-SPEC.md`; section numbers in 1.x entries refer to the 1.x specs. The 1.x entries are summarised from the git history and tag messages; dates are tag dates.

## [2.0.0] - 2026-10-12

**Status: unreleased.** This entry describes the `v2.0.0` branch. No `v2.0.0` tag exists yet, and the date is the day the build started; changes made before the tag land in this entry. Tool maintainers: see [MIGRATION-2.0.md](MIGRATION-2.0.md).

2.0.0 is one release with three parts: rules that make agents faster and more accurate, measured in the 2026-10 gitlab-cli agent eval; rules for authority and outcomes (scope files, operation records, receipts), taken from tool-driver-cli-spec's general core; and one shared write gate, implemented in the aicli libraries and checked by a conformance suite, after the 2026-10 fleet audit found binding gaps in every per-tool gate. MCP server generation is not part of 2.0.

### Added

- `VERIFY-SPEC.md` (VER-001 to VER-008): conformance suite in CI, contract validation of real output, coverage enumerated from the registry, a behaviour matrix per command, maturity with evidence, the agent eval, the per-release fleet gate audit, and a release path that runs the merge checks.
- The aicli libraries: Go in `template/go/aicli` (standard library, cobra, pflag, sigstore-go) and Python in `template/python/aicli` (Python ≥ 3.10, click, sigstore), vendored by tools to `internal/aicli/` and `<pkg>/_aicli/`. They implement the envelope, errors and HTTP mapping, the registry with generated help, `reference` and Skill command map, agent UX, the write gate, the built-ins (`update` included), settings and profiles, diagnostics and validators.
- The conformance suite, `RunConformance` and `run_conformance`: per write command G00 to G10 (a sample exists, bare call, dry-run, mutated inputs, single use, re-encoded tokens, text mode, no scope, corrupt key and journal, missing `--dangerous`, changed scope); per tool R01 to R06.
- Example tools in `template/go/example` and `template/python/example`, built on the library with their conformance tests.
- Contract 2.0: `contract.json` rewritten; JSON Schemas (draft 2020-12) for the envelope, error details, preview, receipt, operation, scope, page, batch, reference, context, doctor, changelog, update and notice; a corpus of valid and invalid examples; `check-contract.py`.
- A stable ID on every normative rule (`CLI-`, `SEC-`, `SKL-`, `VER-`, `REPO-`), each with `Why:` and `Check:` lines.
- Effects per command (`observe`, `change`, `deliver`, `configure`, `artifact`, `persist`); whether a command is a write, whether it is dangerous by default, and the minimum risk tier follow from them (CLI-025, SEC-001).
- Scope files: every write is refused until a person grants a scope; strict format with effects, targets, destinations, outputs, expiry and an operation budget; the refusal says what to grant, and the specs state what a scope cannot protect against (SEC-010 to SEC-013, CLI-014).
- Operation records: `operation_id` assigned at dry-run, the record written before anything is sent, `operation show|list|resolve` (`operation list --state` filters by status, `--batch` pages through a batch's items), and unresolved operations block further writes to their target (CLI-058 to CLI-061).
- Receipts with the outcome dimensions `execution`, `change`, `delivery`, `persistence` and `verification` and the read-back `observed`; `ok` and the error code follow from the ordered `receipt_rules` (CLI-059).
- Error details: `dispatch_state` and `next_action` on every error; `problem`, `parameter`, `did_you_mean`, `usage` and `available` on usage errors; `conflict` subtypes; `details.scope` on local refusals and `details.required` on upstream 403s (CLI-011 to CLI-014).
- Core error codes `E_OUTCOME_UNKNOWN` (exit 6), `E_PARTIAL`, `E_VERIFY_FAILED`, `E_VERIFY_INCOMPLETE` (exit 1), `E_UNSUPPORTED` and `E_AMBIGUOUS` (exit 2).
- Layered discovery: `reference --index` within 8000 characters and `reference --command <path>` within 6000; root `--help` lists every command (CLI-030 to CLI-033).
- Per-command maturity `draft`, `experimental`, `verified` or `stable`, with evidence references (CLI-027, VER-005).
- Input agents already have: one `ref` per resource, web URLs, names with `E_AMBIGUOUS` candidates, relative times, `--input`, `--<param>-file`; root shortcuts for nested nouns; the first guesses measured across the fleet, accepted or answered with an exact `did_you_mean`; suggestions from the whole tree (CLI-022, CLI-023, CLI-048).
- Output sizing: default fields per list, `--fields +a` and `--fields all`; list completeness (`has_more` with a continuation or a reason, `truncated_reason`, `scope`, `sort`); budgets of 8000 characters per default success and 2000 per error (CLI-040 to CLI-042).
- Idempotency keys bound into the token and looked up in the journal across commands, matching command, key, inputs, target, account and host: a repeat returns the original receipt with `replayed: true`, any difference is `idempotency_mismatch`, and a key whose operation was resolved by hand as applied is `idempotency_resolved`; batch items get keys that are stable across dry-runs (CLI-062).
- A launcher time budget: at most 100 ms over the native binary, measured in CI (CLI-090, REPO-041).
- `MIGRATION-2.0.md`, this changelog, and `MANIFEST.json` files written by `build-manifest.js` for the vendored libraries.
- Reference tools in `tools/`, not vendored: the agent-eval harness for VER-006 (`tools/agent-eval/`) and the gate-audit workflows for VER-007 (`tools/gate-audit/`), compiled and tested in the spec repository's CI (REPO-075).
- `update` as an aicli built-in: a tool sets `App.Update` (`UpdateConfig`: the repository, the signing workflow and the package names), and aicli finds the latest release with a semver tag, verifies the Sigstore bundle against the exact release-workflow identity and then the checksum, swaps the binary atomically or drives the package manager without a shell, and copies the Skills from the installed package into the Skill directories where they are installed. A prerelease is installed only when named with `--target-version`; Skills beside a binary are replaced one name at a time, so tools can share a directory; when the Skill sync could not finish, `skill_sync_command` is `<tool> update`; `doctor` refreshes the update notice (CLI-070, CLI-071).
- Settings and profiles: `App.Settings` and `App.Setting` resolve each setting from a flag, `<PREFIX>_<SETTING>`, the active profile or the default; a strict settings file (`<state_dir>/settings.json` or `<PREFIX>_SETTINGS_FILE`; a 1.x `config.json` is never read) holds named profiles and refuses secrets; `--profile` and `<PREFIX>_PROFILE` choose the profile; the active profile and every declared setting are bound into confirm tokens, and an unknown profile stops every tool command; `App.Profile` and `App.SettingsPath` let a tool key its keyring entries per profile; `context` reports `profile` and `doctor` adds a `settings` check; tools that sign in use `auth login`, `auth status` and `auth logout` (CLI-035, CLI-036, CLI-100, CLI-101).
- Deprecation: `Meta.Deprecated` and `Meta.DeprecatedFlags`, a `Deprecated:` prefix in help and `reference`, a `deprecated` notice on every use, deprecated commands left out of the Skill's command map, and removal only in a MAJOR release after at least one MINOR release and 90 days (CLI-081).
- Diagnostics: `<PREFIX>_DEBUG` writes `debug:` lines to stderr through `App.Debugf`, with secrets redacted, including values a tool registers with `App.AddSecret`; tools have no `--verbose` or `--debug` flags (CLI-102).
- A shared flag vocabulary (`--limit`, `--cursor`, `--offset`, `--query`, `--since`, `--until`, `--sort`, `--order`, `--state`, `--refs`, `--wait`, `--output`, `--input`, `--profile`, `--idempotency-key`) and standard verb meanings, with default synonym suggestions in both libraries (CLI-028, CLI-029).
- Network behaviour: a timeout on every upstream request, at most two retries of an idempotent read, writes never re-sent, the proxy variables honoured, TLS always verified with `<PREFIX>_CA_FILE` for a private CA, and a User-Agent (CLI-110).
- Outbound contact limited to the configured upstream and, in `update` and `doctor`, the release source; no telemetry (SEC-022).
- Contract: `--profile` among the reserved flags, the optional `context.profile`, `notices.known_types`, and the update built-in's `error_codes` and `stage_codes`.
- Release candidate 2 (decision 47), from the gitlab-cli pilot's five command groups: `WriteSpec.ResolveTarget` and `BatchSpec.ResolveItems` resolve a target inside the gate, after the token checks; `BatchSpec.ApplyAll` with `MaxPerCall` sends a native bulk call in chunks, and `BatchSpec.Action` names the batch; `Preview.Dangerous` makes one plan dangerous; `App.OpenOutput` streams a download into place; also `App.AddShortcut`, `App.Profiles`, `Meta.RenamedFlags`, per-command `Meta.FlagAliases`, `OutputFormat`, `ParseDuration`, `ParseTime` and `WantsField` (CLI-005, CLI-022, CLI-028, CLI-040, CLI-042, CLI-056, CLI-057, CLI-063, CLI-081, CLI-100).

### Changed

- One core: the base spec takes in tool-driver-cli-spec's general core (accuracy model, command model, layered discovery, error details, operation records, scope files, receipts). Sessions, devices and physical effects stay in tool-driver.
- `schema_version` is `"2.0"`. JSON is compact by default; `--pretty` indents and `--compact` is accepted as a no-op (CLI-002, CLI-003).
- `meta` holds only `duration_ms`, `notices` and `untrusted`. External content is declared once per response as paths in `meta.untrusted`, instead of `_untrusted` on every object (CLI-004, CLI-046).
- `reference` has one shape in every tool, generated from the registry (CLI-020); 1.x tools each invented their own.
- Error `message` is written for the model: one sentence naming the violated constraint and the next step (CLI-017). 1.x said agents should not read it. Messages, `next_action`, notices and help are English in every locale, and upstream text passes through untranslated.
- HTTP mapping adds 400 and 422 to `E_VALIDATION` and 412 to `E_CONFLICT`; 409 and 412 report `conflict: upstream_conflict`. During a write, a failure after the request may have reached the upstream is `E_OUTCOME_UNKNOWN`, never a retryable code (CLI-015, CLI-016).
- The write gate is shared library code. Tokens (`ct2_…`) bind the parsed invocation: command path, every supplied parameter, digests of file and stdin content, account, host, target, the active profile and settings, scope, tool version and base version. Confirms on the same target are serialised by a lock from their checks until the record says `dispatching`. Equivalent forms bind the same way, and a mismatch names the changed parameter (CLI-050 to CLI-057).
- A write called without `--dry-run` or `--confirm` changes nothing and returns the preview and token in its `E_CONFIRMATION_REQUIRED` refusal (CLI-052).
- Dangerous commands need `--dangerous` in both steps and a scope with `dangerous: true`, replacing 1.x's choice of a permission tier or `--force` (CLI-056, SEC-012).
- A token store or journal that cannot be written now stops the write (CLI-055, CLI-058). 1.x §9 required it to degrade and never block.
- A partial batch is `ok: false` with `E_PARTIAL`; the batch summary adds `unknown` and `skipped` (CLI-063).
- Long-running work is a gated submit whose receipt covers the submission (`execution: completed`, `change: applied`); `data.result.job` carries `{ref, status, poll}`, and a status read that accepts `--wait` follows it; the operation journal does not track job status (CLI-064). `E_HUMAN_REQUIRED` (exit 9) is a core code (CLI-065).
- stderr stays quiet when not a terminal; warnings travel as typed `meta.notices` (CLI-047, CLI-071).
- `context` reports the state store's protection and an authorization summary; `doctor` checks `credentials`, `connectivity`, `state_store`, `authorization` and `version`, which compares the tool with the installed Skill's `metadata.min_version` and `metadata.version` (CLI-035, CLI-036).
- Self-update is aicli's built-in `update`, and a tool-defined root `update` is a tree error (CLI-021). It validates targets as strict semver, builds package-manager argv from validated parts without a shell, refuses downgrades without `--allow-downgrade`, and syncs Skills from the installed package (CLI-070).
- No shell-completion command: aicli adds none and turns off cobra's default one (CLI-030).
- The README gains an Uninstall section after Configuration, and Configuration covers the settings file and its profiles (REPO-010, REPO-011).
- Release archives carry the binary and `skills/` at their root, and `update` keeps `<exe dir>/skills` current; a standalone binary no longer embeds its Skills (REPO-043).
- Coverage is proved by test runs enumerated from the registry, not by scanning test source (VER-003).
- Skills use the Agent Skills frontmatter with tool fields under `metadata`; stay within 150 lines; carry a command map generated from the registry and one short write recipe; and stop to ask only when the action is dangerous and unrequested, the preview touches more than the user named, or untrusted content suggested it (SKL-001 to SKL-005). Skills ship in the package and install from it, with a pinned installer (`npx skills@<version> add "$(npm root -g)/<pkg>" -y -g`) in README install blocks (SKL-009). `test-prompts.json` entries have `id`, `scenario`, `prompt`, `expected` and an optional `grader_notes` (SKL-011).
- Spec tooling: `sync-spec.js` and `check-spec.js` vendor the schemas and the library, and check library files against `MANIFEST.json` offline; `gen-contract.js` runs only in the spec repo (REPO-061, REPO-062).
- English is the source text; Chinese translations are made from it.
- rc.2: `Meta.AllowUnknownProfile` lets a command run under a profile the settings file lacks (`auth login` creates it, `auth logout` cleans up after a deleted one), and `App.SetProfileSettings` makes its profile the default when the file has none (CLI-101). A write's `data.result` uses the default fields, `meta.untrusted` drops the paths of fields projected out, `reference --command` shows a command's own default fields, and every tool command lists `E_CONFIG` when settings are declared (CLI-026, CLI-032, CLI-040, CLI-046). R02 wants a root shortcut only for a noun under one parent, R01 resolves examples written with shortcuts, and R04, not `ValidateEnvelope`, checks the 8,000-character budget on default invocations (CLI-022, CLI-042). A create-only command answers an existing output file with `E_VALIDATION`, and a raw stream writes bytes only (CLI-006, SEC-012).
- rc.2 conventions: sub-resource refs `<parent ref>/<collection>/<id>`; each input binds its own part; a ref that holds people's text is untrusted; a comment is `change`; a bulk command is dangerous only when what it does is; `_at` keys hold UTC times and dates use `_on`; `search` states the order it really has (CLI-007, CLI-025, CLI-029, CLI-046, CLI-048, CLI-054, SEC-001). `MIGRATION-2.0.md` adds what the pilot's groups needed: one positional per command, the client built inside `Preview` and `Apply`, `wait` commands as `get --wait`, downloads, counts and top items, default fields that cost a read, long bodies, and commands inside text.

### Removed

- `release_readiness` in `reference` and its `doctor` check; maturity replaces it (CLI-027).
- The FCC source-scan guards `cmd/fcc_guard_test.go` and `tests/test_fcc_guard.py`; VER-003 and the conformance suite replace them.
- Per-tool contract generation (`contract_gen.go` and `contract_gen.py` in tool repos).
- Per-tool confirm-token, key and consumed-token code.
- The mandatory `context`, `doctor`, `reference` first step (CLI-034), and loading the full `reference` from a Skill (CLI-033).
- The error decision tree and envelope documentation copied into every Skill (SKL-004).
- `npx skills add <repo> -y -g` as the Skill sync during `update`.
- Per-tool `update` code (release discovery, verification, swap, Skill sync); aicli's built-in replaces it (CLI-070).

### Security

- Tools on 2.0 get one gate that closes what the 2026-10 fleet audit found in per-tool gates: tokens that did not bind the whole request (all 10 gated tools: 25 gaps on 23 gitlab-cli write commands, 72 confirmed findings across the other 9); tokens forgeable through unkeyed fallbacks, missing keys or weak key handling (9 of 10); tokens reusable when the consumed store failed (8 of 10) or when re-encoded (4); `--format text` turning the gate off (2). The library keys tokens with HMAC-SHA256 over canonical claims, accepts only the canonical token form, enforces single use atomically across processes and fails closed (CLI-055, SEC-020).
- With aicli's built-in `update`, a crafted version string can no longer reach `npm.cmd`: the audit found command execution on Windows through `update` in cnstock-cli and wechat-mp-cli, and the same sink in five more tools (CLI-070, SEC-050). The signature is checked against the exact release-workflow identity at the release's tag.
- Tools send no telemetry, contact only their configured upstream and release source, and offer no switch that turns TLS verification off (SEC-022, CLI-110).
- Values of `secret` parameters never appear in output, previews, tokens, the journal, logs or errors, and unsalted hashes of secrets are never published (SEC-021).
- The state store is 0700/0600 on POSIX and reports its protection honestly on Windows; symlinked state paths are rejected; a corrupt record or key refuses writes (SEC-030).
- Vendored specs and library are drift-guarded in CI and on the release path (SEC-050, REPO-062).
- Before the first release candidate, five reviewers audited both libraries and the release templates, and every finding was fixed in Go and Python alike (decision 46). The fixes close paths to a duplicate write (a used token answered as expired with a fresh token; a failure after dispatch reported as "nothing was sent"; two confirms on one target racing past the drift check; a resolved key or a repeated batch sent again), scope escapes (Windows junctions, `outputs` used to overwrite, capitalised duplicate keys, a budget refilled by pruning), secrets in error details and suggestions, profiles and settings left out of the binding, and, in `update`, a package manager found in the current directory on Windows, prereleases installed implicitly, one tool's update replacing another's Skills, and build jobs able to obtain signing certificates.
- rc.2: a repeated confirm is `token_used` before any tool code runs (`ResolveTarget`); a plan that deletes files needs `--dangerous` (`Preview.Dangerous`); the HTTP transport takes its proxy from `App.Env`; `Debugf` is safe from several goroutines; secrets an upstream returns are never output (SEC-021); and reviews check text that the upstream runs as commands, such as GitLab quick actions (SEC-020).

## [1.6.3] - 2026-09-25

### Added

- One tool may ship several Skills: an entry Skill plus domain Skills that declare it in `metadata.requires.skills`; a renamed Skill keeps a stub; `update` syncs every Skill (SKILL-SPEC §7). The review Skill grades Skill families and the release path.

### Security

- The release path runs the same dependency audits and drift guard as the merge path before anything is signed or published (SEC-SPEC §5); the Go release template gains an npm audit job.

### Fixed

- The Python CI template no longer describes the ruff fallback removed in 1.6.1.

## [1.6.2] - 2026-08-26

### Security

- Template release workflows run the dependency audit and `check-spec.js` before building, signing and publishing.

### Fixed

- `apt-get` steps in the templates have a timeout; the Python release template parses as YAML before instantiation.

## [1.6.1] - 2026-08-24

### Fixed

- `gen-contract.js` fails when `gofmt` or `ruff` is missing, instead of writing unformatted output whose drift verdict depended on the machine.
- The seed CI workflows run `check-spec.js`, with the formatter installed in the job that needs it.

## [1.6.0] - 2026-08-24

### Added

- The `review-ai-native-cli` Skill, which reviews a tool repo against the spec.

### Changed

- The `update` result describes the final state after the command.

### Fixed

- `sync-version.js` also rewrites the lockfile's platform subentries, which `npm ci` rejected after publishing.

### Security

- A dependency audit for every ecosystem a tool ships in; the Go template gains `govulncheck`.

## [1.5.0] - 2026-06-26

### Changed

- `gen-contract.js` formats generated Python with ruff and resolves the extension contract next to `contract.json`; tags have three segments from this release on.

## [1.4] - 2026-06-25

### Added

- `contract/contract.json` as the machine-readable single source of the shared contract, with `gen-contract.js`, `spec-files.js`, `sync-spec.js`, `check-spec.js` and the `.agent/SPEC_VERSION` pin.
- `update` drives the package manager for package-managed installs.

### Changed

- Notice severity is `info` or `warning`; Windows self-update renames the running binary aside.

## [1.3] - 2026-06-22

### Added

- The cached update notice appears in `meta.notices`, with severity graded from the CHANGELOG delta.

## [1.2] - 2026-06-21

### Added

- `update` is a single command with a staged failure and interruption contract; `E_IO` (exit 1) and `E_INTERRUPTED` (exit 130).
- Since 1.1: the `common` plus language-overlay template; npm platform-package distribution; update notices; Functional Contract Coverage and release readiness with an enumeration guard; HMAC-keyed, single-use confirm tokens; the keyring credential pattern; the HTTP status mapping, always-on `meta` and offset pagination; real `output_schema` and examples in `reference`; the batch contract (§15); in-process Sigstore verification and `E_INTEGRITY`; single-source versioning; query rules §8.1 to §8.3.

## [1.1] - 2026-06-07

### Added

- Bilingual seed: every spec in English with a Chinese `_zh` translation.

## [1.0] - 2026-06-07

### Added

- The first spec suite (AGENT, CLI-SPEC, SKILL-SPEC, SEC-SPEC, REPO-SPEC) and the degit seed.

[2.0.0]: https://github.com/fatecannotbealtered/ai-native-cli-spec/compare/v1.6.3...v2.0.0
[1.6.3]: https://github.com/fatecannotbealtered/ai-native-cli-spec/compare/v1.6.2...v1.6.3
[1.6.2]: https://github.com/fatecannotbealtered/ai-native-cli-spec/compare/v1.6.1...v1.6.2
[1.6.1]: https://github.com/fatecannotbealtered/ai-native-cli-spec/compare/v1.6.0...v1.6.1
[1.6.0]: https://github.com/fatecannotbealtered/ai-native-cli-spec/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/fatecannotbealtered/ai-native-cli-spec/compare/v1.4...v1.5.0
[1.4]: https://github.com/fatecannotbealtered/ai-native-cli-spec/compare/v1.3...v1.4
[1.3]: https://github.com/fatecannotbealtered/ai-native-cli-spec/compare/v1.2...v1.3
[1.2]: https://github.com/fatecannotbealtered/ai-native-cli-spec/compare/v1.1...v1.2
[1.1]: https://github.com/fatecannotbealtered/ai-native-cli-spec/compare/v1.0...v1.1
[1.0]: https://github.com/fatecannotbealtered/ai-native-cli-spec/releases/tag/v1.0
