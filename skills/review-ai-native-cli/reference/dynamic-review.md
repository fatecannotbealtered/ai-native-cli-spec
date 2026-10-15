# Dynamic review: build, test and probe

Contents: 1. Set up · 2. Run the tool's tests · 3. Discovery probes · 4. Usage probes · 5. Read probes · 6. Write probes · 7. Schema validation · 8. Record and clean up

Use bash (Git Bash on Windows). `<tool>` is the binary, `<PREFIX>` the tool's env prefix (`App.EnvPrefix`, for example `GITLAB_CLI`), `$SKILL` this Skill's directory. `probe` captures stdout, stderr and the exit code of one call and checks the envelope against the tool's own `contract/contract.json`: key sets, `schema_version`, `meta`, code ↔ exit ↔ `retryable`, required details, `dispatch_state`, snake_case keys, size budgets, and the token and operation-id formats. A `FAIL` line is a lead for a finding; a `~ WARN` line needs judgement.

Some output is built by aicli, not by the tool: the built-in commands, usage errors, gate refusals and conflicts, the envelope around previews and receipts, and `FromHTTP`'s mapping. When such output breaks the pinned spec while the vendored library is unedited (static-review.md §1), the defect traces to aicli at the pin: report it as Info for the spec repo, not as a finding against the tool.

## 1. Set up

```bash
cd <tool-repo>
SKILL=<this Skill's directory>
SCRATCH=$(mktemp -d); command -v cygpath >/dev/null && SCRATCH=$(cygpath -m "$SCRATCH")   # a path native Windows programs read too
mkdir -p "$SCRATCH"/{bin,out,state,home}
go build -o "$SCRATCH/bin/<tool>" ./cmd/<tool>         # or the Makefile's build target; <tool>.exe on Windows
# Python: python -m venv "$SCRATCH/venv" && "$SCRATCH/venv/bin/pip" install -q -e .   (Scripts/ instead of bin/ on Windows)
export TOOL="$SCRATCH/bin/<tool>" OUT="$SCRATCH/out" CONTRACT="$PWD/contract"
export <PREFIX>_STATE_DIR="$SCRATCH/state"      # key, journal and default scope path go to scratch
unset <PREFIX>_SCOPE_FILE                       # no grant inherited from the shell
. "$SKILL/scripts/probe.sh"
```

- Build the working tree, never a published release. A build failure is a Major finding: continue static-only and say why.
- The scratch state directory is what keeps the user's confirm key, journal and scope file out of reach. Credentials still come from the user's normal configuration, so read probes may reach the real upstream; that is fine for reads.
- When the tool keeps its configuration inside the state directory, probes report missing configuration: assert that failure's shape (`E_AUTH` or `E_CONFIG`, exit 4) and continue.

## 2. Run the tool's tests (VER-001–VER-004, REPO-073)

Run them in a subshell with a scratch home and without the tool's variables, so no test can reach the user's state or live credentials:

```bash
export GOMODCACHE="$(go env GOMODCACHE)" GOCACHE="$(go env GOCACHE)" GOPATH="$(go env GOPATH)"   # keep caches when HOME moves
tests() { ( for v in $(compgen -e | grep '^<PREFIX>_'); do unset "$v"; done   # also unset upstream credentials the README names
            HOME="$SCRATCH/home" USERPROFILE="$SCRATCH/home" "$@" ); }
tests go test -count=1 ./... 2>&1 | tee "$OUT/tests.txt"
tests go test -count=1 -v -run Conformance ./... 2>&1 | tee "$OUT/conformance.txt"
# Python: tests python -m pytest -q -rs | tee "$OUT/tests.txt"; tests python -m pytest -v -k conformance | tee "$OUT/conformance.txt"
find "$SCRATCH/home" -type f | head             # a file here means a test used the home directory
```

| You see | It means | Rule, severity |
|---|---|---|
| a failing test | the reviewed tree is red | VER-001, VER-008: Major |
| a failure naming `G00`–`G10` | the gate check failed for a write command | the rule the check cites: Blocker (G00 alone: Major) |
| a failure naming `R01`–`R06` | reference, tree, usage errors, envelopes, built-ins or coverage | the rule the check cites: Major |
| `its binding was not exercised` | a sample input was rejected before the gate, so G03 did not test it | VER-001: Minor; Major when that input changes the effect |
| no conformance test ran | the tool's gate wiring is unproven | VER-001: Major |
| files under `$SCRATCH/home` | a test reached for the user's home state | REPO-073: Major |

The suite runs the App in-process against the fake upstream, so a pass proves the gate for every write command that has a sample. The binary probes below show that `main` wires the same App and that the shipped binary behaves the same.

## 3. Discovery probes (CLI-030–CLI-037, CLI-080)

| # | Run | Expect | Rule |
|---|---|---|---|
| D1 | `probe version --version` | the version of the declared version source (`package.json`), with `spec_version` and `schema_version` | CLI-080 |
| D2 | `rawprobe help --help` | text, exit 0; every command on one line; writes marked as writes | CLI-030 |
| D3 | `probe index reference --index` | ≤ 8000 characters (a `~ WARN` here is a CLI-031 finding), or `data.groups`; then `probe idx-<g> reference --index <group>` for every group | CLI-031, R01 |
| D4 | `probe ref reference`, then `node "$SKILL/scripts/audit-reference.js" "$OUT/ref.json" .` | `spec_version` equals the pin; `schema_version` `"2.0"`; `risk_tier` equals `SECURITY.md`; no `release_readiness`; every `GAP` line checked in the source | CLI-020–CLI-027, CLI-033, SEC-001, VER-005 |
| D5 | the loop below | every `reference --command` ≤ 6000 characters | CLI-032, R01 |
| D6 | `probe ctx context` | `tool`, `version`, `spec_version`, `account`, `host`, `credentials {configured, source}`, `state {dir, available, protection}` with `dir` = the scratch path, `authorization.status` `missing`; no secret, token or hash of one; works offline | CLI-035, SEC-021, SEC-030 |
| D7 | `probe doc doctor` | `ok: true`; checks `credentials`, `connectivity`, `state_store`, `authorization` (warns: no scope), `version`; every `fail` has a `fix`; offline, only `connectivity` fails | CLI-036 |
| D8 | `probe chg changelog --since <an older version>` | only newer entries, as in `CHANGELOG.md` | CLI-037 |
| D9 | `probe ops operation list` | an empty page from the scratch journal | CLI-061, R05 |
| D10 | `probe upd update --check`, only if the tool has `update` | read-only; `status` `available` or `up_to_date`; never a bare `update` | CLI-070 |

```bash
node -e 'const d=JSON.parse(require("fs").readFileSync(process.argv[1],"utf8")).data; for (const c of d.commands||[]) console.log(c.path)' "$OUT/index.json" > "$OUT/paths.txt"
while IFS= read -r p; do probe "cmd-${p// /_}" reference --command "$p" | grep -v '^PASS'; done < "$OUT/paths.txt"
node -e 'for (const f of process.argv.slice(1)) { const n = [...require("fs").readFileSync(f, "utf8")].length; if (n > 6000) console.log("CLI-032 over 6000:", f, n) }' "$OUT"/cmd-*.json
```

With a grouped index, collect the paths from every `idx-<g>.json` instead.

## 4. Usage probes (CLI-005, CLI-012, CLI-021–CLI-023, CLI-053)

| # | Run | Expect | Rule |
|---|---|---|---|
| U1 | `probe u1 <noun> lst`, and a root-level guess such as `probe u1b <plural noun> list` | exit 2 `E_USAGE` `unknown_command` with `did_you_mean` (full paths) and `available`; or the guess accepted | CLI-012, CLI-022, R03 |
| U2 | `probe u2 <noun> list --no-such-flag` | `unknown_flag` with `parameter` and `usage` | CLI-012, CLI-023 |
| U3 | `probe u3 context extra` | `unexpected_argument` | CLI-023 |
| U4 | `probe u4 <noun> list --format raw` on a JSON-only command | `unsupported_format`, as a JSON envelope | CLI-005 |
| U5 | the guess list: a nested noun from the root, a plural, `view` or `show` for `get`, a short flag such as `-p` | accepted, or answered with an exact `did_you_mean` | CLI-022 |
| U6 | a write with both `--dry-run` and `--confirm x` | `E_USAGE` `conflicting_flags`; nothing sent | CLI-053 |

A `TIMEOUT` line from any probe means the command waited for input or ran unbounded with stdin closed (CLI-001, CLI-049).

## 5. Read probes (CLI-040–CLI-048)

Pick the cheapest list and get commands from the index. Without credentials or network, assert the failure shape instead (`E_AUTH` or `E_CONFIG` exit 4, `E_NETWORK` exit 7, `dispatch_state` `not_started`) and record the intended behaviour as Blocked-by-env or Verified-by-source.

| # | Run | Expect | Rule |
|---|---|---|---|
| R1 | `probe r1 <noun> list` | `items`, `count`, `has_more`, a continuation or `continuation_reason` when `has_more`, `scope`, `sort`; ≤ 8000 characters (a `~ WARN` is a finding here); external text listed in `meta.untrusted` | CLI-040–CLI-042, CLI-046 |
| R2 | `probe r2 <noun> list --fields +<field>` and `--fields nope` | the field added; an unknown field is `E_USAGE` `invalid_value` | CLI-040 |
| R3 | `probe r3 <noun> get <ref that does not exist>` | exit 3 `E_NOT_FOUND`, mapped by `FromHTTP`; `dispatch_state` `not_started` (a read sends no state-changing request, CLI-011 at 2.0.0) | CLI-011, CLI-016 |
| R4 | one object by ref, by web URL and by name | the same target; an ambiguous name is `E_AMBIGUOUS` with `candidates` | CLI-048 |
| R5 | `cat "$OUT"/*.err` after the successful probes | empty | CLI-047 |
| R6 | on the fake or a disposable instance: every read command from the index, then an independent read of the state | nothing changed | CLI-025, VER-004 |

## 6. Write probes (CLI-050–CLI-063, SEC-010–SEC-012)

Pick where they run (SKILL.md, Safety):

- **Fake upstream, in-process**: section 2 already ran G00–G10 on every write command with a sample. Read `conformance.txt` and record the G checks as Pass or Fail per command.
- **The binary against a fake or a disposable instance**: run W0–W11 on one ordinary write and, when the tool has one, one dangerous write, each on a probe object the review created.
- **Real upstream only**: with no scope file, run W0, W2 and the dry-run form of W8 on a target that does not exist. The gate refuses before anything is sent: `E_FORBIDDEN` (exit 4, also for `--format text`), and `dangerous_flag_required` (exit 5) for the dangerous command, whose flag is checked before the scope. Record the rest as Verified-by-source from `conformance.txt`.

Count the target's state before and after every step with a read outside the gate (the tool's `get`, the fake's state file, the instance's own API or UI); a refusal must leave it unchanged. If W1, W2, W4, W5, W7, W8 or W9 changes anything, stop the write probes, record a Blocker, tell the user exactly what changed, and restore the target.

After W0, grant a scratch scope for the probe object only (`<target key>` is `details.scope.missing.target` from W0):

```bash
cat > "$SCRATCH/scope.json" <<EOF
{"scope_id":"scp_review","tool":"<tool>","granted_by":"reviewer (scratch)","granted_at":"$(date -u +%FT%TZ)","expires_at":"$(date -u -d '+1 hour' +%FT%TZ)","effects":["change"],"dangerous":true,"targets":[{"key":"<target key>","match":"exact"}]}
EOF
export <PREFIX>_SCOPE_FILE="$SCRATCH/scope.json"   # macOS date: -v+1H instead of -d '+1 hour'
```

Add the command's other effects (`deliver`, `configure`, `artifact`, `persist`) and the matching `destinations` or `outputs` when it needs them (SEC-012). Below, `W` is the write command line (`<noun> <verb> <target> <inputs>`) and `W'` the same with one input changed.

| # | Run | Expect | Rule |
|---|---|---|---|
| W0 | no scope: `probe w0 W` and `probe w0d W --dry-run` | exit 4 `E_FORBIDDEN`; `details.scope` with `status`, `path` (in scratch), `missing`, `grant_example`; `not_started`; nothing changed; no scope file was created | SEC-010, SEC-011, CLI-014 |
| W1 | bare: `probe w1 W` | exit 5 `E_CONFIRMATION_REQUIRED` `confirmation_required`, with `preview`, `confirm_token`, `expires_at`, `operation_id`; nothing changed | CLI-052, G01 |
| W2 | `rawprobe w2 W --format text` | exit 5; nothing changed | CLI-005, CLI-055, G06 |
| W3 | `probe w3 W --dry-run`; `TOK=$(field w3 'e.data.confirm_token')` | exit 0; `preview.changes` say exactly what was asked; `inputs` show secrets as `"***"`; nothing changed | CLI-051, G02 |
| W4 | `probe w4 W' --confirm "$TOK"`; repeat with a flag added and with a flag dropped | exit 6 `E_CONFLICT` `token_mismatch`; `parameter` names the input; nothing changed | CLI-053, CLI-054, G03 |
| W5 | `--confirm` with `$TOK` upper-cased, truncated, or another operation's token | `token_mismatch`, `parameter: "--confirm"`; nothing changed | CLI-055, G05 |
| W6 | `probe w6 W --confirm "$TOK"` | exit per the receipt (0 when verified); `data.receipt.outcome`; `observed` agrees with your independent read; exactly one change | CLI-059, G04 |
| W7 | `probe w7 W --confirm "$TOK"` again | exit 6 `token_used`; no second change | CLI-055, G04 |
| W8 | the dangerous command without `--dangerous`: as a dry-run, then as a confirm of the token from its own `--dangerous --dry-run` | exit 5 `dangerous_flag_required` both times; nothing changed | CLI-056, G09 |
| W9 | dry-run, edit the scope file (add `"notes":"x"`), confirm, then restore it | exit 6 `scope_changed`; nothing changed | CLI-057, G10 |
| W10 | `probe w10 operation show --id "$(field w3 'e.data.operation_id')"` | the record: status `applied`, preview and receipt | CLI-058, CLI-061 |
| W11 | dry-run with the positional, confirm with its flag (or `--body x` against `--body-file` holding `x`) | accepted as the same request | CLI-023, CLI-054 |

- W6 must still work after W4 and W5: the gate checks the inputs before it marks the token used (CLI-057). `token_used` at W6 → Major.
- Lost reply (CLI-015, CLI-060): only when the fake or a fault proxy can drop the reply after the change. Expect `E_OUTCOME_UNKNOWN` (exit 6) with `operation_id` and `lookup`, then `unresolved_operation` on a new dry-run of the same target until `operation resolve`; run `operation show` before anything else. Otherwise read the tool's apply-failure test and record Verified-by-source.
- Batch commands (CLI-063): one dry-run lists every target and per-item operation ids; one token; per-item results and `summary`; any failed item makes the batch `ok: false`.

## 7. Schema validation (VER-002)

`probe` covered the envelope rules. For the schema-level rules, validate the main shapes against the tool's own schemas (`python -m pip install "jsonschema>=4.18" referencing` once):

```bash
V() { python -I "$SKILL/scripts/validate-schema.py" "$CONTRACT/schemas" "$@"; }
V context "$OUT/ctx.json" data; V doctor "$OUT/doc.json" data
V reference "$OUT/ref.json" data; V reference-index "$OUT/index.json" data
V page "$OUT/r1.json" data; V preview "$OUT/w3.json" data.preview; V receipt "$OUT/w6.json" data.receipt
for f in "$OUT"/*.json; do V envelope "$f" >/dev/null || echo "envelope schema: $f"; done
```

A schema failure in output aicli builds is Info for the spec repo (see the top of this file); one in the tool's own data is a finding under the rule the schema encodes. Without Python, rely on `probe` and on the tool's validating test helper (VER-002).

## 8. Record and clean up

- Record every probe: the command line, exit code, the relevant output fragment, the status (Pass, Fail, N/A, Verified-by-source, Blocked-by-env) and the rule. For Verified-by-source and Blocked-by-env, name the command that could not run and why.
- Cross-check with the static review: `reference.risk_tier` against `SECURITY.md` and the effects; `context.version` against the Skill's `metadata.version` and `min_version`, and doctor's `version` check; maturity against the evidence found.
- Restore any probe object on a disposable instance, then `rm -rf "$SCRATCH"`. Nothing in the tool repository may have changed: `git status --short` shows the same as before the review.
