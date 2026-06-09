# ai-native-cli-spec

**中文版 → [README_zh.md](README_zh.md)**

A spec suite **and a copy-paste seed** for building **AI-native CLI tools** — command-line tools designed to be driven reliably by AI agents, not just humans.

Pull the `.agent/` seed into a new project, point your AI at it, and let it build a tool that already conforms to a stable agent contract.

The template is split into a language-agnostic **`common/`** layer plus a thin per-language overlay (**`go/`**, **`python/`**, ...). Pull `common/`, then overlay exactly one language:

```bash
# In a new (or existing) project directory:
#   1. the language-agnostic common layer
npx degit fatecannotbealtered/ai-native-cli-spec/template/common .
#   2. exactly one language overlay (go OR python)
npx degit fatecannotbealtered/ai-native-cli-spec/template/go .

# Then follow template/INSTANTIATE.md to fill placeholders, and tell your AI:
#   "Read AGENTS.md, then build this tool following the .agent/ specs."
```

See [`template/INSTANTIATE.md`](template/INSTANTIATE.md) for the full copy-paste guide (placeholder dictionary, `.gitignore` concatenation, Skill template rename, `test-prompts.json` setup, the `NOTICE.md` drop-if-not-wrapping rule).

That drops a single coherent skeleton into your repo. From `common/`:

```
AGENTS.md          # entry hook every agent reads first
.agent/
├── AGENT.md       # the playbook: navigation + greenfield/extend workflows
├── CLI-SPEC.md    # CLI machine contract — how the tool "speaks"
├── SKILL-SPEC.md  # Skill authoring — how the agent "listens"
└── SEC-SPEC.md    # security baseline — how not to get burned
README / LICENSE / CHANGELOG / CONTRIBUTING / SECURITY / CODE_OF_CONDUCT / NOTICE
.github/ (PR + issue templates, dependabot)  ·  package.json + scripts/{install,run}.js
docs/OPEN_SOURCE_CHECKLIST.md  ·  skills/{SKILL.md.tmpl,test-prompts.json.tmpl}  ·  .gitignore
```

Plus, from the one language overlay you pick (`go/` or `python/`): the CI / release workflows, formatter & linter config, build plumbing, and a language `.gitignore` to append. The repo skeleton standard itself lives at [`REPO-SPEC.md`](REPO-SPEC.md), the standard's meta-doc.

## Why

LLM agents call CLIs constantly, but most CLIs are built for interactive terminals: prose output, ad-hoc exit codes, interactive prompts that hang in automation. This suite encodes one coherent, opinionated standard so that **every tool you build behaves the same way an agent expects** — and so an AI can build a new one from day one without re-deriving the rules.

It is intentionally **layered, not heavy**:

- **One entry** (`AGENT.md`) → four focused specs. Read only what the task needs.
- **Risk-tiered & optional patterns.** A read-only tool stays light; a tool that executes SQL or holds expiring tokens opts into more. The spec scales with the tool.

## What it standardizes

- **Agent-safe output contract** — single JSON document on stdout, uniform `ok` / `schema_version` / `data` / `meta` envelope, logs on stderr.
- **Structured errors** — stable `E_*` codes ↔ exit codes ↔ `retryable`, so agents can decide retry vs. fix vs. ask a human.
- **Write safety** — `--dry-run` → `--confirm <token>` flow with operation-bound tokens.
- **Self-description** — `reference` / `context` / `doctor` / `changelog` so agents discover capabilities and learn what changed after a self-update.
- **Skills** — Anthropic-aligned `SKILL.md` authoring, progressive disclosure, version negotiation.
- **Security** — untrusted-content/injection handling, least privilege, credential-at-rest, supply chain.
- **Release quality gate** — Functional Contract Coverage = 100% for public README / Skill / reference / help / context / doctor / changelog / update behavior; numeric line coverage is secondary.
- **Repo & distribution** — required docs, bilingual README convention, single-source CHANGELOG, npm-wrapper distribution.

## How to use it

- **Add to an existing repo:** pull `template/common` then exactly one `template/<lang>`:
  ```bash
  npx degit fatecannotbealtered/ai-native-cli-spec/template/common .
  npx degit fatecannotbealtered/ai-native-cli-spec/template/go .   # or template/python
  ```
- **Pin a version:** append `#v1.0` to each path, e.g. `.../template/common#v1.0 .`
- **Start a brand-new repo:** click **Use this template** on GitHub, then keep only the one `template/<lang>` you need.

Then walk [`template/INSTANTIATE.md`](template/INSTANTIATE.md) to fill placeholders, customize the `## 本项目` (This project) section at the bottom of `AGENTS.md`, and let the agent follow **Workflow A (greenfield)** in `.agent/AGENT.md`.

## Reference implementation

[**outlook-cli**](https://github.com/fatecannotbealtered/outlook-cli) — an Outlook/Exchange CLI built to this spec. Use it as the worked example for the envelope, dry-run/confirm flow, self-description commands, and the `changelog` mechanism.

## Languages

Every doc is bilingual: English is the primary `X.md`, Chinese is `X_zh.md`, and the two link to each other at the top. This applies to the READMEs, `AGENTS.md`, and all four `.agent/` specs.

## License

[MIT](LICENSE)
