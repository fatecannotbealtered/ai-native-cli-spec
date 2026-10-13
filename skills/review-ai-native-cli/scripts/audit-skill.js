#!/usr/bin/env node
// audit-skill.js <skills-dir> [--tool <name>] [--version <x.y.z>] [--index <reference-index.json>]
//
// Mechanical checks of every <skills-dir>/<name>/SKILL.md and its
// test-prompts.json against SKILL-SPEC 2.0: frontmatter (SKL-001), command-map
// markers and coverage (SKL-002), size and links (SKL-003), STOP CHECKPOINT
// lines (SKL-005), the authority, untrusted, ambiguity, lost-reply and report
// lines (SKL-006..009), family rules (SKL-010) and test prompts (SKL-011:
// id, scenario, prompt, expected and an optional grader_notes, nothing else).
// GAP lines break a rule as written; LEAD lines are text to read before
// deciding. Read the Skill yourself as well: wording rules need judgement.
"use strict";
const fs = require("fs");
const path = require("path");

const args = process.argv.slice(2);
const opt = { dir: "", tool: "", version: "", index: "" };
for (let i = 0; i < args.length; i++) {
  if (args[i] === "--tool") opt.tool = args[++i];
  else if (args[i] === "--version") opt.version = args[++i];
  else if (args[i] === "--index") opt.index = args[++i];
  else opt.dir = args[i];
}
if (!opt.dir) {
  console.error("usage: node audit-skill.js <skills-dir> [--tool <name>] [--version <x.y.z>] [--index <reference-index.json>]");
  process.exit(2);
}

const STANDARD = ["name", "description", "license", "compatibility", "metadata"];
const SCENARIOS = ["read-answer", "write", "ambiguous-target", "injected-instruction", "dangerous-action", "no-scope", "lost-reply", "long-list"];
const PROMPT_KEYS = ["id", "scenario", "prompt", "expected", "grader_notes"];
const START = "<!-- aicli:command-map:start -->";
const END = "<!-- aicli:command-map:end -->";
const builtin = /^(reference|context|doctor|changelog|update|operation( .*)?)$/;

let index = null;
if (opt.index) index = JSON.parse(fs.readFileSync(opt.index, "utf8")).data;
const skills = fs.readdirSync(opt.dir).filter((n) => fs.existsSync(path.join(opt.dir, n, "SKILL.md")));
const entry = opt.tool || (skills.length === 1 ? skills[0] : "");
const covered = new Set();
let gaps = 0;

function unquote(v) {
  v = v.trim();
  if (/^".*"$/.test(v)) return v.slice(1, -1).replace(/\\"/g, '"');
  if (/^'.*'$/.test(v)) return v.slice(1, -1).replace(/''/g, "'");
  return v;
}

for (const name of skills) {
  const out = [];
  const gap = (m) => out.push(`  GAP  ${m}`);
  const lead = (m) => out.push(`  LEAD ${m}`);
  const file = path.join(opt.dir, name, "SKILL.md");
  const text = fs.readFileSync(file, "utf8").replace(/\r\n/g, "\n");
  const lines = text.split("\n");
  const count = lines.length - (text.endsWith("\n") ? 1 : 0);
  if (count > 150) gap(`SKL-003 SKILL.md has ${count} lines (at most 150)`);

  const fm = /^---\n([\s\S]*?)\n---\n/.exec(text);
  const meta = {};
  let fmLines = [];
  if (!fm) gap("SKL-001 no YAML frontmatter");
  else {
    fmLines = fm[1].split("\n");
    const top = fmLines.filter((l) => /^[A-Za-z]/.test(l)).map((l) => l.split(":")[0].trim());
    const extra = top.filter((k) => !STANDARD.includes(k));
    if (extra.length) gap(`SKL-001 top-level keys outside the Agent Skills standard: ${extra.join(", ")}`);
    const value = (key) => {
      const i = fmLines.findIndex((l) => l.startsWith(`${key}:`));
      if (i < 0) return null;
      const v = fmLines[i].slice(key.length + 1).trim();
      if (/^[>|][-+]?$/.test(v)) {
        const block = [];
        for (let j = i + 1; j < fmLines.length && /^\s+\S|^$/.test(fmLines[j]); j++) block.push(fmLines[j].trim());
        return block.join(" ").trim();
      }
      return unquote(v);
    };
    const nm = value("name");
    if (nm !== name) gap(`SKL-001 name ${JSON.stringify(nm)} differs from the directory ${name}`);
    if (nm && (!/^[a-z0-9]+(-[a-z0-9]+)*$/.test(nm) || nm.length > 64 || /anthropic|claude/.test(nm))) gap(`SKL-001 name ${nm} breaks the naming rule`);
    const desc = value("description");
    if (!desc) gap("SKL-001 no description");
    else {
      if (desc.length > 1024) gap(`SKL-001 description has ${desc.length} characters (at most 1024)`);
      if (!/\bnot (for|meant for|used for)\b|\bdo not use\b/i.test(desc)) lead("SKL-001 the description may lack its not-for clause");
    }
    const mi = fmLines.findIndex((l) => /^metadata:\s*$/.test(l));
    if (mi >= 0) {
      for (let j = mi + 1; j < fmLines.length && /^\s+/.test(fmLines[j]); j++) {
        const m = /^\s+([\w.-]+):\s*(.*)$/.exec(fmLines[j]);
        if (m) {
          meta[m[1]] = unquote(m[2]);
          if (!/^["']/.test(m[2].trim())) lead(`SKL-001 metadata.${m[1]} is not quoted (values must stay strings)`);
        }
      }
    }
    for (const k of ["version", "min_version", "requires.bins"]) if (!(k in meta)) gap(`SKL-001 metadata.${k} is missing`);
    if (opt.version) {
      for (const k of ["version", "min_version"]) if (k in meta && meta[k] !== opt.version) gap(`SKL-001 metadata.${k} is ${meta[k]}, the tool is ${opt.version}`);
    }
    if (fmLines.some((l) => /^allowed-tools:/.test(l))) gap("SKL-001 allowed-tools pre-approves the binary's writes");
  }

  const isEntry = !entry || name === entry;
  const domain = entry && name !== entry;
  if (domain) {
    if (meta["requires.skills"] !== entry) gap(`SKL-010 metadata.requires.skills should be "${entry}"`);
    const head = text.slice(fm ? fm[0].length : 0).split("\n").slice(0, 15).join("\n");
    if (!head.includes(`../${entry}/SKILL.md`)) gap(`SKL-010 the body does not open by sending the agent to ../${entry}/SKILL.md`);
  }

  const i = text.indexOf(START);
  const j = text.indexOf(END);
  if (i < 0 || j < i) {
    gap(`SKL-002 no command map between ${START} and ${END}`);
    if (/command-map:begin/.test(text)) lead("SKL-002 uses the pre-release command-map:begin markers");
  } else if (index && index.commands) {
    const block = text.slice(i + START.length, j);
    const mine = index.commands.filter((c) => !builtin.test(c.path));
    for (const c of mine) {
      const words = c.path.split(" ");
      const verb = words.pop();
      if (!block.includes(words.join(" ") || verb) || !new RegExp(`\\b${verb.replace(/[-]/g, "\\-")}\\b`).test(block)) lead(`SKL-002 the map may not name ${c.path}`);
      else if (c.dangerous && !new RegExp(`\\b${verb.replace(/[-]/g, "\\-")} \\(dangerous\\)`).test(block)) lead(`SKL-002 the map may not mark ${c.path} dangerous`);
    }
  }

  const stops = (text.match(/STOP CHECKPOINT/g) || []).length;
  if (isEntry && stops === 0) gap("SKL-005 no STOP CHECKPOINT lines");
  else out.push(`  info ${stops} STOP CHECKPOINT line(s); SKL-005 expects the three risk-scaled ones and none on explicit requests`);

  const body = fm ? text.slice(fm[0].length) : text;
  const has = (re) => re.test(body);
  if (isEntry) {
    if (!has(/details\.scope/)) gap("SKL-006 no line on E_FORBIDDEN with details.scope");
    if (!has(/meta\.untrusted/)) gap("SKL-007 no line on meta.untrusted");
    if (!has(/E_AMBIGUOUS|candidates/)) gap("SKL-007 no ambiguity line");
    if (!has(/operation show/)) gap("SKL-007 no lost-reply line (operation show before any retry)");
    if (!has(/^#+ .*report/im)) lead("SKL-008 no report section heading");
    if (!has(/\bupdate\b/)) lead("SKL-009 no unknown-command / after-update line");
  }
  body.split("\n").forEach((l, n) => {
    const at = `line ${n + 1 + (fm ? fm[0].split("\n").length - 1 : 0)}`;
    if (/\{\{[A-Z_0-9]+\}\}/.test(l)) gap(`${at} leftover template placeholder`);
    if (/\breference\b(?!\s*--(index|command)|\/)/.test(l) && /\b(load|read|run|fetch)\b/i.test(l) && !/never|not |no need/i.test(l)) lead(`SKL-003 ${at} may load the full reference: ${l.trim().slice(0, 100)}`);
    if (/\b(first|before (any|every|each|you)|start (by|with))\b.{0,60}\b(context|doctor|reference)\b/i.test(l) && !/never|not |no need/i.test(l)) lead(`SKL-003 ${at} may be a bootstrap step: ${l.trim().slice(0, 100)}`);
    if (/\|\s*exit\s*\|/i.test(l)) lead(`SKL-004 ${at} looks like an exit-code table`);
    if (/npx skills(@\S+)? add|git clone|curl\s+\S*skill/i.test(l)) lead(`SKL-002/SKL-009 ${at} installs or fetches Skills (no install instructions in a Skill): ${l.trim().slice(0, 100)}`);
    if (/always (ask|confirm|check with)|never \w+ .{0,30}without (asking|confirmation|approval)/i.test(l)) lead(`SKL-005 ${at} may stop explicit requests: ${l.trim().slice(0, 100)}`);
    if (/\b(create|write|edit|update)\b.{0,40}\bscope(\.json| file)/i.test(l) && !/never|unless|do not|don't/i.test(l)) lead(`SKL-006 ${at} may tell the agent to write the scope file: ${l.trim().slice(0, 100)}`);
  });
  const codes = new Set(body.match(/\bE_[A-Z0-9][A-Z0-9_]+/g) || []);
  if (codes.size > 6) lead(`SKL-004 names ${codes.size} error codes; a copied decision tree?`);
  for (const m of body.matchAll(/\]\(([^)#\s]+\.md)\)/g)) {
    const target = m[1];
    if (/^https?:/.test(target)) continue;
    if (!fs.existsSync(path.join(opt.dir, name, target))) gap(`SKL-003 linked file ${target} does not exist`);
    if (target.split("/").length > 2) lead(`SKL-003 ${target} is more than one level deep`);
  }

  const tp = path.join(opt.dir, name, "test-prompts.json");
  if (!fs.existsSync(tp)) gap("SKL-011 no test-prompts.json");
  else {
    let prompts = [];
    try {
      prompts = JSON.parse(fs.readFileSync(tp, "utf8"));
    } catch (e) {
      gap(`SKL-011 test-prompts.json does not parse: ${e.message}`);
    }
    if (!Array.isArray(prompts)) gap("SKL-011 test-prompts.json is not an array");
    else {
      const ids = new Set();
      prompts.forEach((p, k) => {
        const where = `test-prompts[${k}] ${p.id || ""}`.trim();
        for (const key of ["id", "scenario", "prompt", "expected"]) if (!p[key]) gap(`SKL-011 ${where} lacks ${key}`);
        const extra = Object.keys(p).filter((key) => !PROMPT_KEYS.includes(key));
        if (extra.length) gap(`SKL-011 ${where} has keys outside id, scenario, prompt, expected, grader_notes: ${extra.join(", ")}`);
        if (p.id && !/^[a-z0-9]+(-[a-z0-9]+)*$/.test(p.id)) gap(`SKL-011 ${where} id is not kebab-case`);
        if (p.id && ids.has(p.id)) gap(`SKL-011 duplicate id ${p.id}`);
        ids.add(p.id);
        if (/\{\{[A-Z_0-9]+\}\}/.test(JSON.stringify(p))) gap(`SKL-011 ${where} still has template placeholders`);
        if (p.scenario) covered.add(p.scenario);
      });
      out.push(`  info ${prompts.length} test prompt(s); scenarios: ${[...new Set(prompts.map((p) => p.scenario))].join(", ")}`);
    }
  }
  gaps += out.filter((l) => l.startsWith("  GAP")).length;
  console.log(`${name} (${count} lines)`);
  for (const l of out) console.log(l);
}
const missing = SCENARIOS.filter((s) => !covered.has(s));
if (missing.length) {
  gaps++;
  console.log(`GAP SKL-011 / VER-006 scenarios not covered across the Skills: ${missing.join(", ")}`);
} else console.log("ok  every VER-006 scenario has a test prompt (add `update` when the tool has an update command)");
process.exit(gaps ? 1 : 0);
