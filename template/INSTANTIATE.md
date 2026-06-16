# Instantiating a repo from this template

**中文版 → [INSTANTIATE_zh.md](INSTANTIATE_zh.md)**

This template is split into a language-agnostic **`common/`** layer and a thin
per-language overlay (**`go/`**, **`python/`**, ...). You copy `common/` once,
overlay **exactly one** language, fill in the placeholders, and you have a repo
that already conforms to the spec.

```
template/
├── INSTANTIATE.md          # this guide
├── common/                 # language-agnostic — copied into EVERY repo
│   ├── AGENTS.md (+_zh)
│   ├── .agent/{AGENT,CLI-SPEC,SKILL-SPEC,SEC-SPEC}.md (+_zh)
│   ├── README.md (+_zh) / LICENSE / CHANGELOG.md
│   ├── CONTRIBUTING.md (+_zh) / SECURITY.md (+_zh)
│   ├── CODE_OF_CONDUCT.md (+_zh) / NOTICE.md (+_zh)
│   ├── .github/ (PR template, ISSUE_TEMPLATE/, dependabot.yml)
│   ├── package.json / .npmrc / scripts/{run,prepare-npm-platform-packages}.js
│   ├── scripts/{version-files,sync-version,check-version}.js  # version SSoT: `npm version` syncs, CI checks
│   ├── docs/OPEN_SOURCE_CHECKLIST.md (+_zh)
│   ├── skills/{SKILL.md.tmpl,test-prompts.json.tmpl}
│   └── .gitignore / .gitattributes
│                                 # common hygiene; per-language .gitignore appended
├── go/                     # overlay — ci.yml, release.yml, .goreleaser.yml,
│                           #   Makefile, .golangci.yml, cmd/fcc_guard_test.go, .gitignore
└── python/                 # overlay — ci.yml, release.yml, ruff.toml,
│                           #   tests/test_fcc_guard.py, .gitignore
```

## Placeholder dictionary

SHARED PLACEHOLDER DICTIONARY — use these literal tokens, never bake in real values:
- {{TOOL_NAME}}        kebab-case CLI/binary/repo name, e.g. my-cli
- {{DESCRIPTION}}      one-line tool description
- {{MODULE}}           Go module path, e.g. github.com/OWNER/{{TOOL_NAME}}   (go templates only)
- {{CMD_PATH}}         Go main package path, e.g. ./cmd/{{TOOL_NAME}}        (go templates only)
- {{NPM_PKG}}          full npm package name incl. scope, e.g. @SCOPE/{{TOOL_NAME}}
- {{TOOL_PKG}}         Python import package (underscores), e.g. my_cli       (python templates only)
- {{VERSION}}          semver, e.g. 0.1.0
- {{COPYRIGHT_YEAR}}   e.g. 2024-2026
- {{COPYRIGHT_HOLDER}} e.g. Your Name
- {{CONTACT_EMAIL}}    security disclosure contact
- {{RISK_TIER}}        T0 | T1 | T2  (see SEC-SPEC)
- {{RISK_TIER_DESC}}   short justification of the tier
- {{THIRD_PARTY}}      wrapped third-party product name, e.g. GitLab (NOTICE only)
- {{ENV_PREFIX}}       UPPER_SNAKE env var prefix for runtime/update env vars, e.g. MY_CLI
- {{REPO_SLUG}}        GitHub owner/repo, e.g. OWNER/{{TOOL_NAME}}
- {{PRIMARY_TRIGGER}}  primary concrete user intent that activates the Skill
- {{SECONDARY_TRIGGER}} secondary concrete user intent that activates the Skill
- {{OUT_OF_SCOPE_CASE}} concrete case where a different tool or no CLI is the right fit
- {{DOMAIN_1}} / {{DOMAIN_2}} reference doc slugs for larger tools, e.g. issue, ci
- {{DOMAIN_1_INTENT}} / {{DOMAIN_2_INTENT}} user intents mapped to those reference docs
Keep placeholders consistent across files. Write files with the Write tool to the EXACT paths given.

## Steps

1. **Copy the common layer.** Copy the entire contents of `template/common/.`
   into your (new or existing) repo root — dotfiles included (`.agent/`,
   `.github/`, `.gitignore`).

2. **Overlay exactly ONE language.** Copy the entire contents of
   `template/<lang>/.` (e.g. `template/go/.`) on top of the same repo root.
   Pick exactly one — never mix `go/` and `python/` into the same repo.

3. **Concatenate the .gitignore.** The language overlay ships its own
   `.gitignore`. Append it onto the common one rather than overwriting:

   ```bash
   cat template/<lang>/.gitignore >> .gitignore
   ```

   (`common/.gitignore` already shipped to the repo root in step 1; the
   `<lang>` copy is the part you append.)

4. **Fill every placeholder.** Replace every `{{...}}` token across all copied
   files with the real value from the dictionary above. The `{{MODULE}}` and
   `{{CMD_PATH}}` tokens only appear in `go/` files — Python repos won't have
   them. Sanity-check that no `{{` survives:

   ```bash
   # Match only UPPER_SNAKE placeholder tokens — plain '{{' would also hit
   # legitimate ${{ ... }} in workflows and {{ .Var }} in .goreleaser.yml.
   grep -rnE '\{\{[A-Z_0-9]+\}\}' . || echo "all placeholders filled"
   ```

5. **Drop NOTICE.md if not wrapping a third party.** `NOTICE.md` (+ `_zh`) is
   only required when your tool wraps a third-party product (the `{{THIRD_PARTY}}`
   case — Outlook/Exchange, GitLab, Jira, ...). If you don't wrap one, delete
   `NOTICE.md` and `NOTICE_zh.md`.

6. **Rename the Skill templates.** `skills/SKILL.md.tmpl` and
   `skills/test-prompts.json.tmpl` are stubs. Move them into a per-tool
   directory and drop the `.tmpl` suffix:

   ```bash
   mkdir -p skills/{{TOOL_NAME}}
   mv skills/SKILL.md.tmpl skills/{{TOOL_NAME}}/SKILL.md
   mv skills/test-prompts.json.tmpl skills/{{TOOL_NAME}}/test-prompts.json
   ```

   Edit both files before publishing. `SKILL.md` must keep the AI-native
   sections (`Do not use`, `First Step`, `Write Recipe` or read-only boundary,
   `STOP CHECKPOINT`, error decision tree, security boundary, self-update, and
   eval scenarios). `test-prompts.json` is the regression set used by Skill
   review; keep the prompts concrete to this CLI.

7. **Create the build manifest and lockfile.** The CI workflows assume the
   language build manifest exists — the template does not ship it because it
   is project-specific:

   - Go: `go mod init {{MODULE}}` (CI reads the Go version from `go.mod`).
   - Python: a `pyproject.toml`/`setup.py` exposing a `[dev]` extra (pytest,
     ruff, pip-audit, pyinstaller), a `requirements.txt` for `pip-audit -r`,
     and a `build.py` PyInstaller entry used by `release.yml`.
   - npm wrapper: generate and commit the lockfile that `npm ci` / `npm audit`
     in CI require: `npm install --package-lock-only --ignore-scripts`.

8. **Run the open-source checklist.** Walk `docs/OPEN_SOURCE_CHECKLIST.md`
   before the first public push.

## The lightness contract

The split exists so that **adding a new language is cheap and the behavioral
specs never fork.**

- **Adding a language = add `template/<lang>/`** with ~6 files (CI, release,
  formatter/linter config, build manifest, language `.gitignore`). Nothing in
  `common/` changes.
- **Every repo reuses 100% of `common/`** — the same `AGENTS.md`, the same four
  `.agent/` specs, the same governance docs, the same npm wrapper.
- **The `.agent/` behavioral specs (`AGENT`, `CLI-SPEC`, `SKILL-SPEC`,
  `SEC-SPEC`) are language-agnostic and MUST NOT fork per language.** The
  machine contract a Go tool exposes is identical to the one a Python tool
  exposes; only the build/CI plumbing differs, and that plumbing is the *only*
  thing a language overlay is allowed to carry.

If you find yourself wanting a Go-specific or Python-specific behavioral rule,
that is a signal the rule belongs in `common/.agent/` as a language-neutral
statement, not in the overlay.
