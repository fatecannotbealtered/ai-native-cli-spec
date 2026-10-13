# Static review: read the repository

Contents: 0. Ground rules · 1. Pin and library manifest · 2. Wiring, output, errors and settings · 3. Registry and Meta · 4. Writes through App.Write · 5. No private gate code · 6. Scope handling · 7. The Skill · 8. Verification evidence · 9. Self-update, network and supply chain · 10. Repo files and docs

## 0. Ground rules

- Grade against the target's vendored `.agent/*.md` and `contract/` at its pin; cite rule IDs. Open the rule before citing it: this file names the 2.0.0 rules, and a later pin may refine them.
- Evidence is `file:line` with the line quoted. Read the code around a grep hit before filing it; a hit is a lead, not a finding.
- Enumerate commands from the registry, never from grep hits: the full `reference` from the built binary (dynamic-review.md D4), or, static-only, every `Register(` / `register(` call. Grep then finds the call sites behind each command.
- Channel-bound items (npm wrapper, signed binary releases, GitHub workflows, `LICENSE` for an internal tool) are N/A when the tool does not use that channel; CI on another platform counts when it runs the same steps. The machine contract, the gate and the scope rules always apply.
- `<skill>` is this Skill's directory, `<PREFIX>` the tool's env prefix, `$SCRATCH` a scratch directory outside the repo (dynamic-review.md §1).
- Finish by walking SEC-060's checklist up to the tool's tier: every unticked item should already be a finding.

Exclude vendored files from every grep (add `--untracked` to `git grep` for files not committed yet):

```bash
X=(-- . ':(exclude).agent' ':(exclude)contract' ':(exclude)internal/aicli' ':(exclude,glob)**/_aicli/**' \
   ':(exclude)scripts/spec-files.js' ':(exclude)scripts/sync-spec.js' ':(exclude)scripts/check-spec.js')
NT=(':(exclude,glob)**/*_test.go' ':(exclude,glob)**/test_*.py' ':(exclude,glob)**/tests/**')   # also skip tests
```

## 1. Pin and library manifest (REPO-060–REPO-063, SEC-050, REPO-050)

```bash
PIN=$(tr -d ' \r\n' < .agent/SPEC_VERSION); echo "$PIN"
node scripts/check-spec.js --local-only        # library files against MANIFEST.json, pin, required files
git clone -q --depth 1 --branch "$PIN" https://github.com/fatecannotbealtered/ai-native-cli-spec "$SCRATCH/spec"
node "$SCRATCH/spec/template/common/scripts/check-spec.js" --from "$SCRATCH/spec"   # run in the tool root
git grep -lE 'Vendored from ai-native-cli-spec' -- . ':(exclude)internal/aicli' ':(exclude,glob)**/_aicli/**'
git log --oneline -- .agent contract internal/aicli ':(glob)**/_aicli/**' scripts/spec-files.js scripts/sync-spec.js scripts/check-spec.js
```

- The offline layer passes when someone edits a library file and regenerates `MANIFEST.json`, and the tool's own `check-spec.js` may itself be edited. Only the spec's guard compared with the pinned tag proves the copy is unedited. Air-gapped: ask the user for a checkout or release archive (`--from <archive.tar.gz>`); without one, record Blocked-by-env and read `git log` for commits that touch vendored paths without changing `.agent/SPEC_VERSION`.
- Library, contract, schemas or sync scripts differ from the pinned tag → Blocker (REPO-063: an edited gate is a per-tool gate again). Only `.agent/*.md` text differs → Major.
- A second copy of the library (a hit from the `Vendored from` grep, a top-level `aicli/` left from the overlay), or code importing aicli from anywhere but the vendored path → Blocker (REPO-050: the drift guard checks only the vendored path). Go imports must be `<module>/internal/aicli`; Python imports `._aicli` or `<pkg>._aicli`.
- No pin → Minor (scope step 0). `contract/contract-ext.json` is the only tool-owned file under `contract/` (REPO-064).
- Retired files present (REPO-001): `cmd/fcc_guard_test.go`, `tests/test_fcc_guard.py`, `internal/contract/contract_gen.go`, `<pkg>/contract_gen.py`, `release_readiness` in code or docs → Major; old confirm code that still runs → §5.

## 2. Wiring, output, errors and settings (CLI-001–CLI-018, CLI-047, CLI-100–CLI-102, VER-001)

```bash
git grep -nIE 'fmt\.Print|Fprint(f|ln)?\(os\.Stdout|os\.Stdout\.Write|\bprint\(|click\.echo|sys\.stdout\.write' "${X[@]}" "${NT[@]}"
git grep -nIE 'os\.(Getenv|LookupEnv|Environ)\(|os\.environ|os\.getenv\(' "${X[@]}" "${NT[@]}"
git grep -nIE 'strings\.(Contains|HasPrefix)\([^)]*(err|Error\(\)|[Mm]essage)|in str\((e|err|exc)\)' "${X[@]}" "${NT[@]}"
git grep -nIE 'os\.Exit\(|sys\.exit\(' "${X[@]}" "${NT[@]}"
git grep -nIE 'App\.Settings|Settings:|settings=|\.Setting\(|\.setting\(|settings\.json|_SETTINGS_FILE\b|config\.json|_PROFILE\b|"profile"|"verbose"|"debug"|--verbose|--debug|Debugf|\.debug\(' "${X[@]}" "${NT[@]}"
```

| Check | Rule | If wrong |
|---|---|---|
| `main` runs `app.Execute(os.Args[1:])` (Python: the App's entry) and exits with its code; the tree is built in a function per App, not in package globals or `init()` | CLI-001, VER-001 | Major; Blocker if a second entry point bypasses the App |
| no stdout write outside aicli, which also renders `--help`; progress goes to stderr | CLI-001 | Blocker when reachable in json mode |
| environment read through `App.Env` / the app's env hook | VER-001 | Major (suite isolation); Blocker when the value shapes a write (§4) |
| upstream errors mapped by status through `FromHTTP` (`from_http`), never by message text | CLI-016 | Major |
| no exit-code table or `os.Exit` of the tool's own outside `main` | CLI-010 | Major; Blocker if code ↔ exit ↔ `retryable` disagree with `contract.json` |
| extension codes only in `contract-ext.json`: `^E_[A-Z0-9][A-Z0-9_]*$`, not a core name, `exit`, `retryable`, `meaning`; exit 9 only for human action; declared to the App (`ExtCodes`) | CLI-018, REPO-064 | Major |
| messages: one sentence naming the constraint and the next step; no input values or upstream text (sample five constructors) | CLI-017 | Minor; Major if they echo secrets or upstream text |
| no spinners, banners or progress on stderr when it is not a terminal | CLI-047 | Minor |
| `message`, `next_action`, notices and help in English whatever the locale; upstream text passed through untranslated | CLI-017 | Minor |
| settings declared in `App.Settings` and read through `App.Setting` (`app.setting`); no config loader of the tool's own, and a 1.x `config.json` migrated into `settings.json`, never read; the settings file written only by a person or a gated `configure` command | CLI-100 | Major; Blocker when the tool writes a secret into it |
| several accounts or hosts: named profiles in the settings file and aicli's `--profile`; no profile or account-switching flag of the tool's own | CLI-101 | Major |
| diagnostics only through `App.Debugf` (`app.debug`) under `<PREFIX>_DEBUG`, on stderr; no `--verbose` or `--debug` flag | CLI-102 | Minor; Blocker when a secret reaches the debug output |

## 3. Registry and Meta (CLI-006, CLI-020–CLI-029, CLI-038, CLI-040–CLI-048, CLI-081, SEC-001, SEC-002, SEC-021, VER-005)

With a built binary, `node <skill>/scripts/audit-reference.js "$OUT/ref.json" .` lists the gaps below per command. Static-only: `git grep -nE '\.Register\(|\.register\(' "${X[@]}"` and read each `Meta`. Compare the registry with the cobra or click tree: every leaf needs `Register` (CLI-020; an unregistered leaf is missing from help, `reference`, the Skill map and coverage) → Major.

| Meta field | Check | Rule | If wrong |
|---|---|---|---|
| `Effects` | matches what the code does (§4): upstream change `change`, send or share `deliver`, permissions, settings, credentials or profiles `configure`, new local file `artifact`, overwritten file `persist` | CLI-025 | Blocker for a write declared `observe`; else Major |
| `Dangerous` | irreversible or mass changes (delete, merge, drop, purge, bulk) declared; `deliver` and `configure` left dangerous | CLI-025, SEC-001 | Major |
| `App.RiskTier` | at least the floor of the effects (T1 change/artifact/persist, T2 deliver/configure or irreversible damage); equal to `SECURITY.md` and `reference.risk_tier` | SEC-001 | Major |
| `Secrets` | every credential-like flag (token, password, key, masked value) | SEC-021, CLI-024 | Blocker if a value reaches output, a preview, the journal or logs; else Major |
| `FileInputs` | every `--<x>-file` flag mapped, its content read with `ReadFileInput` (`read_file_input`) | CLI-048, CLI-054 | Blocker if a write reads the file itself (§4) |
| `Positional` | an identifying positional binds to its flag | CLI-023 | Minor |
| `Output`, `App.Schema` | the schema exists; `Untrusted` lists every field of external text (titles, bodies, comments, names, upstream messages) | CLI-020, CLI-046, SEC-002 | Major |
| `DefaultFields` | the fields users ask about, not the API's | CLI-040 | Minor; Major when common questions need an extra call |
| `ErrorCodes` | codes the command can return | CLI-026 | Minor |
| `Examples` | writes show the dry-run and confirm pair; dangerous ones carry `--dangerous` | CLI-030, R02 | Major |
| `Maturity`, `Evidence` | `<kind>:<path>[#anchor]` references that resolve; `experimental` needs `simulated:`, `verified` a current `live:` record, `stable` also `eval:`; fake-only evidence caps at `experimental` | CLI-027, VER-005 | Major |
| `BatchFlag` | multi-target writes take one plural flag and use `WriteBatch` | CLI-063 | Major |
| guesses | `Shortcuts` make nested nouns reachable from the root; `ShortFlags`, `FlagAliases`, `FlagSynonyms`, `VerbSynonyms`, `Aliases` hold the tool's guess list; a test runs every entry | CLI-022 | Major for unreachable nouns; Minor otherwise |
| `Deprecated`, `DeprecatedFlags` | a command or flag on its way out names its replacement; nothing was removed outside a MAJOR release (compare `CHANGELOG.md` with the previous release's `reference`) | CLI-081 | Major for a removal in a MINOR or PATCH release; else Minor |

Also:

- Reads: lists return through `app.List(Page{…})` with a true `Scope` that names narrower coverage ("direct members only", a client-side filter over one page) → a false completeness claim is a Blocker (CLI-041). Counting, grouping and finding the latest happen in the tool (CLI-044, Major). Heavy bodies, logs and diffs are separate commands (CLI-043, Minor); rich text comes back as Markdown (CLI-045, Minor). A streaming command emits one envelope per line with `type`, ending in a `summary` line (CLI-006, Major).
- Flags use the shared vocabulary where the concept exists: `--limit` with a stated default and maximum, `--cursor` or `--offset` copied from the page, `--query`, `--since`/`--until`, `--sort`/`--order`, `--state` with `all`, `--refs`, `--wait`, `--output`, `--input`, `--profile`, `--idempotency-key` (CLI-028) → Minor. Verbs keep their meaning (CLI-029): a `list` that fails on no match → Major; an `update` that clears fields the call did not name, or a `delete` that reports success for a missing target → Blocker (an unintended write or a false report).
- Expiring credentials are renewed during the call, or the error is `E_AUTH` naming the sign-in step, or `E_HUMAN_REQUIRED` with `action: sign_in` (CLI-038) → Major. Sign-in commands are `auth login` and `auth logout` (`configure`) and `auth status` (a read that checks the credential upstream and never prints a secret) (CLI-101) → Major.
- Refs, URLs and names resolve to one target; an ambiguous name is `Ambiguous(...)` with candidates, never the first match (CLI-048): look for `[0]` or `first` after a search by name → Major.
- Hooks: `Identity` resolves account and host locally and redacted (CLI-035); `DoctorChecks` adds `credentials` and `connectivity` (CLI-036); `ContextExtra` holds no secrets (SEC-021); `doctor`'s `version` check reads the installed Skill (`App.SkillDirs`, Python `skill_dirs`; default the standard agent Skill directories), and a hand-set `MinSkillVersion` (`min_skill_version`) is only a fallback (CLI-036, CLI-080) → Major when the check is fed a hand-kept constant instead of the installed Skill.
- Built-ins (`reference`, `context`, `doctor`, `changelog`, `operation …`, `update`) are aicli's: do not grade their maturity against the tool; report library anomalies as Info for the spec repo.

## 4. Writes through App.Write (CLI-050–CLI-065, SEC-012, SEC-020)

1. Every write command from the registry (`type: write`) returns `app.Write(cmd, aicli.WriteSpec{…})` or `app.WriteBatch(…)` (Python `app.write(` / `app.write_batch(`) on every path that changes something → otherwise Blocker (CLI-050).
2. Find every state-changing call outside the library and tests, then confirm each is reached only from an `Apply` (self-update is aicli's built-in, so update code in the tool is a §9 finding, CLI-070):

   ```bash
   git grep -nIE 'http\.Method(Post|Put|Patch|Delete)|"(POST|PUT|PATCH|DELETE)"|http\.Post(Form)?\(|requests\.(post|put|patch|delete)\(|\.(post|put|patch|delete)\(|method=.?(POST|PUT|PATCH|DELETE)' "${X[@]}" "${NT[@]}"
   git grep -nIE '\.(Create|Update|Delete|Remove|Add|Set|Send|Share|Merge|Approve|Close|Reopen|Publish|Upload|Move|Rename|Grant|Revoke)[A-Za-z]*\(' "${X[@]}" "${NT[@]}" \
     | grep -vE '\.(AddCommand|AddGroup|SetResolved|Set(Args|Out|Err|In|Context|HelpFunc|UsageFunc|VersionTemplate))\('
   git grep -nIE 'os\.(WriteFile|Create|OpenFile|Rename|Remove(All)?|Symlink)\(|ioutil\.WriteFile|open\([^)]*["'"'"'][wax]b?\+?["'"'"']|\.write_(text|bytes)\(|shutil\.(copy|move|rmtree)' "${X[@]}" "${NT[@]}"
   ```

   A client with one generic request function: list its callers that pass a non-GET method. A call reachable from a read command's handler or from `Preview` → Blocker (CLI-025, CLI-050: the gate, scope and journal are skipped). Local file writes need `artifact` or `persist` and run inside `Apply`; the tool's own config or credential files are written by a `configure` command, through the gate.
3. `Preview` only reads. It sets `BaseVersion` and `BaseSource` when the upstream has a version (`updated_at`, a sha, an ETag) → Major when missing (CLI-057 cannot see drift). A `deliver` preview lists its destinations and an `artifact`/`persist` preview its outputs (SEC-012). A write that cannot apply returns `CanApply: false` with `BlockedBy` (CLI-051, Minor).
4. Every input that shapes the request arrives as a declared flag or positional, a file input read through `ReadFileInput`, or the `Identity` (SEC-020, CLI-054). Inside the handler, `Preview` or `Apply`, look for environment or config reads, `os.ReadFile`/`open` on a flag's path, and defaults computed at apply time (`time.Now()`): any of them shaping the request → Blocker (the token does not bind it). A name resolved to an ID inside `Preview` is resolved again at confirm; where that mapping can change in between, resolving before `app.Write` and binding the resolved value (Go: `aicli.SetResolved`) is better → Advisory.
5. `Target.Key` is the canonical ref after URL, name and alias resolution, one spelling per object (SEC-012); raw user input as the key → Major (a second spelling escapes the scope's selectors).
6. `Apply` classifies failures by what reached the upstream (CLI-015): `NotDispatched` only before anything left the machine (DNS, refused connection, TLS); `Rejected` only for an answer that proves nothing changed (a 4xx, 429 included, through `FromHTTP`); timeouts, resets and 5xx after sending stay unwrapped, so the gate reports `E_OUTCOME_UNKNOWN`. A wrong wrapper → Blocker. An HTTP client that retries POST, PUT, PATCH or DELETE → Blocker (one request, two changes).
7. Receipts (CLI-059): `Observed` comes from the write's response or a re-read, never from the request's own values. `Verification` is left empty, so aicli compares `observed` with the preview's changes, or computed from the read-back; a hard-coded `passed` skips that comparison. A deliver command reports `Delivery`. A faked read-back or a hard-coded verdict → Blocker (false verification).
8. Batches (CLI-063): `WriteBatch` with `Meta.BatchFlag`; no loop that calls `Write` per item or rolls item results into its own `ok` → Major.
9. Idempotency (CLI-062): create-type writes accept `--idempotency-key` and send `op.IdempotencyKey` upstream → Minor when missing.
10. Long-running work (CLI-064): a gated submit whose `data.result.job` is `{ref, status, poll}`, and a status read with `--wait` that never resubmits → Major when a submit blocks past the time limit or a status command can resubmit.
11. Human action (CLI-065): `HumanRequired(action, resume, …)` instead of waiting or prompting → Major.

## 5. No private gate, token, key or secret code (CLI-050, CLI-055, SEC-020, SEC-021, SEC-030, SEC-040)

```bash
git grep -nIiE 'crypto/hmac|\bhmac\.|hashlib|confirm[_-]?(token|secret|key)\b|confirm\.(key|secret)|consumed|single[_-]?use|LEGACY_CONFIRM|newConfirmToken|validateConfirmToken|requireConfirm|prepareWrite|bindInvocation|dangerousFlagRequired' "${X[@]}"
git grep -nIE 'Flags\(\)\.[A-Za-z]+\("(dry-run|confirm|dangerous)"|option\(\s*["'"'"']--(dry-run|confirm|dangerous)' "${X[@]}"
git grep -nIE 'sha256\.(Sum256|New)\(|hashlib\.sha256\(|md5' "${X[@]}" "${NT[@]}"
```

- A token, binding, key, journal or consumed store of the tool's own, or gate flags the tool defines itself → Blocker (CLI-050).
- An environment variable, flag or build tag that weakens binding or skips the gate, test-only switches included (`<PREFIX>_LEGACY_CONFIRM_BINDING`) → Blocker (CLI-055, VER-008).
- Tests that mint tokens from hand-built payloads → Major (MIGRATION-2.0 step 5). Tests that read `data.confirm_token` from a dry-run are fine.
- Old state files still read for gating (`confirm.secret`, `confirm-consumed.json`) → Blocker; read only for migration → Minor.
- An unsalted hash of a secret, whole or truncated, in output, a preview, the journal or an error → Blocker (SEC-021); a keyed HMAC is fine. Secrets in log lines → Blocker.
- Credentials at rest (SEC-040): secrets live in the OS keyring or come from environment variables; config files hold none (read the code that writes them; aicli's settings file refuses a secret setting, CLI-100); a file fallback reports `encrypted_file` in `context.credentials.source`; secret flags are not the documented path; commands that store or change credentials or profiles declare `configure` → Major; a secret written to a config file in plain text → Blocker.

## 6. Scope handling (SEC-010–SEC-013, SKL-006, REPO-010, REPO-011)

```bash
git grep -nIE 'scope\.json|_SCOPE_FILE|scope[-_]?file|scope_id|granted_by|grant_example' "${X[@]}"
```

- The tool never creates, edits, moves or deletes the scope file, and offers no command or flag that does (`scope grant`, an `init` that writes `scope.json`, `--scope-file`) → Blocker (SEC-011). It never reads a scope file itself or skips the library's check → Blocker (SEC-010).
- README, `SECURITY.md`, `AGENTS.md` and the Skill never tell an agent to create or edit the scope file → Blocker if they do (REPO-011, SKL-006: it undoes SEC-010).
- README has the Authorization section (REPO-010) and `SECURITY.md` gives the path (`<PREFIX>_SCOPE_FILE`, else `<state_dir>/scope.json`), a minimal grant, how to withdraw it, and the SEC-013 statement → Major when missing; Major when either calls a scope or token proof of human approval (SEC-013).
- The conformance `Harness.Scope` grants every effect the writes declare, plus the `destinations` and `outputs` the samples need; nil (every effect, no destinations or outputs) fits only a tool without `deliver`, `artifact` or `persist` commands (VER-001).

## 7. The Skill (SKL-001–SKL-011, SEC-013)

```bash
node <skill>/scripts/audit-skill.js skills --tool <tool> --version "$(node -p 'require("./package.json").version')" --index "$OUT/index.json"
git grep -nE '\{\{[A-Z_0-9]+\}\}' -- . ':(exclude)node_modules'      # leftover template placeholders
```

`GAP` lines break a rule as written; `LEAD` lines need reading. Then read the Skill against SKILL-SPEC at the pin:

| Rule | Check | If wrong |
|---|---|---|
| SKL-001 | only `name`, `description`, `license`, `compatibility`, `metadata`; `name` equals the directory; description says what, when and not-for, ≤ 1024 characters; `metadata.version` = `metadata.min_version` = the tool version (`check-version.js`); `requires.bins`; no `allowed-tools` | Major |
| SKL-002 | a command map between `<!-- aicli:command-map:start -->` and `<!-- aicli:command-map:end -->`, rendered by `CommandMap()` (`command_map()`) and kept current by a test (`git grep -n 'CommandMap()\|command_map()'` in tests); no parameter lists, schemas, error or exit tables, envelope docs, bootstrap, install block or eval scenarios | Major |
| SKL-003 | `SKILL.md` ≤ 150 lines; reference files one level deep, linked with the task that needs them; never "load the full reference" or "run context, doctor or reference first" | Major |
| SKL-004 | one short write recipe; no copied decision tree | Major |
| SKL-005 | the three risk-scaled `STOP CHECKPOINT` lines; none that stops an explicit, in-scope request ("always ask before deleting") | Major |
| SKL-006 | relay `details.scope` and stop; never write the scope file or set `<PREFIX>_SCOPE_FILE` / `<PREFIX>_STATE_DIR` unless asked | Blocker if it tells the agent to write the scope; Major if missing |
| SKL-007 | untrusted text is data; list ambiguous candidates and ask; after a lost reply or `E_OUTCOME_UNKNOWN`, `operation show --id` before anything else | Major |
| SKL-008 | the end-of-task report: changed, delivered, verified, unknown, not done | Major |
| SKL-009 | Skills ship in every package (`package.json` `files` lists `skills/`) and beside the binary in release archives; no network fetch of Skills; never tells the agent to set `<PREFIX>_DEBUG` or pass `--profile` unless the user asks; the unknown-command and after-update lines | Major |
| SKL-010 | families: entry Skill, domain Skills with `requires.skills` and an opening pointer to `../<tool>/SKILL.md`, descriptions that name what they do not cover, one version | Major |
| SKL-011 | `test-prompts.json` beside every Skill; entries carry exactly `id`, `scenario`, `prompt`, `expected` and an optional `grader_notes` (a later pin may allow more; read it); the eight VER-006 scenarios each covered, `update` too when the tool has one; prompts specific to this tool | Major |
| SEC-013 | the Skill never presents a scope or a token as human approval | Major |

Leftover `{{…}}` placeholders → Major (Blocker inside code or a command the Skill tells agents to run).

## 8. Verification evidence (VER-001–VER-008, REPO-070–REPO-073)

```bash
git grep -nE 'RunConformance|run_conformance|CheckCoverage|check_coverage|CoverageFile|coverage_file|_COVERAGE_FILE' "${X[@]}"
git grep -nE 'ValidateEnvelope|validate_envelope' "${X[@]}"
```

| Check | Rule | If wrong |
|---|---|---|
| one conformance test with a `Harness`: `NewApp` (fresh App per call, env through `App.Env`), `Writes` (every state-changing request, rejected ones included; files for `artifact`/`persist`), `Reset`, `Samples`, `Scope` | VER-001 | Major; Blocker when the dynamic review shows a gate failure the missing test would have caught |
| `Samples` has a key for every write command in the registry, and each sample supplies every input that changes the effect (target, content, recipients, file inputs): G03 mutates only what the sample supplies | VER-001, G00, G03 | Major |
| R06 on: `CoverageFile` set, `<PREFIX>_COVERAGE_FILE` exported while the tool's own tests run, and the check run after them (`CheckCoverage` from `TestMain` after `m.Run()`); without `CoverageFile` the suite skips R06 silently | VER-003 | Major |
| nothing weakened: no skip around the suite, no filtered samples, no `-short` or `-run` filter in CI's test step | VER-001 | Major |
| tests run the CLI through one helper that validates every envelope and the exit code | VER-002 | Major |
| behaviour matrix per command, subtests named by row, `not applicable:` skips; sample two or three commands, including the apply-failure row (unknown outcome, then `operation show`, nothing resent) for a write | VER-004 | Major for a missing write row; Minor for a read |
| evidence files exist and a test walks `reference` to check them; `stable` commands cite an eval report (plan, results, comparison, noise check, decision) | VER-005, VER-006, REPO-072 | Major |
| `ci.yml` and `release.yml` run the same tests with conformance, `check-spec.js`, `check-version.js`, lint and format, and the dependency audit of every shipped ecosystem (`govulncheck` or `pip-audit`, plus `npm audit` for the wrapper); publish jobs `needs:` them | VER-008, REPO-070, REPO-071 | Major |
| tests point the state directory at a temporary directory (`StateDir: t.TempDir()`, `<PREFIX>_STATE_DIR`) | REPO-073 | Major |

## 9. Self-update, network and supply chain (CLI-070, CLI-071, CLI-110, SEC-022, SEC-050, SKL-009, REPO-041–REPO-043)

`update` is aicli's built-in, turned on by `App.Update` (CLI-070). Read the code; never run a bare `update`.

```bash
git grep -nIE 'UpdateConfig|App\.Update|Update:|update=' "${X[@]}" "${NT[@]}"
git grep -nIE 'ParseVersion|parse_version|CheckUpdateTarget|check_update_target|PackageManagerArgs|package_manager_args|RecordUpdateCheck|record_update_check|sigstore|cosign|skill_sync|releases/(latest|tags)|api\.github\.com' "${X[@]}" "${NT[@]}"
git grep -nIE 'exec\.Command(Context)?\([^)]*"(sh|bash|cmd|cmd\.exe|powershell)"|shell=True|os\.system\(|Sprintf\([^)]*(npm|pnpm|yarn|pip|brew|go install)|npx skills(@[^ ]+)? add|git clone|curl ' "${X[@]}" "${NT[@]}"
git grep -nIE 'InsecureSkipVerify|verify *= *False|CERT_NONE|_create_unverified_context|NODE_TLS_REJECT_UNAUTHORIZED' "${X[@]}" "${NT[@]}"
git grep -nIiE 'sentry|segment\.io|posthog|mixpanel|amplitude|google-analytics|telemetry' "${X[@]}"
```

- The tool has no update code of its own: a command, flag or code path outside aicli that downloads releases, checks signatures, swaps the binary or syncs Skills → Major (it bypasses the tested built-in); Blocker when it skips verification, builds a shell command or lets an unvalidated version reach a command line (a crafted version reaches `npm.cmd`).
- `App.Update` (Python `update=`) names the repository whose release workflow signs `checksums.txt`, `Workflow` matches that workflow's file name, and each package name equals the published one (`package.json` `name`, the Go main package, the PyPI distribution, the brew formula) → Blocker for a package name that is not the tool's (an update would install another package); Major for a wrong repository or workflow (every binary update fails with `E_INTEGRITY`).
- Release archives are named `<tool>-<version>-<os>-<arch>.tar.gz` (`.zip` on Windows) and hold the binary and `skills/` at their root, beside `checksums.txt` and `checksums.txt.sigstore.json` (REPO-042, REPO-043) → Major (binary updates fail, or skip the Skill sync).
- The README's install block installs the Skills from the installed package with a pinned installer (`npx skills@<version> add "$(npm root -g)/<pkg>" -y -g`); `npx skills add <repo>`, an unpinned installer or any fetched branch → Blocker (SKL-009, SEC-050).
- Update notices come from the local cache only, and only `update` and `doctor` contact the release source (CLI-071) → Minor.
- The upstream client (CLI-110): every request has a timeout (by default at most 30 s); only idempotent reads retry, at most twice, honouring `Retry-After` up to 10 s; `HTTPS_PROXY`, `HTTP_PROXY` and `NO_PROXY` are honoured; a private CA comes from `<PREFIX>_CA_FILE`; a User-Agent is sent. A switch that turns TLS verification off → Blocker; a request without a timeout → Major; a retried write → Blocker (§4 item 6).
- Outbound contact (SEC-022): telemetry, analytics or crash reporting → Major, Blocker when it sends request content, identifiers or secrets; a destination other than the configured upstream and the release source that `SECURITY.md` does not list → Minor.
- Releases sign `checksums.txt` (`cosign sign-blob --new-bundle-format`), publish npm with provenance and build from tags (REPO-042, SEC-050) → Major; N/A for channels not used.
- Launcher budget: CI measures the start-up overhead, and the README names the native binary path when the launcher misses 100 ms (REPO-041, CLI-090) → Minor.

## 10. Repo files and docs (REPO-001, REPO-010, REPO-011, REPO-020, REPO-030–REPO-033, REPO-040, REPO-043, REPO-051, REPO-074)

- REPO-001 files present, with conditional ones only where their condition holds; retired files absent (§1). A missing MUST file → Minor, or Major for `SECURITY.md`, `AGENTS.md`, `CHANGELOG.md` and the conformance test.
- README sections in REPO-010 order, including Authorization, Configuration (settings, the settings file and its profiles) and Uninstall (the package or binary, the state directory, the keyring entries, a settings file kept elsewhere, the Skill directories); the install block runs as pasted, installs the Skill of the same version and runs no `context`, `doctor` or `reference` first (CLI-034); no copied flag or schema tables (REPO-011) → Minor; Major for a bootstrap or a missing Authorization section.
- `SECURITY.md` states the risk tier and its worst case in one sentence (SEC-001) → Major when missing.
- One version source: `node scripts/check-version.js` passes; tests read the version at run time; `changelog.go` or `changelog.py` embeds `CHANGELOG.md`, and no `release-notes.md` is committed (REPO-031, REPO-032) → Major for a mismatch.
- npm wrapper: `files` lists `skills/`; no install script downloads anything; binaries are not committed (REPO-040, REPO-043, SEC-050) → Major.
- Bilingual pairs link to each other and keep the same headings (REPO-020) → Minor.
- Exactly one language overlay (not both `.golangci.yml` and `ruff.toml`); `go.mod` pins one Go version that workflows read with `go-version-file`; `release.yml` runs on `v*` tags; one formatter per language and a PR template with the self-check (REPO-051, REPO-033, REPO-074) → Minor.
