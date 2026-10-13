#!/usr/bin/env node
// build-manifest.js — spec repository only. Writes the MANIFEST.json that
// sits next to each shared library and lists exactly the files a tool vendors:
//
//   template/go/aicli/MANIFEST.json      (vendored to internal/aicli/)
//   template/python/aicli/MANIFEST.json  (vendored to <pkg>/_aicli/)
//
// Format: {"spec_version": "<contract.json spec_version>", "files": {"<path>": "<sha256>"}}.
// Every file under the library directory is listed except tests and their data
// (*_test.go, test_*.py, testdata/, tests/), the library's own module and
// project files (go.mod, go.sum, pyproject.toml, ...), caches (__pycache__),
// dotfiles and MANIFEST.json itself; spec-files.js holds the exact rule. The
// sha256 is taken over the bytes with CRLF normalised to LF. A library
// directory that does not exist yet is skipped.
//
// sync-spec.js copies the listed files into a tool and check-spec.js verifies
// them, so run this after every library change; CI runs --check.
//
// Usage (from anywhere; paths resolve from this script's location):
//   node template/common/scripts/build-manifest.js           # write both manifests
//   node template/common/scripts/build-manifest.js --check   # exit 1 if a manifest is stale
//   ... --root <spec checkout>                                 # another checkout (tests)

"use strict";

const fs = require("fs");
const path = require("path");
const spec = require("./spec-files.js");

const USAGE = "usage: node template/common/scripts/build-manifest.js [--check] [--root <ai-native-cli-spec checkout>]";

function parseArgs(argv) {
  const out = { check: false, root: "" };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--check") out.check = true;
    else if (a === "--root") {
      out.root = argv[++i];
      if (!out.root) fail(2, `--root needs a directory\n${USAGE}`);
    } else if (a === "-h" || a === "--help") {
      console.log(USAGE);
      process.exit(0);
    } else fail(2, `unknown argument ${a}\n${USAGE}`);
  }
  return out;
}

function fail(code, msg) {
  console.error(msg);
  process.exit(code);
}

function describeDiff(oldText, next) {
  let old;
  try {
    old = JSON.parse(oldText);
  } catch (_) {
    return ["existing MANIFEST.json is not valid JSON"];
  }
  const lines = [];
  if (old.spec_version !== next.spec_version) lines.push(`spec_version ${old.spec_version} -> ${next.spec_version}`);
  const a = (old && old.files) || {};
  const b = next.files;
  for (const k of Object.keys(b)) {
    if (!(k in a)) lines.push(`added    ${k}`);
    else if (a[k] !== b[k]) lines.push(`changed  ${k}`);
  }
  for (const k of Object.keys(a)) if (!(k in b)) lines.push(`removed  ${k}`);
  if (!lines.length) lines.push("formatting differs");
  return lines;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const root = path.resolve(args.root || path.join(__dirname, "..", "..", ".."));
  const contractPath = path.join(root, "template", "common", "contract", "contract.json");
  if (!fs.existsSync(contractPath)) {
    fail(2, `build-manifest.js runs only in the ai-native-cli-spec repository (no template/common/contract/contract.json under ${root})`);
  }
  let specVersion;
  try {
    specVersion = JSON.parse(fs.readFileSync(contractPath, "utf8")).spec_version;
  } catch (e) {
    fail(1, `[manifest] cannot read spec_version from ${contractPath}: ${e.message}`);
  }
  if (!spec.semverCore(specVersion)) fail(1, `[manifest] contract.json spec_version ${JSON.stringify(specVersion)} is not a version`);

  let stale = 0;
  for (const [lang, lib] of Object.entries(spec.LIBRARIES)) {
    const libRoot = spec.toAbs(root, lib.src);
    if (!fs.existsSync(libRoot)) {
      console.log(`[manifest] skip ${lib.src}: not present`);
      continue;
    }
    let manifest;
    try {
      manifest = spec.buildManifest(libRoot, specVersion, lang);
    } catch (e) {
      fail(1, `[manifest] ${e.message}`);
    }
    const text = spec.serializeManifest(manifest);
    const outPath = path.join(libRoot, spec.MANIFEST);
    const existing = spec.readOrNull(outPath);
    const current = existing ? spec.normalizeText(existing).toString("utf8") : null;
    const count = Object.keys(manifest.files).length;
    if (args.check) {
      if (current === text) {
        console.log(`[manifest] OK: ${lib.src}/${spec.MANIFEST} (${count} files, spec ${specVersion})`);
      } else {
        stale++;
        console.error(`[manifest] STALE: ${lib.src}/${spec.MANIFEST}`);
        const diff = current === null ? ["missing"] : describeDiff(current, manifest);
        for (const d of diff) console.error(`    ${d}`);
      }
    } else if (current === text) {
      console.log(`[manifest] unchanged ${lib.src}/${spec.MANIFEST} (${count} files)`);
    } else {
      fs.writeFileSync(outPath, text);
      console.log(`[manifest] wrote ${lib.src}/${spec.MANIFEST} (${count} files, spec ${specVersion})`);
    }
  }
  if (stale) {
    fail(1, `[manifest] ${stale} manifest(s) out of date. Run: node template/common/scripts/build-manifest.js`);
  }
}

main();
