"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const os = require("os");
const crypto = require("crypto");
const { execFileSync, spawnSync } = require("child_process");
const { check, smoke } = require("../../template/common/scripts/check-platform-delivery.js");

const sha = "a".repeat(40);
const requirements = { minimum_os: "test OS", cpu: "64-bit", external_software: [],
  system_libraries: [], node_wrapper: "Node 20", host: "none" };
const hash = (bytes) => crypto.createHash("sha256").update(bytes).digest("hex");

function binaryFor(platform, arch) {
  const data = Buffer.alloc(128);
  if (platform === "windows") {
    data.write("MZ", 0); data.writeUInt32LE(64, 0x3c); data.write("PE\0\0", 64, "binary");
    data.writeUInt16LE(arch === "amd64" ? 0x8664 : 0xaa64, 68);
  } else if (platform === "linux") {
    Buffer.from([0x7f, 0x45, 0x4c, 0x46, 2, 1]).copy(data);
    data.writeUInt16LE(arch === "amd64" ? 62 : 183, 18);
  } else {
    data.writeUInt32BE(0xcffaedfe, 0);
    data.writeUInt32LE(arch === "amd64" ? 0x01000007 : 0x0100000c, 4);
  }
  return data;
}
function makeArchive(file, binary, data) {
  const script = [
    "import io,sys,tarfile,zipfile",
    "out,name,data=sys.argv[1],sys.argv[2],bytes.fromhex(sys.argv[3])",
    "items=[(name,data),('skills/demo/SKILL.md',b'fixture')]",
    "if out.endswith('.zip'):",
    " with zipfile.ZipFile(out,'w') as z:",
    "  for n,b in items: z.writestr(n,b)",
    "else:",
    " with tarfile.open(out,'w:gz') as t:",
    "  for n,b in items:",
    "   i=tarfile.TarInfo(n); i.size=len(b); t.addfile(i,io.BytesIO(b))",
  ].join("\n");
  const python = process.platform === "win32" ? "python" : "python3";
  execFileSync(python, ["-c", script, file, binary, data.toString("hex")], { windowsHide: true });
}
function setup(t) {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "platform-test-"));
  assert.equal(path.dirname(temp), path.resolve(os.tmpdir()));
  t.after(() => fs.rmSync(temp, { recursive: true, force: true }));
  const archives = path.join(temp, "archives"), records = path.join(temp, "records");
  fs.mkdirSync(archives); fs.mkdirSync(records);
  return { temp, archives, records };
}
function add(f, platform, arch = "amd64", actualArch = arch) {
  const binary = "demo" + (platform === "windows" ? ".exe" : "");
  const archive = "demo-1.2.3-" + platform + "-" + arch + (platform === "windows" ? ".zip" : ".tar.gz");
  const bytes = binaryFor(platform, actualArch), archivePath = path.join(f.archives, archive);
  makeArchive(archivePath, binary, bytes);
  const record = { record_version: "1.0", tool: "demo", product_version: "1.2.3", os: platform, arch,
    archive, archive_sha256: hash(fs.readFileSync(archivePath)), binary, binary_sha256: hash(bytes),
    source_commit: sha, runner: platform + "/" + arch, runner_version: "fixture",
    tested_at: "2026-01-01T00:00:00.000Z", install: "passed", startup: "passed", critical: "passed",
    commands: [{ phase: "startup", argv: [binary, "reference", "--index"], exit_code: 0 },
      { phase: "critical", argv: [binary, "inspect"], exit_code: 0 }],
    cli_schema_version: "2.0", legacy_readiness: null,
    key_operation: { argv: [binary, "inspect"], exit_code: 0, observed: "fixture" }, requirements };
  const file = path.join(f.records, platform + "-" + arch + ".json");
  fs.writeFileSync(file, JSON.stringify(record));
  return { file, record, bytes, archivePath };
}
function invoke(f, expected = sha) { return check(f.archives, f.records, "demo", "1.2.3", expected); }
function three(f) { return ["windows", "darwin", "linux"].map((p) => add(f, p)); }
function update(item) { fs.writeFileSync(item.file, JSON.stringify(item.record)); }

test("checks real archives and accepts multiple architectures without record collisions", (t) => {
  const f = setup(t); three(f); add(f, "linux", "arm64"); add(f, "darwin", "arm64");
  assert.deepEqual(invoke(f), ["darwin", "linux", "windows"]);
});
test("missing OS, invalid structure, mismatched commit and duplicate target are rejected", (t) => {
  const f = setup(t); const items = three(f); const [win] = items;
  fs.unlinkSync(win.file); assert.throws(() => invoke(f), /windows/); update(win);
  assert.throws(() => invoke(f, "b".repeat(40)), /stale or failed/i);
  assert.throws(() => invoke(f, ""), /expected-source-sha/);
  win.record.extra = true; update(win); assert.throws(() => invoke(f), /structure/);
  delete win.record.extra; update(win);
  fs.copyFileSync(win.file, path.join(f.records, "duplicate.json"));
  assert.throws(() => invoke(f), /Duplicate target/);
});
test("wrong architecture and archive or binary digest cannot pass", (t) => {
  const f = setup(t); const [win, mac, linux] = three(f);
  linux.record.arch = "arm64"; update(linux); assert.throws(() => invoke(f), /identity/);
  linux.record.arch = "amd64"; update(linux);
  win.record.archive_sha256 = "0".repeat(64); update(win); assert.throws(() => invoke(f), /Archive digest mismatch/);
  win.record.archive_sha256 = hash(fs.readFileSync(win.archivePath)); update(win);
  mac.record.binary_sha256 = "0".repeat(64); update(mac); assert.throws(() => invoke(f), /binary digest mismatch/);
  mac.record.binary_sha256 = hash(mac.bytes); update(mac);
  add(f, "linux", "arm64", "amd64"); assert.throws(() => invoke(f), /architecture mismatch/);
});
test("an extra archive cannot be published without its own passed smoke", (t) => {
  const f = setup(t); three(f);
  const extra = add(f, "linux", "arm64"); fs.unlinkSync(extra.file);
  assert.throws(() => invoke(f), /no matching passed smoke/);
});
test("failed key operation and incomplete runtime requirements are rejected", (t) => {
  const f = setup(t); const [win] = three(f);
  win.record.critical = "failed"; update(win); assert.throws(() => invoke(f), /failed smoke/);
  win.record.critical = "passed"; win.record.key_operation.exit_code = 1; update(win);
  assert.throws(() => invoke(f), /key-operation/);
  win.record.key_operation.exit_code = 0; win.record.requirements = { ...requirements, host: "" }; update(win);
  assert.throws(() => invoke(f), /runtime requirements/);
});
test("legacy unpublishable is retained but does not block passing package smoke", (t) => {
  const f = setup(t); const items = three(f);
  for (const item of items) {
    item.record.cli_schema_version = "1.0";
    item.record.legacy_readiness = { level: "unpublishable", fcc_status: "missing" };
    item.record.commands.splice(1, 0, { phase: "legacy_readiness", argv: [item.record.binary, "reference"], exit_code: 0 });
    update(item);
  }
  assert.deepEqual(invoke(f), ["darwin", "linux", "windows"]);
});
test("smoke binds installed executable to archived bytes and records actual legacy value", (t) => {
  const f = setup(t);
  const platform = { win32: "windows", darwin: "darwin", linux: "linux" }[process.platform];
  const arch = { x64: "amd64", arm64: "arm64" }[process.arch];
  const item = add(f, platform, arch), binary = path.join(f.temp, item.record.binary);
  const output = path.join(f.temp, "smoke.json");
  fs.writeFileSync(binary, item.bytes);
  fs.mkdirSync(path.join(f.temp, "skills", "demo"), { recursive: true });
  fs.writeFileSync(path.join(f.temp, "skills", "demo", "SKILL.md"), "fixture");
  const outputs = [
    { status: 0, stdout: JSON.stringify({ ok: true, schema_version: "1.0", data: { tool: "demo", version: "1.2.3" } }) },
    { status: 0, stdout: JSON.stringify({ ok: true, schema_version: "1.0", data: { tool: "demo", version: "1.2.3",
      release_readiness: { level: "unpublishable", fcc_status: "unknown" } } }) },
    { status: 0, stdout: JSON.stringify({ argv: [binary, "inspect"], requirements }) },
    { status: 0, stdout: "actual command output" },
    { status: 0, stdout: JSON.stringify({ observed: "fixture" }) },
  ];
  const calls = [];
  const result = smoke(item.archivePath, binary, platform, arch, output, "test.js", "demo", "1.2.3", sha,
    (command, args) => { calls.push([command, args]); return outputs.shift(); });
  assert.equal(result.binary_sha256, hash(item.bytes));
  assert.deepEqual(result.legacy_readiness, { level: "unpublishable", fcc_status: "unknown" });
  assert.ok(calls.some(([command, args]) => command === binary && JSON.stringify(args) === '["inspect"]'));
  fs.writeFileSync(binary, "different binary");
  assert.throws(() => smoke(item.archivePath, binary, platform, arch, output, "test.js", "demo", "1.2.3", sha),
    /differs from archive/);
  assert.equal(JSON.parse(fs.readFileSync(output)).install, "failed");
});
test("key operation failure or no-op leaves failed smoke evidence", (t) => {
  const f = setup(t);
  const platform = { win32: "windows", darwin: "darwin", linux: "linux" }[process.platform];
  const arch = { x64: "amd64", arm64: "arm64" }[process.arch];
  const item = add(f, platform, arch), binary = path.join(f.temp, item.record.binary), output = path.join(f.temp, "out.json");
  fs.writeFileSync(binary, item.bytes);
  fs.mkdirSync(path.join(f.temp, "skills", "demo"), { recursive: true });
  fs.writeFileSync(path.join(f.temp, "skills", "demo", "SKILL.md"), "fixture");
  for (const failure of ["prepare", "noop", "operation", "validate"]) {
    let n = 0;
    assert.throws(() => smoke(item.archivePath, binary, platform, arch, output, "test.js", "demo", "1.2.3", sha,
      () => {
        n++;
        if (n === 1) return { status: 0, stdout: JSON.stringify({ ok: true, schema_version: "2.0", data: { tool: "demo", version: "1.2.3" } }) };
        if (n === 2) return failure === "prepare" ? { status: 1 } :
          { status: 0, stdout: failure === "noop" ? "{}" : JSON.stringify({ argv: [binary, "inspect"], requirements }) };
        if (n === 3) return failure === "operation" ? { status: 1 } : { status: 0, stdout: "actual output" };
        return failure === "validate" ? { status: 1 } : { status: 0, stdout: '{"observed":"ok"}' };
      }), /key-operation/i);
    assert.equal(JSON.parse(fs.readFileSync(output)).critical, "failed");
  }
});
test("command line requires the expected source SHA", () => {
  const script = require.resolve("../../template/common/scripts/check-platform-delivery.js");
  const result = spawnSync(process.execPath, [script, "check", ".", ".", "demo", "1.2.3"], { encoding: "utf8" });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /expected-source-sha/);
});

test("npm keeps the five native targets and labels Windows ARM64 x64 emulation", (t) => {
  const f = setup(t), project = path.join(f.temp, "project"), scripts = path.join(project, "scripts");
  fs.mkdirSync(scripts, { recursive: true });
  const source = path.resolve(__dirname, "../../template/common/scripts/prepare-npm-platform-packages.js");
  fs.copyFileSync(source, path.join(scripts, "prepare-npm-platform-packages.js"));
  const optionalDependencies = {};
  for (const suffix of ["darwin-x64", "darwin-arm64", "linux-x64", "linux-arm64", "win32-x64", "win32-arm64"])
    optionalDependencies["@example/demo-" + suffix] = "1.2.3";
  fs.writeFileSync(path.join(project, "package.json"), JSON.stringify({ name: "@example/demo", version: "1.2.3",
    bin: { demo: "scripts/run.js" }, optionalDependencies }));
  for (const [platform, arch] of [["windows", "amd64"], ["darwin", "amd64"], ["darwin", "arm64"],
    ["linux", "amd64"], ["linux", "arm64"]]) add(f, platform, arch);
  const output = path.join(f.temp, "npm-platform");
  execFileSync(process.execPath, [path.join(scripts, "prepare-npm-platform-packages.js"), f.archives, output],
    { windowsHide: true, stdio: "pipe" });
  const x64 = path.join(output, "example__demo-win32-x64"), arm = path.join(output, "example__demo-win32-arm64");
  const meta = JSON.parse(fs.readFileSync(path.join(arm, "package.json"), "utf8"));
  assert.match(meta.description, /amd64.*x64 emulation.*not native/);
  assert.deepEqual(meta.os, ["win32"]); assert.deepEqual(meta.cpu, ["arm64"]);
  assert.deepEqual(fs.readFileSync(path.join(arm, "bin", "demo.exe")), fs.readFileSync(path.join(x64, "bin", "demo.exe")));
  assert.equal(fs.readdirSync(output).length, 6);
});
