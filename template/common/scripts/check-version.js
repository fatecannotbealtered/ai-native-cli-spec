#!/usr/bin/env node
"use strict";

// Fail-closed version-consistency guard. Asserts every derived location equals
// the source of truth (package.json "version"); the CHANGELOG's newest released
// heading must also match. Wired into ci.yml (every push/PR) and release.yml
// (at tag time), so any drift — even from a hand edit that bypassed
// `npm version` — turns CI red and cannot be merged or published.
//
// Exit 0 = all in sync; exit 1 = drift (prints every offending location).

const path = require("path");
const { sourceVersion, derivedLocations } = require("./version-files");

const root = path.resolve(__dirname, "..");
const version = sourceVersion(root);

const problems = [];
// An absent Skill field is the one problem a resync cannot fix: sync-version
// rewrites values in place and never adds a field it did not find.
let skillFieldMissing = false;
let resyncable = false;
for (const loc of derivedLocations(root)) {
  for (const occ of loc.read()) {
    if (occ.value !== version) {
      if (occ.value === null && loc.label.startsWith("skills/")) skillFieldMissing = true;
      else resyncable = true;
      problems.push(`${loc.label} [${occ.where}]: ${occ.value === null ? "(missing)" : occ.value} != ${version}`);
    }
  }
}

if (problems.length) {
  console.error(`check-version: ${problems.length} location(s) out of sync with package.json (${version}):`);
  for (const p of problems) console.error(`  - ${p}`);
  if (skillFieldMissing) {
    console.error(
      "A Skill field marked (missing) was not found in the form the tooling reads, and sync-version will not add it: " +
        'add `version: "<v>"` to the frontmatter and `"min_version": "<v>"` inside ' +
        "its one-line JSON `metadata` (SKILL-SPEC §2), then re-run."
    );
  }
  if (resyncable) {
    console.error("Run `npm version <patch|minor|major>` (or `node scripts/sync-version.js`) to resync.");
  }
  process.exit(1);
}

console.log(`check-version: OK — all derived locations match ${version}`);
