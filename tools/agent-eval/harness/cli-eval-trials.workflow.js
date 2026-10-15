export const meta = {
  name: 'cli-eval-trials',
  description: 'Run agent-eval trials as isolated sub-agents: read trials in parallel, write trials in sequential per-task lanes with resets and state snapshots',
  whenToUse: 'Step 3 of the VER-006 agent eval. Args: the JSON object in <work_dir>/runs/<batch>/workflow-args.json, written by harness/plan.py',
  phases: [{ title: 'Trials', detail: 'each trial is a fresh sub-agent; helper steps reset and snapshot the test upstream between write trials' }],
}

// args is <work_dir>/runs/<batch>/workflow-args.json, written by harness/plan.py:
// {batch, effort, step, upstream_url, ws_root, prompt_template: {on, off}, lanes: [{task, kind, task_prompt, trials: [{wid, model, skill}]}]}
// Optional: step_model (default 'sonnet') and step_effort (default 'low') for the helper agents that run lane.py.
const REQUIRED = ['batch', 'effort', 'step', 'upstream_url', 'ws_root', 'prompt_template', 'lanes']
const missing = REQUIRED.filter(k => !args || args[k] === undefined || args[k] === null)
if (missing.length) {
  throw new Error(`workflow args lack ${missing.join(', ')}: pass the JSON object in runs/<batch>/workflow-args.json (written by harness/plan.py) as args`)
}
if (typeof args.prompt_template.on !== 'string' || typeof args.prompt_template.off !== 'string') {
  throw new Error('workflow args: prompt_template needs "on" and "off" strings')
}

// Renders the prompt exactly as render_prompt() in harness/plan.py does; collect.py verifies every received prompt.
function buildPrompt(t, taskPrompt) {
  const ws = `${args.ws_root}/${t.wid}`
  return args.prompt_template[t.skill].split('{ws}').join(ws).split('{request}').join(taskPrompt)
}

const STEP_CONTEXT = `This command is part of the agent evaluation the user asked for: sub-agent trials against a disposable test instance at ${args.upstream_url}. It resets or snapshots that test instance between trials, and touches nothing outside it and the evaluation's work folder.`

async function step(label, cmd) {
  for (let attempt = 0; attempt < 3; attempt++) {
    const out = await agent(
      `${STEP_CONTEXT}\n\nRun exactly this one command with the Bash tool and reply with only the last line it prints. Run nothing else.\n\n${cmd}`,
      { label: attempt ? `${label}:retry${attempt}` : label, phase: 'Trials', model: args.step_model || 'sonnet', effort: args.step_effort || 'low' })
    if (typeof out === 'string' && out.includes('STEP OK')) return true
  }
  return false
}

async function runTrial(t, taskPrompt) {
  const answer = await agent(buildPrompt(t, taskPrompt),
    { label: `run:${t.wid}`, phase: 'Trials', model: t.model, effort: args.effort })
  return { wid: t.wid, status: typeof answer === 'string' && answer.trim() ? 'answered' : 'no answer' }
}

async function runWriteLane(lane) {
  const out = []
  const prep = wid => `${args.step} step ${args.batch} --prep ${wid}`
  let ready = await step(`step:prep:${lane.trials[0].wid}`, prep(lane.trials[0].wid))
  for (let i = 0; i < lane.trials.length; i++) {
    const t = lane.trials[i]
    const next = lane.trials[i + 1]
    if (!ready) {
      out.push({ wid: t.wid, status: 'skipped: reset failed' })
      log(`skipped ${t.wid}: reset failed`)
      if (next) ready = await step(`step:prep:${next.wid}`, prep(next.wid))
      continue
    }
    out.push(await runTrial(t, lane.task_prompt))
    ready = await step(`step:${t.wid}`, `${args.step} step ${args.batch} --post ${t.wid}` + (next ? ` --prep ${next.wid}` : ''))
    if (!ready) log(`state step after ${t.wid} failed`)
  }
  return out
}

phase('Trials')
// The concurrency queue is first in, first out: submit the write lanes first so they do not wait behind reads.
const readThunks = args.lanes.filter(l => l.kind === 'read')
  .flatMap(l => l.trials.map(t => () => runTrial(t, l.task_prompt)))
const writeThunks = args.lanes.filter(l => l.kind === 'write' && l.trials.length).map(l => () => runWriteLane(l))
const results = await parallel([...writeThunks, ...readThunks])
const all = results.filter(Boolean).flat()
log(`${all.filter(r => r.status === 'answered').length}/${all.length} trials answered`)
return all
