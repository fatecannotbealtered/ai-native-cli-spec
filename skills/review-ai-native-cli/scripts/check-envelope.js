#!/usr/bin/env node
// check-envelope.js <stdout-file> <exit-code> [contract-dir]
//
// Checks one captured stdout (json mode, one envelope) against the target tool's
// own vendored contract/contract.json and contract/contract-ext.json, so the
// review grades against the pinned contract, not against memory. Covers
// CLI-001..004, CLI-007, CLI-010..015, CLI-042, CLI-055, CLI-058.
// Prints PASS or FAIL with one line per problem, and WARN lines that need the
// reviewer's judgement. Exit 0 = PASS, 1 = FAIL. Node 16+, standard library only.
"use strict";
const fs = require("fs");
const path = require("path");

const [file, exitArg, dir = "contract"] = process.argv.slice(2);
if (!file || exitArg === undefined) {
  console.error("usage: node check-envelope.js <stdout-file> <exit-code> [contract-dir]");
  process.exit(2);
}
const c = JSON.parse(fs.readFileSync(path.join(dir, "contract.json"), "utf8"));
const ext = {};
try {
  const e = JSON.parse(fs.readFileSync(path.join(dir, "contract-ext.json"), "utf8"));
  const table = e.error_codes || e.codes || e;
  for (const k of Object.keys(table)) if (/^E_/.test(k)) ext[k] = table[k];
} catch (_) {
  // no extension codes
}
const raw = fs.readFileSync(file, "utf8");
const exit = Number(exitArg);
const bad = [];
const warn = [];
const chars = [...raw].length;

if (raw.charCodeAt(0) === 0xfeff) bad.push("CLI-001 stdout starts with a BOM");
if (raw.includes("\r")) bad.push("CLI-001 stdout contains CR line endings");
let env;
try {
  env = JSON.parse(raw);
} catch (e) {
  console.log(`FAIL ${file} exit=${exit}: CLI-001 stdout is not exactly one JSON document (${e.message})`);
  process.exit(1);
}
if (raw.trimEnd().includes("\n")) bad.push("CLI-003 not compact: the document spans several lines");

const sorted = (a) => [...a].sort().join(",");
const E = c.envelope;
const ok = env.ok === true;
if (sorted(Object.keys(env)) !== sorted(ok ? E.success_keys : E.error_keys)) bad.push(`CLI-002 envelope keys are ${sorted(Object.keys(env))}`);
if (env.schema_version !== E.schema_version_value) bad.push(`CLI-002 schema_version is ${JSON.stringify(env.schema_version)}`);
const meta = env.meta || {};
if (!Number.isInteger(meta.duration_ms)) bad.push("CLI-004 meta.duration_ms is missing or not an integer");
for (const k of Object.keys(meta)) if (![...E.meta_required_keys, ...E.meta_optional_keys].includes(k)) bad.push(`CLI-004 unexpected meta.${k}`);

if (ok) {
  if (exit !== 0) bad.push(`CLI-010 ok:true with exit ${exit}`);
  if (chars > c.size_budgets.default_success_chars)
    warn.push(`CLI-042 success is ${chars} chars (> ${c.size_budgets.default_success_chars}); a finding only with default flags (full reference and --fields all are exempt)`);
} else {
  const er = env.error || {};
  const d = er.details || {};
  const spec = (c.error_codes.core || {})[er.code] || ext[er.code];
  if (sorted(Object.keys(er)) !== sorted(E.error_object_keys)) bad.push(`CLI-010 error keys are ${sorted(Object.keys(er))}`);
  if (!spec) bad.push(`CLI-010/018 ${er.code} is neither a core code nor in contract-ext.json`);
  else {
    if (spec.exit !== exit) bad.push(`CLI-010 ${er.code} exited ${exit}; the contract says ${spec.exit}`);
    if (spec.retryable !== er.retryable) bad.push(`CLI-010 ${er.code} retryable is ${er.retryable}; the contract says ${spec.retryable}`);
  }
  const ed = c.error_details;
  for (const k of ed.always_required) if (!(k in d)) bad.push(`CLI-011 details.${k} is missing`);
  if ("dispatch_state" in d) {
    if (!ed.dispatch_state.includes(d.dispatch_state)) bad.push(`CLI-011 dispatch_state is ${JSON.stringify(d.dispatch_state)}`);
    const local = ["E_USAGE", "E_AMBIGUOUS", "E_CONFIRMATION_REQUIRED", "E_IO"].includes(er.code) ||
      (er.code === "E_FORBIDDEN" && d.scope) ||
      (er.code === "E_CONFLICT" && /^(token_|state_drift|scope_changed|unresolved_operation|lock_held|idempotency_mismatch)/.test(d.conflict || ""));
    if (local && d.dispatch_state !== "not_started") bad.push(`CLI-011 ${er.code} ${d.conflict || d.problem || ""} is refused locally, so dispatch_state must be not_started`);
    // A request that may have reached the upstream: dispatched when it was sent, unknown when the tool cannot tell.
    if (er.code === "E_OUTCOME_UNKNOWN" && !["dispatched", "unknown"].includes(d.dispatch_state)) bad.push("CLI-015 E_OUTCOME_UNKNOWN needs dispatch_state dispatched or unknown");
    if (er.code === "E_NETWORK" && d.dispatch_state !== "not_started") bad.push("CLI-015 E_NETWORK means nothing was sent, so dispatch_state must be not_started");
  }
  const rule = (ed.by_code || {})[er.code] || {};
  const need = [...(rule.required || [])];
  if (d.problem === "confirmation_required") need.push(...(rule.required_when_confirmation_required || []));
  if (d.conflict === "token_mismatch") need.push(...(rule.required_when_token_mismatch || []));
  if (["unresolved_operation", "lock_held"].includes(d.conflict)) need.push(...(rule.required_when_unresolved_operation_or_lock_held || []));
  if (rule.required_one_of && !rule.required_one_of.some((k) => k in d)) bad.push(`CLI-012/013 ${er.code} needs one of details.${rule.required_one_of.join("|")}`);
  for (const k of need) if (!(k in d)) bad.push(`CLI-012/013 ${er.code} lacks details.${k}`);
  if (d.problem && !(ed.problem || []).includes(d.problem)) bad.push(`CLI-012 problem ${d.problem} is not in the contract enum`);
  if (d.conflict && !(ed.conflict || []).includes(d.conflict)) bad.push(`CLI-013 conflict ${d.conflict} is not in the contract enum`);
  if (chars > c.size_budgets.error_chars) bad.push(`CLI-042 error is ${chars} chars (> ${c.size_budgets.error_chars})`);
}

// Gate artefacts wherever they appear: canonical token and operation id, expiry within the TTL.
const tokenRe = new RegExp(c.token.format);
const opRe = new RegExp(`${c.operation_id.format}|${c.operation_id.batch_format}`);
const holder = ok ? env.data || {} : (env.error || {}).details || {};
if (typeof holder.confirm_token === "string" && !tokenRe.test(holder.confirm_token)) bad.push("CLI-055 confirm_token is not canonical");
if (typeof holder.operation_id === "string" && !opRe.test(holder.operation_id)) bad.push("CLI-058 operation_id has the wrong format");
if (typeof holder.expires_at === "string") {
  const ttl = (Date.parse(holder.expires_at) - Date.now()) / 1000;
  if (!(ttl <= c.token.max_ttl_seconds + 60)) bad.push(`CLI-055 expires_at is ${Math.round(ttl)} s away (max ${c.token.max_ttl_seconds})`);
}
if (holder.preview && holder.preview.can_apply === false && holder.confirm_token) bad.push("CLI-051 can_apply is false but a token was issued");

// CLI-007: snake_case keys; maps keyed by error codes or numbers are exempt.
const snake = /^[a-z][a-z0-9_]*$/;
const exempt = /^(E_[A-Z0-9_]+|\d+)$/;
(function walk(v, p) {
  if (Array.isArray(v)) return v.forEach((x) => walk(x, `${p}[]`));
  if (!v || typeof v !== "object") return;
  for (const [k, x] of Object.entries(v)) {
    if (exempt.test(k)) continue;
    if (!snake.test(k)) bad.push(`CLI-007 key ${p ? p + "." : ""}${k} is not snake_case`);
    walk(x, p ? `${p}.${k}` : k);
  }
})(env, "");

console.log(`${bad.length ? "FAIL" : "PASS"} ${file} exit=${exit} chars=${chars} ${ok ? "ok" : (env.error || {}).code}`);
for (const b of bad) console.log(`  - ${b}`);
for (const w of warn) console.log(`  ~ WARN ${w}`);
process.exit(bad.length ? 1 : 0);
