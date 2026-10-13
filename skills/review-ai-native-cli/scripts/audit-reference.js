#!/usr/bin/env node
// audit-reference.js <reference.json> [repo-root]
//
// Reads a capture of the tool's full `reference` (run from the built binary)
// and lists registry gaps per command: CLI-020 (summary, output schema,
// example), CLI-024 (no default on a required parameter), CLI-025 (type vs
// effects, deliver/configure dangerous), CLI-026 (error codes), CLI-030 and
// CLI-056 (write and dangerous examples), SEC-021 (parameters that look secret
// but are not marked), VER-005 (maturity backed by evidence that resolves from
// repo-root), and SEC-001 (declared risk tier against the effects' floor).
// Built-in commands come from aicli, so their evidence is not graded here.
// Findings are leads: confirm each in the source before filing it.
"use strict";
const fs = require("fs");
const path = require("path");

const [file, root = "."] = process.argv.slice(2);
if (!file) {
  console.error("usage: node audit-reference.js <reference.json> [repo-root]");
  process.exit(2);
}
const r = JSON.parse(fs.readFileSync(file, "utf8")).data;
const builtin = /^(reference|context|doctor|changelog|update|operation( .*)?)$/;
const tierOf = (e) => (e.some((x) => x === "deliver" || x === "configure") ? 2 : e.some((x) => x !== "observe") ? 1 : 0);
// VER-005 is cumulative: each level keeps the evidence of the one below it.
const needs = { experimental: ["simulated"], verified: ["simulated", "live"], stable: ["simulated", "live", "eval"] };
const secretName = /token|password|passwd|secret|api[-_]?key|private[-_]?key|credential|passphrase/i;
let floor = 0;
let writes = 0;
let gapCount = 0;

console.log(`tool=${r.tool} version=${r.version} spec_version=${r.spec_version} schema_version=${r.schema_version} risk_tier=${r.risk_tier}`);
if ("release_readiness" in r) console.log("GAP release_readiness is still published (CLI-027: removed in 2.0)");
for (const c of r.commands || []) {
  const gaps = [];
  const eff = c.effects || [];
  const ex = c.examples || [];
  const ev = c.evidence || [];
  const isWrite = eff.some((x) => x !== "observe");
  floor = Math.max(floor, tierOf(eff));
  if (isWrite) writes++;
  if ((c.type === "write") !== isWrite) gaps.push(`CLI-025 type ${c.type} does not match effects ${eff.join("+")}`);
  if (!c.summary) gaps.push("CLI-020 no summary");
  if (!eff.length) gaps.push("CLI-025 no effects declared");
  if (!c.output || !(r.schemas || {})[c.output]) gaps.push(`CLI-020 output schema ${JSON.stringify(c.output)} is not in reference.schemas`);
  if (!(c.error_codes || []).length) gaps.push("CLI-026 no error codes listed");
  if (!ex.length) gaps.push("CLI-020 no example");
  if (isWrite && !(ex.some((x) => /--dry-run/.test(x)) && ex.some((x) => /--confirm/.test(x)))) gaps.push("CLI-030 write examples lack the --dry-run / --confirm pair");
  if (c.dangerous && !ex.every((x) => /--dangerous/.test(x))) gaps.push("CLI-056 a dangerous command's example lacks --dangerous");
  if ((eff.includes("deliver") || eff.includes("configure")) && !c.dangerous) gaps.push("CLI-025 deliver/configure command is not dangerous");
  for (const p of c.params || []) {
    if (secretName.test(p.name) && !p.secret) gaps.push(`SEC-021 parameter ${p.name} looks secret but is not marked secret`);
    if (p.required && p.default !== undefined && p.default !== null) gaps.push(`CLI-024 required parameter ${p.name} has a default`);
  }
  if (!builtin.test(c.path)) {
    const kinds = new Set(ev.map((e) => (/^[a-z]+:/.test(e) ? e.split(":")[0] : "(none)")));
    for (const k of needs[c.maturity] || []) if (!kinds.has(k)) gaps.push(`VER-005 ${c.maturity} without ${k}: evidence`);
    if (kinds.has("(none)")) gaps.push("VER-005 an evidence reference lacks its <kind>: prefix");
    for (const e of ev) {
      const p = e.replace(/^[a-z]+:/, "").split("#")[0];
      if (p && !/^https:\/\//.test(p) && !fs.existsSync(path.join(root, p))) gaps.push(`VER-005 evidence file not found: ${p}`);
    }
  }
  gapCount += gaps.length;
  console.log(`${gaps.length ? "GAP" : "ok "} ${c.path} [${c.type}; ${eff.join("+")}${c.dangerous ? "; dangerous" : ""}; ${c.maturity}]`);
  for (const g of gaps) console.log(`    - ${g}`);
}
const tier = Number(String(r.risk_tier).replace(/^T/, ""));
console.log(`${writes} write command(s); their effects need at least T${floor}; reference declares ${r.risk_tier}${tier < floor ? "  <- SEC-001: below the floor" : ""}`);
process.exit(gapCount || tier < floor ? 1 : 0);
