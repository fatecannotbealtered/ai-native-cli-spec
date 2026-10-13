# Tools

These are reference tools for the verification rules in [VERIFY-SPEC](../template/common/.agent/VERIFY-SPEC.md). Tools do not vendor them; `scripts/sync-spec.js` copies nothing from this folder. They are how the spec repository, and any tool maintainer who copies them, produce the evidence VERIFY-SPEC asks for. Each folder has its own README. The spec repository's CI compiles every Python file here and vets and tests the agent-eval recorder (REPO-075).

**[agent-eval/](agent-eval/README.md)** is the reference harness for the agent eval in VER-006.
- It seeds a disposable instance of the real upstream and gives each trial agent a recorder in place of the tool.
- It runs every trial in a fresh, isolated session: Claude Code workflow sub-agents, or `claude -p`.
- Code grades the upstream state and the answer independently of the agent's report, and arms are compared with paired bootstrap confidence intervals.

The tasks, seed and graders are those of the 2026-10 gitlab-cli eval, kept as the worked example; to evaluate another tool, port four files. Run it before a release that changes commands, outputs or a Skill, and before a command is marked or kept `stable` (VER-005).

**[gate-audit/](gate-audit/README.md)** holds the workflows for the fleet gate audit in VER-007. For every fleet tool, a read-only mapper and an independent skeptic check:
- confirm-token binding;
- key and store handling;
- output-mode bypasses;
- whether every effect-bearing command is gated;
- the self-update path;
- secrets in previews and tokens.

`audit_table.py` turns the runs' journals into `fleet-audit.json` and the summary table published with each spec release. Run it before tagging every spec release.

The two tools answer different questions. VER-006 measures how well agents use one tool through its Skill. VER-007 checks whether every fleet tool's write gate holds. Both rest on independent checking: graders query the upstream instead of trusting the agent's report, and skeptics try to refute the mappers' findings. Both run their agents through Claude Code's Workflow tool (`run.py` uses the `claude` CLI instead). Neither writes to a tool's repository.
