# gate-audit: the fleet gate audit for VER-007

These are the reference workflows for [VERIFY-SPEC, VER-007](../../template/common/.agent/VERIFY-SPEC.md#ver-007-fleet-gate-audit-on-each-spec-release). Before each spec release, they check how every fleet tool implements the write gate, without modifying any tool.

| File | What it does |
|---|---|
| `fleet-confirm-binding-audit.js` | The audit. For each tool, a mapper traces the confirm-token mechanism and classifies every write command; an independent skeptic then tries to refute each finding and looks for misses. It covers binding, key and token, store, modes, reach and secrets, and records any update-path issue it sees. |
| `fleet-update-injection-check.js` | A static trace of each tool's self-update path (CLI-070). It checks whether the target version is validated as strict semver before the package manager runs (on Windows, `npm.cmd` runs under cmd.exe), how downgrades are handled, and where Skills come from. |
| `gitlab-cli-token-binding-audit.js` | A per-file variant for one large tool whose token mechanism is already known: a finder per source file, then a skeptic per reported gap. Its default context describes gitlab-cli 1.2.17, the worked example. For any other tool, pass `context` and `gate_calls`. |
| `audit_table.py` | Builds `fleet-audit.json` from the workflows' journals and prints the summary table for the release notes. |

## Method

- **Read-only.** Agents only read the tool repositories. They never modify, create or delete anything in them, never commit, never contact a network service or real account, and never print credential files. Anything they build, test or run happens in a copy under `<scratch_dir>/<tool>/`, against loopback mocks. The update check is static only.
- **A mapper per tool.** The mapper:
  - finds how a token is minted and checked, and what the token binds;
  - enumerates every write command from the command definitions (cobra, click or argparse registrations, the registry, the reference generator), not from grep hits;
  - lists each command's inputs (flags, positionals, stdin, input files, values from environment or config) and checks each against what is hashed on that code path;
  - marks each command `all-bound`, `gap`, `not-gated` or `unclear`;
  - gives every gap a two-call example, a severity and file:line evidence;
  - lists other weaknesses, each tagged with its VER-007 area (key, store, modes, reach, update, secrets).
- **An independent skeptic per tool.** The skeptic receives the mapper's JSON and:
  - tries to refute every gap by tracing the real code path, reproducing it offline where it can;
  - looks for missed commands and inputs, starting with commands that take many inputs, file content, credentials or recipients;
  - checks the mechanism claims and every other issue.

  Its verdicts are `confirmed`, `refuted` or `uncertain`.
- **Order.** Each tool's mapper and skeptic run as a pipeline: the skeptic starts as soon as its mapper finishes, and tools run in parallel.
- **Severity.** High when the input changes the target, a recipient, a credential, a secret value or the destructive scope; medium when it changes content or attributes; low when the change is cosmetic.

## Running it from Claude Code

You need Claude Code with the Workflow tool, a clean checkout of every fleet tool at the version being audited, and a scratch folder outside every repository.

1. List the tools as the workflow's args:

   ```json
   {
     "scratch_dir": "/path/to/audit-scratch",
     "clis": [
       {"name": "gitlab-cli", "path": "/path/to/gitlab-cli", "lang": "Go"},
       {"name": "jira-cli", "path": "/path/to/jira-cli", "lang": "Go"},
       {"name": "example-py-cli", "path": "/path/to/example-py-cli", "lang": "Python"}
     ]
   }
   ```

2. Ask Claude Code to run the Workflow tool with `scriptPath` set to the full path of `tools/gate-audit/fleet-confirm-binding-audit.js` and `args` set to that object (pass an object, not a string). It returns `{results: [{cli, map, verify}], incomplete: [...]}`; re-run any tool listed in `incomplete`.
3. Run `fleet-update-injection-check.js` the same way, with `{"clis": [...]}`. It needs no scratch folder.
4. Build the results file from both runs' journals. The Workflow tool reports each run's transcript directory; the journal is at `~/.claude/projects/<project>/<session>/subagents/workflows/<run>/journal.jsonl`.

   ```
   python tools/gate-audit/audit_table.py --journal <binding run>/journal.jsonl --journal <update run>/journal.jsonl fleet-audit.json
   ```

   This overwrites `fleet-audit.json` and prints the table, so pass every journal of the release audit in one call. `python tools/gate-audit/audit_table.py fleet-audit.json` prints the table again.
5. Triage the findings (see Triage), then publish `fleet-audit.json` and the table with the release notes.

For a deeper pass on one tool whose mechanism is already known, run `gitlab-cli-token-binding-audit.js` with `{"root": "<checkout>", "files": ["cmd/mr.go", ...]}`. Unless the tool is gitlab-cli 1.2.17, also pass:
- `tool` and `lang`;
- `gate_calls`: the calls that mint or check a token;
- `context`: how the token binds inputs.

It returns `{confirmed, files}`: every write command per file, and the gaps a skeptic confirmed.

## fleet-audit.json

The file holds one entry per tool:

```json
{
  "<tool>": {
    "map": {
      "cli": "…", "version": "…",
      "mechanism": {"summary": "…", "binding_style": "generic|hand-built|stored-plan|mixed|none|unclear",
                    "payload_fields": ["…"], "hmac_keyed": "…", "token_compare": "…", "single_use": "…", "expiry": "…",
                    "account_bound": "…", "token_store_failure": "…", "record_before_send": "…", "aicli": "…",
                    "evidence": ["file:line …"]},
      "write_commands": [{"command": "…", "status": "all-bound|gap|not-gated|unclear", "unbound_inputs": ["…"], "note": "…"}],
      "gaps": [{"command": "…", "unbound_inputs": ["…"], "example": "…", "severity": "high|medium|low", "evidence": "…"}],
      "other_issues": [{"area": "key|store|modes|reach|update|secrets|other", "issue": "…", "severity": "…", "evidence": "…"}]
    },
    "verify": {
      "cli": "…",
      "gap_verdicts": [{"command": "…", "verdict": "confirmed|refuted|uncertain", "unbound_inputs": ["…"],
                        "reproduced": true, "reasoning": "…", "evidence": "…"}],
      "missed": [{"command": "…", "unbound_inputs": ["…"], "verdict": "confirmed|uncertain", "severity": "…",
                  "reproduced": false, "evidence": "…"}],
      "mechanism_corrections": ["…"],
      "other_issue_verdicts": [{"issue": "…", "verdict": "confirmed|refuted|uncertain", "reasoning": "…"}],
      "summary": "…"
    },
    "trace": {"cli": "…", "verdict": "vulnerable|validated|unclear", "sources": ["…"], "validation": "…",
              "exec_path": "…", "gated": "…", "downgrade": "…", "skill_sync": "…", "evidence": "…"}
  }
}
```

`map` and `verify` come from the binding audit, and `trace` from the update check. Results from the 2026-10 scripts lack `token_compare`, `record_before_send`, `aicli`, `area`, `reproduced`, `downgrade` and `skill_sync`; `audit_table.py` reads both formats.

For each tool, the table shows:
- the version, the binding style and the number of write commands;
- confirmed findings by severity: gap verdicts `confirmed` plus `missed` entries `confirmed`;
- refuted and uncertain gaps;
- findings only the skeptic found;
- other issues the skeptic confirmed;
- the update verdict.

## Triage

1. **Count only confirmed findings.** A finding counts when the skeptic confirmed it. Refuted gaps are dropped. A person settles each uncertain one, by reading the code or reproducing it in a scratch copy.
2. **Review high severity by hand.** Severity is the mapper's (the skeptic's for missed findings). A person reviews every confirmed high finding; record any disputed rating next to the counts.
3. **Find where each confirmed finding comes from:**
   - *aicli* (the shared library, vendored unchanged): a high-severity finding blocks the spec release (VER-007). Fix it in `template/go/aicli` or `template/python/aicli`, together with a conformance check that would have caught it.
   - *Tool code* (the tool's own gate, a write that bypasses `App.Write`, a vendored copy that differs from its manifest): file it against the tool, with the two-call example and the evidence.
   - *Spec text* (a weakness the spec itself requires, such as v1's rule that a token-store failure must never block the write): change the spec.
4. **Publish.** Release `fleet-audit.json` and the table with the release notes, together with the count of open findings per tool and a link to each filed issue.

## Changes from the 2026-10 scripts

- **Paths from args.** Every path comes from the workflow args: the tool checkouts (`clis[].path`, `root`) and the scratch folder (`scratch_dir`).
- **Binding audit.**
  - It cites the 2.0 binding rule (CLI-054, CLI-055) instead of v1's section 7.
  - It asks for every VER-007 area: canonical token comparison, record before send, and the aicli manifest and `App.Write` check, plus an `area` on each other issue.
  - Builds and tests run only in the scratch copy.
  - Skeptics record whether they reproduced each finding (`reproduced`).
- **Update check.** It covers Python tools as well as Go, and reports downgrade handling and Skill sync.
- **Per-file audit.** It takes the tool, files and context from args. Without them it describes the 2026-10 gitlab-cli 1.2.17 audit.
- **audit_table.py.** It reads journals given on the command line, merges the update check, and adds columns for confirmed other issues and the update verdict.

No audit results or journals are kept in this folder.
