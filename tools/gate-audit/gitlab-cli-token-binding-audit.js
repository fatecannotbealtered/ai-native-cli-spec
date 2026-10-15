export const meta = {
  name: 'token-binding-audit',
  description: 'Per-file confirm-token binding audit of one CLI whose mechanism is already known (worked example: gitlab-cli 1.2.17): a finder per source file, an independent skeptic per reported gap',
  whenToUse: 'For one large tool whose token mechanism is already mapped. Args: {root, files, tool?, lang?, gate_calls?, context?}',
  phases: [
    { title: 'Find', detail: 'one reader per source file with write commands' },
    { title: 'Verify', detail: 'independent skeptic per reported gap' },
  ],
}

// args: {root: "<checkout of the tool>", files: ["cmd/mr.go", ...]} plus, for a tool other than the worked example,
// tool, lang, gate_calls (the calls that mint or check a token) and context (how the token binds inputs).
const USAGE = 'args must be {root: "<checkout of the tool>", files: ["<path relative to root>", ...], tool?, lang?, gate_calls?, context?}'
if (!args || typeof args.root !== 'string' || !args.root || !Array.isArray(args.files) || !args.files.length) {
  throw new Error(USAGE)
}
const ROOT = args.root.replace(/[\\/]+$/, '')
const FILES = args.files
const TOOL = args.tool || 'gitlab-cli'
const LANG = args.lang || 'Go'
const GATE_CALLS = args.gate_calls || 'prepareWrite / prepareBatchWrite / requireConfirm'

// The default describes gitlab-cli 1.2.17, the worked example; pass args.context for another tool or version.
const MECHANISM = args.context || `Write commands call prepareWrite(cmd, action, detail) in cmd/root.go, or prepareBatchWrite in cmd/batch.go. The confirm token is ` +
  `buildConfirmToken(action, payload, expires) in cmd/confirm.go: an HMAC over the action, the command path, the account context and ` +
  `normalizeConfirmPayload(payload). A dry run prints the same payload as the preview. So a user input is "bound" only if its value ends up in ` +
  `the payload map passed to prepareWrite/prepareBatchWrite (directly or via a variable built earlier). Read cmd/confirm.go and cmd/root.go first if needed.`

const FIND_SCHEMA = {
  type: 'object',
  properties: {
    commands: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          command: { type: 'string', description: `e.g. "${TOOL} mr update"` },
          line: { type: 'integer', description: `line of the ${GATE_CALLS} call` },
          change_inputs: { type: 'array', items: { type: 'string' }, description: 'flags/args that change WHAT the write does (targets and new values), e.g. --assignee, --add-labels, <iid>' },
          bound_inputs: { type: 'array', items: { type: 'string' }, description: 'which of change_inputs are part of the payload passed to the confirm-token function' },
          unbound_inputs: { type: 'array', items: { type: 'string' }, description: 'change_inputs NOT bound into the confirm token' },
          preview_omits: { type: 'array', items: { type: 'string' }, description: 'change_inputs the dry-run preview does not show' },
          evidence: { type: 'string', description: 'quote the payload map and the flag reads, with line numbers' },
        },
        required: ['command', 'line', 'change_inputs', 'bound_inputs', 'unbound_inputs', 'preview_omits', 'evidence'],
      },
    },
  },
  required: ['commands'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    real: { type: 'boolean', description: 'true if at least one listed input is genuinely NOT bound into the confirm token' },
    confirmed_unbound: { type: 'array', items: { type: 'string' } },
    refuted: { type: 'array', items: { type: 'string' }, description: 'inputs the finder listed that ARE in fact bound (explain in reasoning)' },
    reasoning: { type: 'string' },
  },
  required: ['real', 'confirmed_unbound', 'refuted', 'reasoning'],
}

const CONTEXT = `You are auditing the ${LANG} source of ${TOOL} at ${ROOT} (read-only: do not modify, build or run anything; use Read/Grep/Glob only). ${MECHANISM}`

phase('Find')
const results = await pipeline(
  FILES,
  file => agent(`${CONTEXT}\n\nAudit every write command defined in ${ROOT}/${file}. For each call to ${GATE_CALLS} in that file, list the user inputs (flags and positional args) that change what the write does, and say which are bound into the confirm payload and which the preview shows. Include commands with no gaps too (empty unbound_inputs). Be exact; quote code with line numbers.`,
    { label: `find:${file}`, phase: 'Find', schema: FIND_SCHEMA }),
  (found, file) => {
    const gaps = (found && found.commands || []).filter(c => c.unbound_inputs && c.unbound_inputs.length)
    return parallel(gaps.map(c => () =>
      agent(`${CONTEXT}\n\nA reviewer claims that in ${ROOT}/${file} the command "${c.command}" (around line ${c.line}) does NOT bind these inputs into its confirm token: ${JSON.stringify(c.unbound_inputs)}. Their evidence: ${c.evidence}\n\nTry hard to REFUTE this: trace where the payload is built, whether the inputs are added later, whether the payload normalisation or the account context could include them, and whether the command even uses these inputs in the write. Mark real=true only for inputs you confirm are used by the write but absent from the token payload.`,
        { label: `verify:${c.command}`, phase: 'Verify', schema: VERDICT_SCHEMA })
        .then(v => ({ file, command: c.command, line: c.line, claimed_unbound: c.unbound_inputs, preview_omits: c.preview_omits, verdict: v }))))
      .then(vs => ({ file, all_commands: (found && found.commands || []).map(c => ({ command: c.command, line: c.line, unbound: c.unbound_inputs, preview_omits: c.preview_omits })), gaps: vs.filter(Boolean) }))
  },
)
const ok = results.filter(Boolean)
const confirmed = ok.flatMap(r => r.gaps).filter(g => g.verdict && g.verdict.real)
log(`${ok.reduce((n, r) => n + r.all_commands.length, 0)} write commands audited; ${confirmed.length} confirmed token-binding gaps`)
return { confirmed, files: ok }
