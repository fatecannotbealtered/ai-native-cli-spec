#!/usr/bin/env node
"use strict";

// Versioned per-target package evidence. Run smoke on the target OS; check
// validates those records against the actual archives before a release.
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const osInfo = require("os");
const { spawnSync } = require("child_process");

const requiredOS = ["windows", "darwin", "linux"];
const allowedArch = ["amd64", "arm64"];
const runtimeOS = { win32: "windows", darwin: "darwin", linux: "linux" };
const runtimeArch = { x64: "amd64", arm64: "arm64" };
const digest = (bytes) => crypto.createHash("sha256").update(bytes).digest("hex");
const hash = (file) => digest(fs.readFileSync(file));
const string = (v) => typeof v === "string" && v.trim().length > 0;
const strings = (v) => Array.isArray(v) && v.every(string);
const hex256 = (v) => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
const exactKeys = (v, keys) => v && typeof v === "object" && !Array.isArray(v) &&
  Object.keys(v).length === keys.length && keys.every((key) => Object.prototype.hasOwnProperty.call(v, key));
const fileBase = (v) => path.posix.basename(v.replace(/\\/g, "/"));

function validateIdentity(name, version, expectedSha) {
  if (!string(name) || !/^[a-z0-9][a-z0-9-]*$/.test(name) ||
      !string(version) || !/^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/.test(version)) {
    throw new Error("Invalid product name or version");
  }
  if (typeof expectedSha !== "string" || !/^[a-f0-9]{40}$/.test(expectedSha)) {
    throw new Error("An explicit 40-digit lowercase --expected-source-sha is required");
  }
}
function archiveName(name, version, os, arch) {
  return name + "-" + version + "-" + os + "-" + arch + (os === "windows" ? ".zip" : ".tar.gz");
}
function binaryName(name, os) { return name + (os === "windows" ? ".exe" : ""); }
function hasSkill(dir) {
  if (!fs.existsSync(dir) || !fs.statSync(dir).isDirectory()) return false;
  return fs.readdirSync(dir, { withFileTypes: true })
    .some((entry) => entry.isDirectory() && fs.existsSync(path.join(dir, entry.name, "SKILL.md")));
}

function inspectBinary(bytes, os, arch) {
  const machine = arch === "amd64" ? 0x01000007 : 0x0100000c;
  if (os === "linux") {
    if (bytes.length < 20 || !bytes.subarray(0, 4).equals(Buffer.from([0x7f, 0x45, 0x4c, 0x46])) ||
        bytes[4] !== 2 || ![1, 2].includes(bytes[5])) throw new Error("Invalid Linux ELF executable");
    const value = bytes[5] === 1 ? bytes.readUInt16LE(18) : bytes.readUInt16BE(18);
    if (value !== (arch === "amd64" ? 62 : 183)) throw new Error("Linux binary architecture mismatch");
  } else if (os === "windows") {
    if (bytes.length < 64 || bytes.toString("ascii", 0, 2) !== "MZ") throw new Error("Invalid Windows PE executable");
    const offset = bytes.readUInt32LE(0x3c);
    if (offset + 6 > bytes.length || bytes.toString("binary", offset, offset + 4) !== "PE\0\0")
      throw new Error("Invalid Windows PE executable");
    if (bytes.readUInt16LE(offset + 4) !== (arch === "amd64" ? 0x8664 : 0xaa64))
      throw new Error("Windows binary architecture mismatch");
  } else {
    if (bytes.length < 8) throw new Error("Invalid macOS Mach-O executable");
    const magic = bytes.readUInt32BE(0);
    let machines = [];
    if (magic === 0xcffaedfe) machines = [bytes.readUInt32LE(4)];
    else if (magic === 0xfeedfacf) machines = [bytes.readUInt32BE(4)];
    else if (magic === 0xcafebabe || magic === 0xcafebabf) {
      const count = bytes.readUInt32BE(4), stride = magic === 0xcafebabe ? 20 : 32;
      if (count < 1 || count > 32 || 8 + count * stride > bytes.length) throw new Error("Invalid macOS universal binary");
      for (let i = 0; i < count; i++) machines.push(bytes.readUInt32BE(8 + i * stride));
    } else throw new Error("Invalid macOS Mach-O executable");
    if (!machines.includes(machine)) throw new Error("macOS binary architecture mismatch");
  }
}

function archiveBinary(archive, expectedBinary, os, arch) {
  if (!fs.statSync(archive).isFile() || !fs.statSync(archive).size) throw new Error("Archive missing or empty");
  const options = { windowsHide: true, timeout: 60000, maxBuffer: 256 * 1024 * 1024 };
  // Windows ships bsdtar with ZIP support. GNU tar on Linux does not read
  // ZIP, so use the same unzip utility used by npm repackaging there and on macOS.
  const unzip = archive.endsWith(".zip") && process.platform !== "win32";
  const listing = unzip ? spawnSync("unzip", ["-Z1", archive], options) : spawnSync("tar", ["-tf", archive], options);
  if (listing.error || listing.status !== 0) throw new Error("Cannot read release archive");
  const names = listing.stdout.toString("utf8").split(/\r?\n/).filter(Boolean);
  const matches = names.filter((name) => name.replace(/^(\.\/)+/, "") === expectedBinary);
  if (matches.length !== 1) throw new Error("Archive must contain exactly one root executable named " + expectedBinary);
  const contents = unzip ? spawnSync("unzip", ["-p", archive, matches[0]], options) :
    spawnSync("tar", ["-xOf", archive, matches[0]], options);
  if (contents.error || contents.status !== 0 || !contents.stdout.length) throw new Error("Cannot read executable from release archive");
  inspectBinary(contents.stdout, os, arch);
  return digest(contents.stdout);
}

function validateRequirements(requirements) {
  if (!exactKeys(requirements, ["minimum_os", "cpu", "external_software", "system_libraries", "node_wrapper", "host"]) ||
      !string(requirements.minimum_os) || !string(requirements.cpu) || !strings(requirements.external_software) ||
      !strings(requirements.system_libraries) || !string(requirements.node_wrapper) || !string(requirements.host)) {
    throw new Error("Invalid runtime requirements: declare minimum OS, CPU, software, libraries, Node wrapper and host");
  }
}
function validateRecord(record, file, name, version, expectedSha) {
  const fields = ["record_version", "tool", "product_version", "os", "arch", "archive", "archive_sha256",
    "binary", "binary_sha256", "source_commit", "runner", "runner_version", "tested_at",
    "install", "startup", "critical", "commands", "cli_schema_version", "legacy_readiness",
    "key_operation", "requirements"];
  if (!exactKeys(record, fields) || record.record_version !== "1.0" || record.tool !== name ||
      record.product_version !== version || !requiredOS.includes(record.os) || !allowedArch.includes(record.arch))
    throw new Error("Invalid record structure or target: " + file);
  const binary = binaryName(name, record.os);
  if (record.archive !== archiveName(name, version, record.os, record.arch) || record.binary !== binary ||
      !hex256(record.archive_sha256) || !hex256(record.binary_sha256)) throw new Error("Invalid archive or binary identity: " + file);
  if (record.source_commit !== expectedSha || record.runner !== record.os + "/" + record.arch ||
      !string(record.runner_version) || typeof record.tested_at !== "string" ||
      Number.isNaN(Date.parse(record.tested_at)) || new Date(record.tested_at).toISOString() !== record.tested_at ||
      record.install !== "passed" || record.startup !== "passed" || record.critical !== "passed")
    throw new Error("Stale or failed smoke evidence: " + file);
  if (!["1.0", "2.0"].includes(record.cli_schema_version)) throw new Error("Invalid CLI schema version: " + file);
  const phases = record.cli_schema_version === "1.0" ? ["startup", "legacy_readiness", "critical"] : ["startup", "critical"];
  if (!Array.isArray(record.commands) || record.commands.length !== phases.length ||
      record.commands.some((c, i) => !exactKeys(c, ["phase", "argv", "exit_code"]) ||
        c.phase !== phases[i] || !strings(c.argv) || c.argv.length < 2 || c.exit_code !== 0))
    throw new Error("Invalid smoke commands: " + file);
  const startup = record.commands[0].argv;
  if (startup.length !== 3 || fileBase(startup[0]) !== binary || startup[1] !== "reference" || startup[2] !== "--index")
    throw new Error("Invalid startup command: " + file);
  const critical = record.commands[record.commands.length - 1].argv;
  if (fileBase(critical[0]) !== binary) throw new Error("Invalid key-operation invocation: " + file);
  if (record.cli_schema_version === "1.0") {
    const readiness = record.legacy_readiness, argv = record.commands[1].argv;
    if (!exactKeys(readiness, ["level", "fcc_status"]) ||
        !["stable", "beta", "unpublishable"].includes(readiness.level) ||
        !["verified", "missing", "not_applicable", "unknown"].includes(readiness.fcc_status) ||
        argv.length !== 2 || fileBase(argv[0]) !== binary || argv[1] !== "reference")
      throw new Error("Invalid legacy readiness: " + file);
  } else if (record.legacy_readiness !== null) throw new Error("Unexpected legacy readiness: " + file);
  const key = record.key_operation;
  if (!exactKeys(key, ["argv", "exit_code", "observed"]) || !strings(key.argv) || key.argv.length < 2 ||
      fileBase(key.argv[0]) !== binary || key.exit_code !== 0 || !string(key.observed) ||
      JSON.stringify(key.argv) !== JSON.stringify(critical))
    throw new Error("Invalid key-operation result: " + file);
  validateRequirements(record.requirements);
}

function check(archivesDir, recordsDir, name, version, expectedSha) {
  validateIdentity(name, version, expectedSha);
  const files = fs.readdirSync(recordsDir).filter((file) => file.endsWith(".json")).sort();
  const passed = new Set(), targets = new Set(), checkedArchives = new Set();
  for (const file of files) {
    const record = JSON.parse(fs.readFileSync(path.join(recordsDir, file), "utf8"));
    validateRecord(record, file, name, version, expectedSha);
    const target = record.os + "/" + record.arch;
    if (targets.has(target)) throw new Error("Duplicate target: " + target);
    targets.add(target);
    const archive = path.join(archivesDir, record.archive);
    if (hash(archive) !== record.archive_sha256) throw new Error("Archive digest mismatch: " + record.archive);
    if (archiveBinary(archive, record.binary, record.os, record.arch) !== record.binary_sha256)
      throw new Error("Archived binary digest mismatch: " + record.archive);
    checkedArchives.add(record.archive);
    passed.add(record.os);
  }
  for (const os of requiredOS) if (!passed.has(os)) throw new Error("No passed package smoke for " + os);
  for (const entry of fs.readdirSync(archivesDir, { withFileTypes: true })) {
    if (entry.isFile() && /\.(zip|tar\.gz)$/.test(entry.name) && !checkedArchives.has(entry.name))
      throw new Error("Archive has no matching passed smoke: " + entry.name);
  }
  return [...passed].sort();
}

function smoke(archive, binary, os, arch, output, productSmoke, name, version, expectedSha, run = spawnSync) {
  validateIdentity(name, version, expectedSha);
  if (!requiredOS.includes(os) || !allowedArch.includes(arch)) throw new Error("Unknown OS or architecture");
  const record = { record_version: "1.0", tool: name, product_version: version, os, arch,
    archive: path.basename(archive), archive_sha256: null, binary: binaryName(name, os), binary_sha256: null,
    source_commit: expectedSha, runner: (runtimeOS[process.platform] || process.platform) + "/" + (runtimeArch[process.arch] || process.arch),
    runner_version: osInfo.release(), tested_at: new Date().toISOString(), install: "failed", startup: "not_run",
    critical: "not_run", commands: [], cli_schema_version: null, legacy_readiness: null, key_operation: null, requirements: null };
  let error;
  try {
    if (process.env.GITHUB_SHA && process.env.GITHUB_SHA !== expectedSha) throw new Error("Expected source SHA differs from runner commit");
    if (record.runner !== os + "/" + arch) throw new Error("Runner does not match target; emulation requires separate testing");
    if (record.archive !== archiveName(name, version, os, arch) || fileBase(binary) !== record.binary)
      throw new Error("Archive or executable name does not match product and target");
    record.archive_sha256 = hash(archive);
    record.binary_sha256 = archiveBinary(archive, record.binary, os, arch);
    if (!fs.statSync(binary).isFile() || hash(binary) !== record.binary_sha256 ||
        !hasSkill(path.join(path.dirname(binary), "skills"))) throw new Error("Installed executable differs from archive or Skills are missing");
    record.install = "passed";
    record.startup = "failed";
    const first = run(binary, ["reference", "--index"], { encoding: "utf8", timeout: 60000, windowsHide: true });
    record.commands.push({ phase: "startup", argv: [binary, "reference", "--index"], exit_code: first.status });
    if (first.error || first.status !== 0) throw new Error("Installed CLI could not start or run reference --index");
    let envelope;
    try { envelope = JSON.parse(first.stdout); } catch { throw new Error("Startup output is not JSON"); }
    if (envelope.ok !== true || !["1.0", "2.0"].includes(envelope.schema_version) ||
        envelope.data?.tool !== name || envelope.data?.version !== version)
      throw new Error("Installed CLI identity, version or schema does not match the archive");
    record.cli_schema_version = envelope.schema_version;
    if (envelope.schema_version === "1.0") {
      const full = run(binary, ["reference"], { encoding: "utf8", timeout: 60000, windowsHide: true });
      record.commands.push({ phase: "legacy_readiness", argv: [binary, "reference"], exit_code: full.status });
      if (full.error || full.status !== 0) throw new Error("Cannot read legacy readiness");
      let reference;
      try { reference = JSON.parse(full.stdout); } catch { throw new Error("Legacy reference is not JSON"); }
      const readiness = reference.data?.release_readiness;
      if (reference.ok !== true || reference.schema_version !== envelope.schema_version ||
          reference.data?.tool !== name || reference.data?.version !== version ||
          !readiness || !["stable", "beta", "unpublishable"].includes(readiness.level) ||
          !["verified", "missing", "not_applicable", "unknown"].includes(readiness.fcc_status))
        throw new Error("Invalid legacy readiness");
      record.legacy_readiness = { level: readiness.level, fcc_status: readiness.fcc_status };
    }
    record.startup = "passed";
    record.critical = "failed";
    const prepare = run(process.execPath, [productSmoke, "prepare", binary], { encoding: "utf8", timeout: 300000, windowsHide: true });
    if (prepare.stderr) process.stderr.write(prepare.stderr);
    if (prepare.error || prepare.status !== 0) throw new Error("Product key-operation smoke could not prepare");
    let plan;
    try { plan = JSON.parse(prepare.stdout); } catch { throw new Error("Key-operation preparation did not return JSON"); }
    if (!exactKeys(plan, ["argv", "requirements"]) || !strings(plan.argv) || plan.argv.length < 2 || plan.argv[0] !== binary)
      throw new Error("Invalid key-operation plan");
    validateRequirements(plan.requirements);
    const key = run(binary, plan.argv.slice(1), { encoding: "utf8", timeout: 300000, windowsHide: true, maxBuffer: 4 * 1024 * 1024 });
    record.commands.push({ phase: "critical", argv: plan.argv, exit_code: key.status });
    if (key.error || key.status !== 0) throw new Error("Product key-operation smoke failed");
    const verify = run(process.execPath, [productSmoke, "validate", binary], { input: key.stdout,
      encoding: "utf8", timeout: 300000, windowsHide: true });
    if (verify.stderr) process.stderr.write(verify.stderr);
    if (verify.error || verify.status !== 0) throw new Error("Product key-operation result validation failed");
    let result;
    try { result = JSON.parse(verify.stdout); } catch { throw new Error("Key-operation validation did not return JSON"); }
    if (!exactKeys(result, ["observed"]) || !string(result.observed)) throw new Error("Invalid key-operation observation");
    record.key_operation = { argv: plan.argv, exit_code: key.status, observed: result.observed };
    record.requirements = plan.requirements;
    record.critical = "passed";
  } catch (e) { error = e; }
  fs.mkdirSync(path.dirname(output), { recursive: true });
  fs.writeFileSync(output, JSON.stringify(record, null, 2) + "\n");
  if (error) throw error;
  return record;
}

if (require.main === module) {
  try {
    const [mode, ...args] = process.argv.slice(2);
    if (mode === "check" && args.length === 6 && args[4] === "--expected-source-sha")
      console.log("Package smoke passed: " + check(...args.slice(0, 4), args[5]).join(", "));
    else if (mode === "smoke" && args.length === 10 && args[8] === "--expected-source-sha") {
      smoke(...args.slice(0, 8), args[9]);
      console.log("Recorded package smoke: " + args[4]);
    } else throw new Error("Usage: check-platform-delivery.js check <archives-dir> <records-dir> <name> <version> --expected-source-sha <sha> | smoke <archive> <binary> <os> <arch> <record.json> <product-smoke.js> <name> <version> --expected-source-sha <sha>");
  } catch (e) { console.error(e.message); process.exitCode = 1; }
}
module.exports = { check, smoke };
