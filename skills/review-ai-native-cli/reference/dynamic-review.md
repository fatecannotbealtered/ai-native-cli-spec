# Dynamic review — build and probe the real binary

Contents: 1. Build · 2. Safety rules · 3. Probe matrix · 4. Envelope assertions ·
5. Write-path probes · 6. Recording results

## 1. Build

Build from the repo's own entry points — never `go install` / `pip install` a
published version (you must test the working tree):

- Go: `make build` if a Makefile exists, else `go build ./cmd/<tool>`.
- Python: `pip install -e .[dev]` into a scratch venv, or run the module entry point.
- Build failure = a Major finding by itself; degrade to static-only and say so.

Use the built binary via an explicit path (`./bin/<tool>` or venv), not one on PATH.

## 2. Safety rules (repeat before probing)

- Default probes are read-only. `context`, `doctor`, `reference`, `changelog`, `--version`, `--help` are always safe.
- Write probes (MUST — applies to all of P9–P11): only use **update/delete-style** commands aimed at **fabricated, nonexistent IDs**. Remember the confirm gate itself is what you are testing — assume it may be missing, so the target must be unmutable even if every gate fails. If the tool's only write commands are create/send-style (send mail, post, broadcast — no "nonexistent target" exists), skip P9–P11 as N/A unless the user explicitly points at a disposable environment.
- Never pass `--dangerous`. Never run bare `update` (it would replace the binary and sync skills). `update --check` / `update --dry-run` are read-only per CLI-SPEC §14 and safe.
- Confirm tokens: a **fabricated** token (P10) is safe by design — the contract requires its rejection. A token returned by a real dry-run is live ammunition: never send it in any probe, discard it unused. "Never confirm" means never a *real* token.
- No credentials configured → run the probe anyway and assert the failure shape (`E_AUTH`/`E_CONFIG`, exit 4, valid envelope). The error path is part of the contract. Only mark N/A what genuinely cannot be exercised.
- **Upstream reachability preflight.** An unreachable upstream is the *normal* reviewing condition, not an edge case — you often grade a repo whose backend you cannot reach (VPN, internal net, no creds). Before the semantic probes, establish reachability once (P2 `context` / P8 both reveal it): if commands that hit the API return `E_NETWORK`/`E_TIMEOUT` (exit 7/8), the network layer is failing **before** target logic runs. When that happens, a probe cannot positively observe its intended semantics (NOT_FOUND, CONFIRMATION_REQUIRED, pagination) — the network error arrives first. Do **not** call that Pass (you did not see the behavior) or plain Fail (the tool isn't wrong). Follow the degraded-verification protocol in §4, and record such probes as **Verified-by-source** or **Blocked-by-env** (§6), never silently as Pass.
- Timebox every probe (e.g. `timeout 30`) — a hanging command is itself a finding (non-interactive violation, CLI-SPEC §1 rule 4). When a probe times out, distinguish *slow* from *waiting for input* before filing: re-run with stdin closed (`< /dev/null`, or `< NUL` in cmd). Exits immediately with a read/EOF error → it was prompting (Blocker, non-interactive violation). Still runs → slow command or network wait (report the timeout itself, likely Minor unless it violates a documented bound). Prompts hiding behind a TTY check won't fire in either run — also grep stderr captures for prompt fragments (`password:`, `[y/N]`, `continue?`).
- Shell note: examples assume a POSIX shell (on Windows use Git Bash, or adapt — PowerShell reads exit codes via `$LASTEXITCODE`, and its `timeout` is a *wait* command). `jq` is convenient but optional; any strict JSON parser works for the assertions.
- Capture-file path: write probe artifacts to a **repo-relative** scratch dir (e.g. `mkdir -p .review-probes && … 1>.review-probes/ctx.json`), not `/tmp` or `$TEMP`. On Windows those resolve to different namespaces depending on who reads them — a Git Bash `/tmp` path handed to a native-Windows `node`/`jq` process fails to resolve, and `$TEMP` is not stable across shell invocations. A path under the repo is seen identically by every tool. Add the scratch dir to your cleanup at the end (and it should already match `.gitignore` patterns, but never commit it).

## 3. Probe matrix

Run each; capture stdout and stderr separately into the repo-relative scratch dir, and record the exit code (`… 1>.review-probes/ctx.json 2>.review-probes/ctx.err; echo $?`). Wrap with `timeout 30` per §2.

| # | Probe | Expect | Spec |
|---|-------|--------|------|
| P1 | `<tool> --version` | version string; matches the repo's declared version source (package.json for npm-shell repos, else the manifest identified in static §4) | §14 |
| P2 | `<tool> context --compact` | envelope; `data.version`; `credentials` as boolean/summary, no secrets | §11 |
| P3 | `<tool> doctor --compact` | envelope; `checks[]` each with `check`/`status`/`fix`; includes `release_readiness` check | §11 |

> P3 "must not fail on network" — scope note: §11 says a network failure must not make `doctor` *fail by itself*, but does not define whether "fail" means the **envelope** (`ok:false`) or the **process exit code**. Grade what the spec clearly requires: the envelope MUST stay `ok:true` with the failing probe honestly encoded in `checks[]` (`network:fail` + actionable `fix`). If the envelope is honest but the process still exits non-zero purely because the network probe failed, record it as at most a Minor (borderline; the machine-readable contract itself conformed) and note the spec ambiguity — do not escalate to Blocker/Major on the exit code alone.
| P4 | `<tool> reference --compact` | envelope; `release_readiness.level`; every command has non-stub `output_schema` resolving into `schemas{}` and ≥1 example; exit_codes table matches contract.json | §11, §3.1 |
| P5 | `<tool> changelog --since <older-version>` | envelope; entries strictly newer than `--since`; content matches CHANGELOG.md | §11 |
| P6 | `<tool> reference --fields <a,b>` | only requested fields in `data` | §8 |
| P7 | unknown command / bad flag | `E_USAGE`/`E_VALIDATION`, exit 2, failure envelope on stdout | §6 |
| P8 | a read command with a nonexistent ID | `E_NOT_FOUND`, exit 3 (or auth failure if no creds: `E_AUTH`/`E_CONFIG`, exit 4) | §6 |
| P9 | a write command (update/delete-style, fabricated ID) with no `--dry-run`/`--confirm` | `E_CONFIRMATION_REQUIRED`, exit 5; nothing mutated | §7 |
| P10 | same write with `--confirm ct_fabricated_0000` (a made-up token, run this **before** any real dry-run so no live token exists yet) | `E_CONFLICT` (exit 6) or `E_CONFIRMATION_REQUIRED` (exit 5); **must not execute** | §7 |
| P11 | same write with `--dry-run` | preview + `confirm_token` + `expires_at` and no side effects; `E_NOT_FOUND` (exit 3) is also a pass for a fabricated ID. **Discard any returned token unused — never send it** | §7 |
| P12 | `<tool> update --check --compact` (only if the tool ships self-update) | read-only; envelope with current/latest, install method; release host unreachable from this network → valid `E_NETWORK` envelope, mark N/A | §14 |
| P13 | a list command with `--limit 1` | pagination fields (`count`, `has_more`, cursor/offset per contract) | §8 |
| P14 | any command producing external content | `_untrusted` array present listing the external fields | SEC §2 |

P8/P13/P14 need a real read command — pick the cheapest from `reference` (P4). Skip-with-reason anything requiring live credentials the user hasn't provided.

## 4. Envelope assertions (apply to every probe)

For each captured stdout:

1. Parses as exactly **one** JSON document (`jq . out.json`); nothing before or after it — no banner, no color codes (Blocker: CLI-SPEC §4).
2. Has `ok` + `schema_version`; success payload under `data`, failure under `error{code,message,retryable}`; `meta.duration_ms` present on success **and** failure.
3. `error.code` ↔ exit code ↔ `retryable` agree with the table in the repo's `contract/contract.json` (read the actual file, don't recall it).
4. stderr may carry human text; stdout must never depend on it.
5. IDs are strings; timestamps ISO 8601 UTC (spot-check).
6. No secrets anywhere in stdout/stderr (grep captured output for token/password/authorization values you configured, if any).

**Degraded-verification protocol (probe blocked by environment).** When a probe returns `E_NETWORK`/`E_TIMEOUT` before its target logic runs (see §2 preflight), the envelope of the *network error* is still fully assertable — apply assertions 1–6 to it and, if they hold, the tool's error contract passed at that layer. But the probe's **intended** behavior is unobserved. Two honest outcomes:

- **Verified-by-source** — you read the implementation and confirmed the intended behavior is correctly coded (e.g. the confirm gate in `cmd/confirm.go` rejects empty/mismatched tokens). State the file/function as evidence. This is the preferred fallback for safety-critical gates (P9–P11).
- **Blocked-by-env** — the behavior cannot be observed *and* source inspection is inconclusive or out of scope (e.g. pagination shape P13). Record it plainly; do not guess a verdict.

Source-verification is a fallback, not a substitute: prefer live observation whenever the upstream (or a user-provided mock/E2E env) is reachable. Never upgrade a Verified-by-source gate to Pass — the distinction tells the reader what was actually exercised.

## 5. Write-path probes — extra care

- Target selection is a MUST, not a preference (see §2): update/delete-style commands only, fabricated IDs only, so even total gate failure mutates nothing. Create/send-style writes → N/A.
- If P9 or P10 executes (or attempts) the write instead of rejecting it: **stop probing writes immediately**, record a Blocker, and tell the user exactly what was attempted.
- Only when the user explicitly points at a disposable mock/E2E environment (docs/E2E.md) may a full dry-run → confirm cycle — or a create/send-style probe — be exercised. Ask first (STOP CHECKPOINT).

## 6. Recording results

For every probe record: command line, exit code, first ~20 lines of stdout, status per assertion, spec section. The probe-status vocabulary is **five** values — three is not enough in practice (a reachable upstream is not guaranteed):

- **Pass** — intended behavior observed live and all envelope assertions held.
- **Fail** — intended behavior observed and it violates the contract → file a finding.
- **N/A** — the probe does not apply to this tool (e.g. no write commands for P9–P11; create/send-only writes).
- **Verified-by-source** — behavior unobservable this run (upstream blocked) but confirmed correct by reading the implementation; cite the source (§4).
- **Blocked-by-env** — unobservable and not source-verified; no verdict claimed.

Only Pass/Fail carry contract weight; a Fail feeds the findings table. Verified-by-source and Blocked-by-env must name *why* the probe couldn't run live (which command returned `E_NETWORK`/`E_TIMEOUT`), so the reader can distinguish "tool is fine" from "reviewer couldn't check." Feed the full matrix into the report's probe-results section. Cross-check dynamic evidence against static claims:

- `reference.release_readiness.level` vs. actual test/FCC evidence found in Phase 1 — dishonest `stable` is a Major (CLI-SPEC §13).
- Risk tier claimed in SECURITY.md vs. tier reported in `reference`.
- `context.data.version` vs. SKILL.md `metadata.requires.min_version`.
