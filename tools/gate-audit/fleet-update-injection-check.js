export const meta = {
  name: 'fleet-update-injection-check',
  description: 'Static trace, per fleet CLI, of the self-update path: version validation before the package manager runs (cmd.exe injection on Windows), downgrades and Skill sync (VER-007 update, CLI-070)',
  whenToUse: 'With fleet-confirm-binding-audit before an ai-native-cli-spec release (VER-007). Args: {clis: [{name, path, lang}]}',
  phases: [{ title: 'Trace', detail: 'one read-only tracer per tool' }],
}

// args: {clis: [{name: "cnstock-cli", path: "<checkout>", lang: "Go"}, ...]}
const USAGE = 'args must be {clis: [{name, path, lang}, ...]}'
if (!args || !Array.isArray(args.clis) || !args.clis.length) throw new Error(USAGE)
for (const c of args.clis) {
  if (!c || !c.name || !c.path || !c.lang) throw new Error(`${USAGE}; got ${JSON.stringify(c)}`)
}

const SCHEMA = {
  type: 'object',
  properties: {
    cli: { type: 'string' },
    verdict: { type: 'string', enum: ['vulnerable', 'validated', 'unclear'] },
    sources: { type: 'array', items: { type: 'string' }, description: 'where the version string can come from (flag, env endpoint, release JSON field)' },
    validation: { type: 'string', description: 'exact check applied before exec, with file:line, or "none"' },
    exec_path: { type: 'string', description: 'how the package manager is executed (directly, through cmd.exe because the target is npm.cmd or another .cmd/.bat, through a shell), file:line' },
    gated: { type: 'string', description: 'is update confirm-gated or a single ungated call' },
    downgrade: { type: 'string', description: 'what happens when the target is older than the installed version: refused unless an explicit flag (name it), accepted silently, or no target option; file:line' },
    skill_sync: { type: 'string', description: 'where Skills come from after an update: the installed package, a pinned source, or an unpinned network fetch (quote the command); file:line' },
    evidence: { type: 'string' },
  },
  required: ['cli', 'verdict', 'sources', 'validation', 'exec_path', 'gated', 'downgrade', 'skill_sync', 'evidence'],
}

const RULES = `Static analysis only. Do NOT run the tool's update command, npm, npx, pip, or anything that installs or executes packages, and do not modify any file in the repository. Do not contact the network. You may read code and the toolchain's sources.`

const BACKGROUND = `Background: in cnstock-cli 1.1.14 an independent reviewer found that \`update\` builds argv {"install","-g", pkg+"@"+target} and runs it with exec.CommandContext("npm", ...). On Windows "npm" resolves through PATHEXT to npm.cmd, so the process is cmd.exe, and Go's Windows argument escaping (syscall.makeCmdLine / appendEscapeArg) quotes only for space, tab, quote and backslash. It does not neutralize cmd.exe metacharacters such as & | < > ^ %. normalizeVersion only trimmed whitespace, "refs/tags/" and a leading v; no strict semver check was applied to the target. So a crafted --target-version, or a tag_name returned by an overridable release endpoint (env var), gave arbitrary command execution in one ungated call. Go 1.22+ has some cmd.exe argument checks for .bat/.cmd: check the toolchain version in go.mod and whether exec rejects such args (os/exec: "exec: cannot run .bat or .cmd with arguments containing..." / syscall EINVAL) in the version this repo builds with. If the toolchain blocks it, say so precisely. Python tools have the same sink when they run npm or pip through a shell (shell=True) or through a .cmd/.bat file.`

const results = await parallel(args.clis.map(c => () => agent(
  `Determine whether \`${c.name}\` (source ${c.path}, ${c.lang}) has the same flaw, and how its update path handles downgrades and Skills.

${BACKGROUND}

${RULES}

Trace every source of the version string used in the update path (the --target-version flag or equivalent; release-endpoint responses such as tag_name; any env var that overrides the endpoint) to the exec call. Report the exact validation applied before exec (strict semver regex/parse that rejects anything else, or none), how the package manager is executed, whether the toolchain itself would refuse metacharacters for .cmd targets, and whether \`update\` runs in a single ungated call. Verdict: vulnerable (an attacker-controllable string reaches cmd.exe or a shell unvalidated and the toolchain does not block it), validated, or unclear. Also report what happens when the target is older than the installed version (downgrade) and where Skills come from after an update (skill_sync). ai-native-cli-spec 2.0 (CLI-070) requires a strict semver target, argv built without a shell, an explicit downgrade flag, and Skill sync from the installed package.`,
  { label: `trace:${c.name}`, phase: 'Trace', schema: SCHEMA })))
return results.filter(Boolean)
