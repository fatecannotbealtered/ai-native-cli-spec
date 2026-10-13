export const meta = {
  name: 'fleet-confirm-binding-audit',
  description: 'Read-only VER-007 audit of the confirm-token write gate across fleet CLIs: a mapper per tool, then an independent skeptic who tries to refute every finding',
  whenToUse: 'Before tagging an ai-native-cli-spec release (VER-007). Args: {scratch_dir, clis: [{name, path, lang}]}',
  phases: [
    { title: 'Map', detail: 'one agent per CLI maps the token mechanism and every write command' },
    { title: 'Verify', detail: 'one skeptic per CLI tries to refute each finding and looks for misses' },
  ],
}

// args: {scratch_dir: "<folder outside every repository>", clis: [{name: "gitlab-cli", path: "<checkout>", lang: "Go"}, ...]}
const USAGE = 'args must be {scratch_dir: "<folder outside every repository>", clis: [{name, path, lang}, ...]}'
if (!args || typeof args.scratch_dir !== 'string' || !args.scratch_dir || !Array.isArray(args.clis) || !args.clis.length) {
  throw new Error(USAGE)
}
for (const c of args.clis) {
  if (!c || !c.name || !c.path || !c.lang) throw new Error(`${USAGE}; got ${JSON.stringify(c)}`)
}
const CLIS = args.clis
const SCRATCH = args.scratch_dir.replace(/[\\/]+$/, '')

const RULES = `Rules for this audit:
- Read-only. Do not modify, create, rename or delete anything inside the tool's folder, and do not commit. Anything you build, test or run happens in a copy of the repository under ${SCRATCH}/<tool-name>/ (tests write caches into the tree they run in); put any other scratch files there too.
- Do not run the tool against any network service or real account, and do not print the contents of credential, token or secret files. Reproduce findings offline, in the scratch copy, against loopback mocks.
- Evidence is file:line plus the relevant identifier. A false alarm costs as much as a miss; when the code does not settle a question, say "unclear" instead of guessing.`

const BACKGROUND = `Background. These command-line tools gate writes behind dry-run then confirm: \`--dry-run\` (or a write called without either flag, which is refused with the preview) returns a preview plus a confirm token, and the write runs only when the same command is called again with \`--confirm <token>\`. ai-native-cli-spec 2.0 (CLI-054, CLI-055) requires the token to bind the resolved request -- the command path; every supplied parameter, flags and positional arguments, after normalisation, excluding only declared presentation flags; the content of every file or stdin input, by digest; the resolved account, host and target, including values that come from environment variables or config files; for batch commands, every target and every per-item input -- under an HMAC keyed with a machine-local secret, and to be single-use. Earlier spec versions stated the same rule in less detail; tools written to them are audited against it too.

In gitlab-cli 1.2.17 each command hand-built the map that got hashed, and 23 of its 59 write commands left out inputs that change the effect. Proven example: \`mr update 5 --assignee alice --dry-run\` minted a token that \`mr update 5 --assignee dave --confirm <token>\` accepted, and the MR was assigned to dave. Other gaps there: \`variable create --value\` (value not bound), \`repo file update --content\`, \`issue create --title/--description\`, \`auth login --token\`.

An input is a GAP when changing it between the dry run and the confirm changes what is written, where, or to whom, yet the token still validates. Presentation-only inputs (output format, verbosity, --fields) do not count. Content read from a file or stdin counts: binding the path but not the content is a gap. If the confirm step executes a stored plan and ignores re-supplied arguments, that is not this kind of gap -- report it as the mechanism instead.`

const AREAS = ['key', 'store', 'modes', 'reach', 'update', 'secrets', 'other']

const MAP_SCHEMA = {
  type: 'object',
  properties: {
    cli: { type: 'string' },
    version: { type: 'string' },
    mechanism: {
      type: 'object',
      properties: {
        summary: { type: 'string' },
        binding_style: { type: 'string', enum: ['generic', 'hand-built', 'stored-plan', 'mixed', 'none', 'unclear'] },
        payload_fields: { type: 'array', items: { type: 'string' } },
        hmac_keyed: { type: 'string' },
        token_compare: { type: 'string' },
        single_use: { type: 'string' },
        expiry: { type: 'string' },
        account_bound: { type: 'string' },
        token_store_failure: { type: 'string' },
        record_before_send: { type: 'string' },
        aicli: { type: 'string' },
        evidence: { type: 'array', items: { type: 'string' } },
      },
      required: ['summary', 'binding_style', 'payload_fields', 'hmac_keyed', 'token_compare', 'single_use', 'expiry', 'account_bound', 'token_store_failure', 'record_before_send', 'aicli', 'evidence'],
    },
    write_commands: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          command: { type: 'string' },
          status: { type: 'string', enum: ['all-bound', 'gap', 'not-gated', 'unclear'] },
          unbound_inputs: { type: 'array', items: { type: 'string' } },
          note: { type: 'string' },
        },
        required: ['command', 'status'],
      },
    },
    gaps: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          command: { type: 'string' },
          unbound_inputs: { type: 'array', items: { type: 'string' } },
          example: { type: 'string' },
          severity: { type: 'string', enum: ['high', 'medium', 'low'] },
          evidence: { type: 'string' },
        },
        required: ['command', 'unbound_inputs', 'example', 'severity', 'evidence'],
      },
    },
    other_issues: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          area: { type: 'string', enum: AREAS },
          issue: { type: 'string' },
          severity: { type: 'string', enum: ['high', 'medium', 'low'] },
          evidence: { type: 'string' },
        },
        required: ['area', 'issue', 'severity', 'evidence'],
      },
    },
  },
  required: ['cli', 'version', 'mechanism', 'write_commands', 'gaps', 'other_issues'],
}

const VERIFY_SCHEMA = {
  type: 'object',
  properties: {
    cli: { type: 'string' },
    gap_verdicts: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          command: { type: 'string' },
          verdict: { type: 'string', enum: ['confirmed', 'refuted', 'uncertain'] },
          unbound_inputs: { type: 'array', items: { type: 'string' } },
          reproduced: { type: 'boolean' },
          reasoning: { type: 'string' },
          evidence: { type: 'string' },
        },
        required: ['command', 'verdict', 'unbound_inputs', 'reproduced', 'reasoning', 'evidence'],
      },
    },
    missed: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          command: { type: 'string' },
          unbound_inputs: { type: 'array', items: { type: 'string' } },
          verdict: { type: 'string', enum: ['confirmed', 'uncertain'] },
          severity: { type: 'string', enum: ['high', 'medium', 'low'] },
          reproduced: { type: 'boolean' },
          evidence: { type: 'string' },
        },
        required: ['command', 'unbound_inputs', 'verdict', 'severity', 'reproduced', 'evidence'],
      },
    },
    mechanism_corrections: { type: 'array', items: { type: 'string' } },
    other_issue_verdicts: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          issue: { type: 'string' },
          verdict: { type: 'string', enum: ['confirmed', 'refuted', 'uncertain'] },
          reasoning: { type: 'string' },
        },
        required: ['issue', 'verdict', 'reasoning'],
      },
    },
    summary: { type: 'string' },
  },
  required: ['cli', 'gap_verdicts', 'missed', 'mechanism_corrections', 'other_issue_verdicts', 'summary'],
}

function mapPrompt(c) {
  return `Audit the write gate of the \`${c.name}\` command-line tool. Source: ${c.path} (${c.lang}).

${BACKGROUND}

${RULES}

Steps:
1. Find the confirm-token mechanism: how a token is minted at dry-run and checked at confirm, and exactly what goes into the hashed payload. Say whether binding is generic (computed from the whole parsed invocation), hand-built per command, a stored plan, or mixed. Report: whether the token is keyed with a machine-local secret, what happens when the key is missing, empty, short or unreadable, and whether the caller can choose the key's location (hmac_keyed); whether tokens are compared in one canonical form, so that a re-encoded token (other case, padding, another encoding of the same values) cannot replay (token_compare); whether single use is enforced, atomically across processes (single_use); expiry; binding to the account or host; what happens when the token store cannot be read or written (token_store_failure); whether the operation record is written before anything is sent (record_before_send). If the tool vendors the shared aicli library (internal/aicli or <package>/_aicli), check the vendored files against the sha256 entries of its MANIFEST.json and say whether any write bypasses App.Write / app.write; otherwise say "not vendored" (aicli).
2. Enumerate EVERY command that writes (anything gated by dry-run/confirm, plus any write that is not gated). Work from the command definitions (cobra/argparse/click registrations, the registry or reference generator), not from grep hits alone, so none are missed. Record the tool version.
3. For each write command, list its user-supplied inputs (positional args, flags, stdin, input files, env- or config-derived defaults) and check each against the payload actually hashed on that code path. Mark the command all-bound, gap, not-gated, or unclear.
4. For each gap, give a concrete two-call example in the style of the gitlab-cli one, a severity (high: changes target, recipient, credential, secret value, or destructive scope; medium: changes content or attributes; low: cosmetic), and file:line evidence.
5. Report other weaknesses as other_issues, each with its area: key (an unkeyed fallback, an empty or short key accepted, a caller-chosen key location); store (single use failing open, no cross-process lock, a re-encoded token replaying, the record written after sending); modes (an output format, flag or environment variable that skips the gate); reach (an effect-bearing command outside the gate, declared effects or a read/write label in reference that do not match behaviour, scope not enforced); update (the self-update command: a target version not validated as strict semver, argv built through a shell or a .cmd/.bat target, a silent downgrade, Skills fetched from an unpinned source); secrets (a secret value, or an unsalted hash of one, in previews, tokens, journals, logs or errors); other.`
}

function verifyPrompt(c, m) {
  return `Adversarially verify an audit of the write gate of the \`${c.name}\` command-line tool. Source: ${c.path} (${c.lang}).

${BACKGROUND}

${RULES}

Another agent produced this audit:
${JSON.stringify(m)}

Your job is to find where it is wrong.
1. For EACH reported gap, try hard to refute it. Trace the real code path from argument parsing through token minting at dry-run to validation at confirm. Look for binding the first agent may have missed: a wrapper or middleware that hashes all flags, a payload built from the request body, a stored plan executed on confirm, a later check that compares the re-supplied inputs. Where you can, reproduce it in the scratch copy against a loopback mock, and set reproduced accordingly. Verdict: confirmed (you traced or reproduced it, and the input is not bound and does change the effect), refuted (it is bound, or it does not change the effect), or uncertain.
2. Look for misses: write commands or inputs the audit did not cover or marked all-bound wrongly. Check a sample of its all-bound commands, prioritising ones with many inputs, file/stdin content, credentials or recipients.
3. Check the mechanism claims (key handling, canonical token comparison, single use, expiry, account binding, token store failure, record before send, the aicli check) and each other_issue; correct anything wrong.
Return evidence as file:line for every verdict.`
}

const results = await pipeline(
  CLIS,
  c => agent(mapPrompt(c), { label: `map:${c.name}`, phase: 'Map', schema: MAP_SCHEMA }),
  (m, c) => {
    if (!m) return { cli: c.name, map: null, verify: null }
    return agent(verifyPrompt(c, m), { label: `verify:${c.name}`, phase: 'Verify', schema: VERIFY_SCHEMA })
      .then(v => ({ cli: c.name, map: m, verify: v }))
  },
)

const out = results.filter(Boolean)
const failed = CLIS.filter(c => !out.find(r => r.cli === c.name && r.map && r.verify)).map(c => c.name)
if (failed.length) log(`Incomplete audits: ${failed.join(', ')}`)
return { results: out, incomplete: failed }
