// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"

	"github.com/spf13/cobra"
)

// The built-in update (CLI-070) against fakes only: an httptest server for the
// GitHub API and the downloads, a fake Sigstore verifier, a fake process runner
// and temporary install and Skill directories. No test touches the network.

const (
	testRepo     = "fatecannotbealtered/notes-cli-example"
	testIdentity = "https://github.com/fatecannotbealtered/notes-cli-example/.github/workflows/release.yml@refs/tags/v1.3.1"
)

type verifyCall struct {
	artifact, bundle []byte
	identity, issuer string
}

// updateFixture is one fake world: releases, an installed binary, Skill roots.
type updateFixture struct {
	t        *testing.T
	h        *tharness
	dir      string
	server   *httptest.Server
	mu       sync.Mutex
	requests []string
	releases []map[string]any
	assets   map[string][]byte
	exe      string
	roots    []string
	// method is the install method the fake detection reports ("" = DetectInstallMethod).
	method      string
	verifyErr   error
	verifyCalls []verifyCall
	runCalls    [][]string
	// managerInstalls is the version a package-manager run installs ("" = none);
	// managerErr is the error it reports; managerMissing simulates it not on PATH.
	managerInstalls string
	managerErr      error
	managerMissing  bool
	skillCopy       func(src, dst string) error
	override        func(w http.ResponseWriter, r *http.Request) bool
}

func binName() string {
	if runtime.GOOS == "windows" {
		return "notes.exe"
	}
	return "notes"
}

func skillMD(version string) string {
	return "---\nname: notes\ndescription: Read and change notes.\nmetadata:\n  version: \"" + version +
		"\"\n  min_version: \"" + version + "\"\n---\n# notes\n"
}

func newUpdateFixture(t *testing.T) *updateFixture {
	t.Helper()
	f := &updateFixture{t: t, dir: t.TempDir(), assets: map[string][]byte{}}
	f.h = newHarness(t)
	f.roots = []string{filepath.Join(f.dir, "agents"), filepath.Join(f.dir, "claude")}
	f.exe = filepath.Join(f.dir, "bin", binName())
	f.writeFile(f.exe, "old notes 1.3.0")
	// Release assets and redirects must be https (E13): the fake GitHub serves TLS,
	// trusted through NOTES_CA_FILE like a private CA (CLI-110).
	f.server = httptest.NewTLSServer(http.HandlerFunc(f.serve))
	t.Cleanup(f.server.Close)
	ca := filepath.Join(f.dir, "fake-github-ca.pem")
	if err := os.WriteFile(ca, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: f.server.Certificate().Raw}), 0o600); err != nil {
		t.Fatal(err)
	}
	f.h.env["NOTES_CA_FILE"] = ca
	f.h.build = func(u *upstream) *App {
		a := testApp(u)
		a.Version = "1.3.0"
		a.SkillDirs = f.roots
		a.skillCopy = f.skillCopy
		cfg := &UpdateConfig{Repo: testRepo, NPMPackage: "notes-cli-example", APIBase: f.server.URL,
			Verify: func(_ context.Context, artifact, bundle []byte, identity, issuer string) error {
				f.mu.Lock()
				f.verifyCalls = append(f.verifyCalls, verifyCall{artifact, bundle, identity, issuer})
				f.mu.Unlock()
				return f.verifyErr
			},
			Executable: func() (string, error) { return f.exe, nil },
			Run:        f.run,
		}
		if f.method != "" {
			cfg.InstallMethod = func(string) string { return f.method }
		}
		a.Update = cfg
		return a
	}
	return f
}

func (f *updateFixture) writeFile(path, content string) {
	f.t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		f.t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o755); err != nil {
		f.t.Fatal(err)
	}
}

func (f *updateFixture) readFile(path string) string {
	f.t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		f.t.Fatal(err)
	}
	return string(b)
}

// installSkill puts the notes Skill at version into a Skill root.
func (f *updateFixture) installSkill(root, version string) {
	f.writeFile(filepath.Join(root, "notes", "SKILL.md"), skillMD(version))
}

func (f *updateFixture) serve(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	f.requests = append(f.requests, r.URL.RequestURI())
	override := f.override
	f.mu.Unlock()
	if override != nil && override(w, r) {
		return
	}
	base := "/repos/" + testRepo + "/releases"
	switch p := r.URL.Path; {
	case p == base && r.URL.Query().Get("per_page") == "50":
		_ = json.NewEncoder(w).Encode(f.releases)
	case p == base+"/latest":
		for _, rel := range f.releases {
			if rel["draft"] != true && rel["prerelease"] != true {
				_ = json.NewEncoder(w).Encode(rel)
				return
			}
		}
		http.NotFound(w, r)
	case strings.HasPrefix(p, base+"/tags/"):
		tag := strings.TrimPrefix(p, base+"/tags/")
		for _, rel := range f.releases {
			if rel["tag_name"] == tag {
				_ = json.NewEncoder(w).Encode(rel)
				return
			}
		}
		http.NotFound(w, r)
	case strings.HasPrefix(p, "/download/"):
		b, ok := f.assets[strings.TrimPrefix(p, "/download/")]
		if !ok {
			http.NotFound(w, r)
			return
		}
		_, _ = w.Write(b)
	default:
		http.NotFound(w, r)
	}
}

// downloads counts the asset requests the server saw.
func (f *updateFixture) downloads() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, r := range f.requests {
		if strings.HasPrefix(r, "/download/") {
			n++
		}
	}
	return n
}

func (f *updateFixture) requestCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.requests)
}

// run is the fake process runner: `<binary> --version` reports the version written
// in the binary file ("<old|new> notes <version>"); a package manager "installs" by
// rewriting the installed executable.
func (f *updateFixture) run(_ context.Context, argv []string) ([]byte, error) {
	f.mu.Lock()
	f.runCalls = append(f.runCalls, append([]string{}, argv...))
	f.mu.Unlock()
	if len(argv) == 2 && argv[1] == "--version" {
		b, err := os.ReadFile(argv[0])
		if err != nil {
			return nil, err
		}
		fields := strings.Fields(string(b))
		if len(fields) != 3 {
			return []byte("exec format error"), errors.New("exit status 1")
		}
		return []byte(`{"ok":true,"schema_version":"2.0","data":{"tool":"notes","version":"` + fields[2] +
			`","spec_version":"2.0.0","schema_version":"2.0"},"meta":{"duration_ms":1}}` + "\n"), nil
	}
	if contains(updateInstallMethods, argv[0]) {
		if f.managerMissing {
			return nil, &exec.Error{Name: argv[0], Err: exec.ErrNotFound}
		}
		if f.managerInstalls != "" {
			f.writeFile(f.exe, "new notes "+f.managerInstalls)
		}
		if f.managerErr != nil {
			return []byte("npm ERR! code EBUSY"), f.managerErr
		}
		return []byte("added 1 package"), nil
	}
	return nil, fmt.Errorf("unexpected command %v", argv)
}

// archiveFor builds the platform archive of a release: the binary and the Skill at
// the archive root, plus extra entries.
func archiveFor(t *testing.T, binContent, skillVersion string, extra map[string]string) []byte {
	t.Helper()
	files := map[string]string{binName(): binContent, "README.md": "notes"}
	if skillVersion != "" {
		files["skills/notes/SKILL.md"] = skillMD(skillVersion)
		files["skills/notes/reference/commands.md"] = "# commands\n"
	}
	for k, v := range extra {
		files[k] = v
	}
	var buf bytes.Buffer
	if runtime.GOOS == "windows" {
		zw := zip.NewWriter(&buf)
		for _, name := range sortedKeys(files) {
			w, err := zw.Create(name)
			if err != nil {
				t.Fatal(err)
			}
			_, _ = w.Write([]byte(files[name]))
		}
		if err := zw.Close(); err != nil {
			t.Fatal(err)
		}
		return buf.Bytes()
	}
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	for _, name := range sortedKeys(files) {
		hdr := &tar.Header{Name: name, Mode: 0o644, Size: int64(len(files[name])), Typeflag: tar.TypeReg}
		if name == binName() {
			hdr.Mode = 0o755
		}
		if err := tw.WriteHeader(hdr); err != nil {
			t.Fatal(err)
		}
		_, _ = tw.Write([]byte(files[name]))
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func assetName(version string) string {
	ext := "tar.gz"
	if runtime.GOOS == "windows" {
		ext = "zip"
	}
	return fmt.Sprintf("notes-%s-%s-%s.%s", version, runtime.GOOS, runtime.GOARCH, ext)
}

// addRelease publishes a release with its platform archive, checksums.txt and the
// Sigstore bundle (the fake verifier decides whether that verifies).
func (f *updateFixture) addRelease(version, body string, archive []byte) {
	f.t.Helper()
	asset := assetName(version)
	sum := sha256.Sum256(archive)
	f.assets[asset] = archive
	f.assets["checksums-"+version] = []byte(hex.EncodeToString(sum[:]) + "  " + asset + "\n" +
		strings.Repeat("0", 64) + "  notes-" + version + "-plan9-amd64.tar.gz\n")
	f.assets["bundle-"+version] = []byte(`{"mediaType":"application/vnd.dev.sigstore.bundle.v0.3+json"}`)
	url := func(name string) string { return f.server.URL + "/download/" + name }
	f.releases = append(f.releases, map[string]any{"tag_name": "v" + version, "draft": false, "prerelease": false, "body": body,
		"assets": []any{
			map[string]any{"name": asset, "browser_download_url": url(asset)},
			map[string]any{"name": "checksums.txt", "browser_download_url": url("checksums-" + version)},
			map[string]any{"name": "checksums.txt.sigstore.json", "browser_download_url": url("bundle-" + version)},
		}})
}

// mustJSON compares a value with golden JSON (both re-encoded with sorted keys).
func mustJSON(t *testing.T, what string, got any, golden string) {
	t.Helper()
	var want any
	if err := json.Unmarshal([]byte(golden), &want); err != nil {
		t.Fatalf("%s: bad golden: %v", what, err)
	}
	g, err1 := json.Marshal(got)
	w, err2 := json.Marshal(want)
	if err1 != nil || err2 != nil || string(g) != string(w) {
		t.Fatalf("%s:\n got %s\nwant %s", what, g, w)
	}
}

// updateData checks a successful update's data against the update schema rules.
func updateData(t *testing.T, r tout) map[string]any {
	t.Helper()
	if r.exit != 0 {
		t.Fatalf("update failed: %s", r)
	}
	v := &validation{}
	validateUpdateData(v, "data", r.data())
	if v.failed() {
		t.Fatalf("update data does not validate: %v\n%s", v.err(), r.stdout)
	}
	return r.data()
}

func noTempDirsLeft(t *testing.T, dir string) {
	t.Helper()
	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		if strings.Contains(e.Name(), "-update-") || strings.Contains(e.Name(), ".new-") || strings.Contains(e.Name(), ".old-") {
			t.Errorf("left behind in %s: %s", dir, e.Name())
		}
	}
}

// G-U1: --check on a binary install, latest 1.3.1, current 1.3.0.
func TestUpdateGoldenCheck(t *testing.T) {
	f := newUpdateFixture(t)
	f.method = "binary"
	f.addRelease("1.3.1", "### Fixed\n- Paging.", archiveFor(t, "new notes 1.3.1", "1.3.1", nil))
	r := f.h.run("update", "--check")
	mustJSON(t, "G-U1", updateData(t, r), `{"status":"available","current_version":"1.3.0","target_version":"1.3.1","update_available":true,"install_method":"binary","signature_status":"not_checked","skill_sync_status":"skipped"}`)
	if f.downloads() != 0 || f.readFile(f.exe) != "old notes 1.3.0" {
		t.Fatal("--check must change nothing")
	}
	if _, has := r.env["meta"].(map[string]any)["notices"]; has {
		t.Fatalf("update's own output carries no update notice: %s", r)
	}
	cache := f.readFile(filepath.Join(f.h.state, "update-check.json"))
	if !strings.Contains(cache, `"latest_version":"1.3.1"`) || !strings.Contains(cache, `"severity":"info"`) {
		t.Fatalf("the check refreshes the notice cache: %s", cache)
	}
	// --dry-run is a synonym of --check
	r = f.h.run("update", "--dry-run")
	if updateData(t, r)["status"] != "available" || f.downloads() != 0 {
		t.Fatalf("--dry-run: %s", r)
	}
	// a release whose notes have a Security heading makes the notice a warning
	f.releases = nil
	f.addRelease("1.3.1", "Notes\n\n## Security\n\n- Fixed a token leak.", archiveFor(t, "new notes 1.3.1", "1.3.1", nil))
	f.h.run("update", "--check")
	if cache := f.readFile(filepath.Join(f.h.state, "update-check.json")); !strings.Contains(cache, `"severity":"warning"`) {
		t.Fatalf("a security release is a warning: %s", cache)
	}
}

// G-U2: a binary update whose signature verifies; the Skill is installed in one
// root at 1.3.0.
func TestUpdateGoldenBinary(t *testing.T) {
	f := newUpdateFixture(t)
	f.method = "binary"
	f.addRelease("1.3.1", "", archiveFor(t, "new notes 1.3.1", "1.3.1", nil))
	f.installSkill(f.roots[0], "1.3.0")
	r := f.h.run("update")
	mustJSON(t, "G-U2", updateData(t, r), `{"status":"updated","current_version":"1.3.1","target_version":"1.3.1","previous_version":"1.3.0","update_available":false,"install_method":"binary","signature_status":"verified","signature_verified":true,"checksum_verified":true,"skill_sync_status":"synced","binary_replaced":true}`)
	if got := f.readFile(f.exe); got != "new notes 1.3.1" {
		t.Fatalf("the binary was not replaced: %q", got)
	}
	if len(f.verifyCalls) != 1 {
		t.Fatalf("verify calls: %d", len(f.verifyCalls))
	}
	vc := f.verifyCalls[0]
	if vc.identity != testIdentity || vc.issuer != "https://token.actions.githubusercontent.com" ||
		string(vc.artifact) != string(f.assets["checksums-1.3.1"]) || string(vc.bundle) != string(f.assets["bundle-1.3.1"]) {
		t.Fatalf("verify got identity %q issuer %q", vc.identity, vc.issuer)
	}
	if v, _ := skillVersions(f.readFile(filepath.Join(f.roots[0], "notes", "SKILL.md"))); v != "1.3.1" {
		t.Fatalf("the installed Skill was not synced: %s", v)
	}
	if _, err := os.Stat(filepath.Join(f.roots[1], "notes")); err == nil {
		t.Fatal("a Skill missing from a root must not be created")
	}
	binDir := filepath.Dir(f.exe)
	if v, _ := skillVersions(f.readFile(filepath.Join(binDir, "skills", "notes", "SKILL.md"))); v != "1.3.1" {
		t.Fatalf("<exe dir>/skills holds the release's Skills: %s", v)
	}
	if _, err := os.Stat(filepath.Join(binDir, "README.md")); err == nil {
		t.Fatal("only the binary and skills/ are extracted")
	}
	_, oldErr := os.Stat(f.exe + ".old")
	if runtime.GOOS == "windows" && oldErr != nil {
		t.Fatal("on Windows the running binary is renamed aside to <exe>.old")
	}
	if runtime.GOOS != "windows" && oldErr == nil {
		t.Fatal("POSIX renames into place; no .old is left")
	}
	noTempDirsLeft(t, binDir)
	noTempDirsLeft(t, f.roots[0])
	// The installed version is current now (the next run of the new binary): the
	// stale .old goes and the Skills are already in step.
	f.h.build = func(build func(*upstream) *App) func(*upstream) *App {
		return func(u *upstream) *App {
			a := build(u)
			a.Version = "1.3.1"
			return a
		}
	}(f.h.build)
	f.writeFile(f.exe+".old", "old notes 1.3.0")
	r = f.h.run("update")
	if d := updateData(t, r); d["status"] != "up_to_date" || d["skill_sync_status"] != "not_needed" {
		t.Fatalf("second run: %s", r)
	}
	if _, err := os.Stat(f.exe + ".old"); err == nil {
		t.Fatal("a later update removes the stale <exe>.old")
	}
}

// G-U3: an npm install; npm succeeds and the installed binary reports 1.3.1.
func TestUpdateGoldenNPM(t *testing.T) {
	f := newUpdateFixture(t)
	pkg := filepath.Join(f.dir, "prefix", "node_modules", "notes-cli-example")
	f.exe = filepath.Join(pkg, "bin", binName())
	f.writeFile(f.exe, "old notes 1.3.0")
	f.writeFile(filepath.Join(pkg, "package.json"), `{"name":"notes-cli-example","version":"1.3.1"}`)
	f.writeFile(filepath.Join(pkg, "skills", "notes", "SKILL.md"), skillMD("1.3.1"))
	f.addRelease("1.3.1", "", archiveFor(t, "new notes 1.3.1", "1.3.1", nil))
	f.installSkill(f.roots[1], "1.3.0")
	f.managerInstalls = "1.3.1"
	r := f.h.run("update")
	mustJSON(t, "G-U3", updateData(t, r), `{"status":"updated","current_version":"1.3.1","target_version":"1.3.1","previous_version":"1.3.0","update_available":false,"install_method":"npm","signature_status":"not_checked","signature_verified":false,"skill_sync_status":"synced","binary_replaced":true,"command":["npm","install","-g","notes-cli-example@1.3.1"]}`)
	if f.downloads() != 0 || len(f.verifyCalls) != 0 {
		t.Fatal("the package-manager path downloads nothing itself")
	}
	if v, _ := skillVersions(f.readFile(filepath.Join(f.roots[1], "notes", "SKILL.md"))); v != "1.3.1" {
		t.Fatalf("the Skill is synced from the wrapper package: %s", v)
	}
	// --check reports the argv that would run
	f.writeFile(f.exe, "old notes 1.3.0")
	r = f.h.run("update", "--check")
	if c := updateData(t, r)["command"]; fmt.Sprint(c) != "[npm install -g notes-cli-example@1.3.1]" {
		t.Fatalf("check command: %v", c)
	}
}

// G-U4: a signature that does not verify.
func TestUpdateGoldenBadSignature(t *testing.T) {
	f := newUpdateFixture(t)
	f.method = "binary"
	f.verifyErr = errors.New("signature verification failed: certificate identity mismatch")
	f.addRelease("1.3.1", "", archiveFor(t, "new notes 1.3.1", "1.3.1", nil))
	r := f.h.run("update")
	if r.exit != 1 || r.code() != "E_INTEGRITY" || r.message() != "The release signature did not verify, so the update was refused and the installed version is unchanged." {
		t.Fatalf("bad signature: %s", r)
	}
	mustJSON(t, "G-U4", r.details(), `{"dispatch_state":"not_started","next_action":"Do not retry; tell the user the release failed signature verification.","stage":"verify_signature","current_version":"1.3.0","target_version":"1.3.1","binary_replaced":false,"skill_sync_status":"skipped","install_method":"binary"}`)
	if f.readFile(f.exe) != "old notes 1.3.0" {
		t.Fatal("the binary must be untouched")
	}
	noTempDirsLeft(t, filepath.Dir(f.exe))
}

// G-U5: a crafted --target-version is E_USAGE before any request.
func TestUpdateGoldenInjectedTarget(t *testing.T) {
	f := newUpdateFixture(t)
	r := f.h.run("update", "--target-version", "1.0.12&calc")
	if r.exit != 2 || r.code() != "E_USAGE" {
		t.Fatalf("injected target: %s", r)
	}
	mustJSON(t, "G-U5", r.details(), `{"problem":"invalid_value","parameter":"--target-version","expected":"semantic version, e.g. 1.2.3","dispatch_state":"not_started","next_action":"Give --target-version a value from details.expected and run the command again.","stage":"discover","current_version":"1.3.0","binary_replaced":false,"skill_sync_status":"skipped"}`)
	if f.requestCount() != 0 || len(f.runCalls) != 0 {
		t.Fatalf("no request or process may run: %v %v", f.requests, f.runCalls)
	}
}

func TestUpdateChecksumMismatchLeavesTheBinary(t *testing.T) {
	f := newUpdateFixture(t)
	f.method = "binary"
	f.addRelease("1.3.1", "", archiveFor(t, "new notes 1.3.1", "1.3.1", nil))
	f.assets[assetName("1.3.1")] = archiveFor(t, "evil notes 1.3.1", "1.3.1", nil) // not what checksums.txt signed
	r := f.h.run("update")
	if r.exit != 1 || r.code() != "E_INTEGRITY" || r.message() != "The downloaded archive does not match its signed checksum, so it was not installed." {
		t.Fatalf("checksum mismatch: %s", r)
	}
	mustJSON(t, "details", r.details(), `{"dispatch_state":"not_started","next_action":"Do not retry; tell the user the release archive failed its checksum.","stage":"verify_checksum","current_version":"1.3.0","target_version":"1.3.1","binary_replaced":false,"skill_sync_status":"skipped","install_method":"binary"}`)
	if f.readFile(f.exe) != "old notes 1.3.0" {
		t.Fatal("the binary must be untouched")
	}
	noTempDirsLeft(t, filepath.Dir(f.exe))
}

func TestUpdateMissingTargetRelease(t *testing.T) {
	f := newUpdateFixture(t)
	f.method = "binary"
	f.addRelease("1.3.1", "", archiveFor(t, "new notes 1.3.1", "1.3.1", nil))
	r := f.h.run("update", "--target-version", "1.4.0")
	if r.exit != 2 || r.code() != "E_USAGE" || r.message() != "No release v1.4.0 exists in fatecannotbealtered/notes-cli-example." {
		t.Fatalf("missing release: %s", r)
	}
	mustJSON(t, "details", r.details(), `{"problem":"invalid_value","parameter":"--target-version","dispatch_state":"not_started","next_action":"Run `+"`notes update --check`"+` to see the latest version, or give an existing version.","stage":"discover","current_version":"1.3.0","target_version":"1.4.0","binary_replaced":false,"skill_sync_status":"skipped","install_method":"binary"}`)
	// an existing tag installs that version
	r = f.h.run("update", "--target-version", "v1.3.1", "--check")
	if d := updateData(t, r); d["target_version"] != "1.3.1" {
		t.Fatalf("target check: %s", r)
	}
}

func TestUpdateNetworkFailureInDiscover(t *testing.T) {
	f := newUpdateFixture(t)
	f.method = "binary"
	f.server.Close() // nothing listens there now
	r := f.h.run("update", "--check")
	host := strings.TrimPrefix(f.server.URL, "https://")
	if r.exit != 7 || r.code() != "E_NETWORK" || r.message() != "Could not reach "+host+" to check for releases." {
		t.Fatalf("network failure: %s", r)
	}
	if r.env["error"].(map[string]any)["retryable"] != true || r.details()["next_action"] != "Check the network or HTTPS_PROXY, then run `notes update` again; it is safe to repeat." {
		t.Fatalf("network failure details: %s", r)
	}
}

func TestUpdateRateLimitAndHTTPStatus(t *testing.T) {
	f := newUpdateFixture(t)
	f.method = "binary"
	f.override = func(w http.ResponseWriter, _ *http.Request) bool {
		w.Header().Set("X-RateLimit-Remaining", "0")
		w.Header().Set("Retry-After", "30")
		w.WriteHeader(http.StatusForbidden)
		return true
	}
	r := f.h.run("update")
	if r.exit != 7 || r.code() != "E_RATE_LIMITED" || r.message() != "GitHub's API rate limit was hit." ||
		fmt.Sprint(r.details()["retry_after_ms"]) != "30000" ||
		r.details()["next_action"] != "Wait 30 s, or set GITHUB_TOKEN, then run `notes update` again." {
		t.Fatalf("rate limit: %s", r)
	}
	f.override = func(w http.ResponseWriter, _ *http.Request) bool {
		w.WriteHeader(http.StatusBadGateway)
		return true
	}
	if r := f.h.run("update"); r.code() != "E_NETWORK" || r.message() != "The release source answered HTTP 502." {
		t.Fatalf("HTTP status: %s", r)
	}
}

func TestUpdatePackageManagerFailure(t *testing.T) {
	f := newUpdateFixture(t)
	f.method = "npm"
	f.addRelease("1.3.1", "", archiveFor(t, "new notes 1.3.1", "1.3.1", nil))
	f.managerErr = errors.New("exit status 1")
	r := f.h.run("update")
	if r.exit != 1 || r.code() != "E_IO" || r.message() != "`npm install -g notes-cli-example@1.3.1` failed, so notes is still 1.3.0." {
		t.Fatalf("npm failure: %s", r)
	}
	mustJSON(t, "details", r.details(), `{"dispatch_state":"dispatched","next_action":"Run `+"`npm install -g notes-cli-example@1.3.1`"+` yourself, then run `+"`notes update`"+` to sync the Skills.","stage":"replace","current_version":"1.3.0","target_version":"1.3.1","binary_replaced":false,"skill_sync_status":"skipped","install_method":"npm","command":["npm","install","-g","notes-cli-example@1.3.1"]}`)

	// npm finished, but another notes still answers 1.3.0
	f.managerErr = nil
	r = f.h.run("update")
	if r.code() != "E_IO" || r.message() != "npm finished, but `notes --version` still reports 1.3.0." ||
		r.details()["next_action"] != "Check which notes comes first on PATH, then run `notes update` again." {
		t.Fatalf("npm finished without the target: %s", r)
	}
	// npm reported an error, but the target is installed: that is an update. Here
	// no wrapper package holds Skills, so the sync is skipped (DESIGN-ADDITIONS D38
	// step 6); update.schema.json's "updated" rule allows only synced/not_needed,
	// so this data is checked with ValidateEnvelope (run) but not validateUpdateData.
	f.managerErr, f.managerInstalls = errors.New("exit status 1"), "1.3.1"
	r = f.h.run("update")
	// skill_sync_command appears only when a sync could not finish (F10).
	if d := r.data(); r.exit != 0 || d["status"] != "updated" || d["skill_sync_status"] != "skipped" || hasKey(d, "skill_sync_command") {
		t.Fatalf("installed despite the manager's error: %s", r)
	}
	// not on PATH, and no package name
	f.writeFile(f.exe, "old notes 1.3.0")
	f.managerMissing = true
	if r := f.h.run("update"); r.code() != "E_CONFIG" || r.message() != "npm was not found on PATH." || r.details()["dispatch_state"] != "not_started" {
		t.Fatalf("npm missing: %s", r)
	}
	f.method = "pip"
	if r := f.h.run("update"); r.code() != "E_CONFIG" || r.message() != "notes does not know its pip package name, so it cannot update this install." ||
		r.details()["next_action"] != "Reinstall notes with the install command in its README." {
		t.Fatalf("no pip package: %s", r)
	}
}

// A Skill sync that fails after the swap is E_PARTIAL: the new binary runs, the
// Skill still describes the old version.
func TestUpdateSkillSyncFailureIsPartial(t *testing.T) {
	f := newUpdateFixture(t)
	f.method = "binary"
	f.addRelease("1.3.1", "", archiveFor(t, "new notes 1.3.1", "1.3.1", nil))
	f.installSkill(f.roots[0], "1.3.0")
	f.skillCopy = func(string, string) error { return errors.New("no space left on device") }
	r := f.h.run("update")
	if r.exit != 1 || r.code() != "E_PARTIAL" || r.message() != "notes was updated but its Skills were not synced, so the installed Skill still describes the old version." {
		t.Fatalf("partial: %s", r)
	}
	mustJSON(t, "details", r.details(), `{"dispatch_state":"dispatched","next_action":"Run `+"`notes update`"+` again to finish the Skill sync, then read `+"`notes changelog --since 1.3.0`"+`.","status":"partial","stage":"skill_sync","binary_replaced":true,"skill_sync_status":"failed","skill_sync_command":"notes update","previous_version":"1.3.0","target_version":"1.3.1","current_version":"1.3.1","update_available":false,"install_method":"binary","signature_status":"verified"}`)
	if f.readFile(f.exe) != "new notes 1.3.1" {
		t.Fatal("the swap happened before the sync")
	}
	if v, _ := skillVersions(f.readFile(filepath.Join(f.roots[0], "notes", "SKILL.md"))); v != "1.3.0" {
		t.Fatalf("the failed sync left the old Skill in place: %s", v)
	}
	noTempDirsLeft(t, f.roots[0])
}

// Already at the latest version: no download, no package manager; the run finishes
// the Skill sync from the installed package.
func TestUpdateUpToDateSyncsSkills(t *testing.T) {
	f := newUpdateFixture(t)
	f.method = "binary"
	f.addRelease("1.3.0", "", archiveFor(t, "new notes 1.3.0", "1.3.0", nil))
	f.releases = append(f.releases,
		map[string]any{"tag_name": "v1.4.0-rc.1", "prerelease": true, "assets": []any{}},
		map[string]any{"tag_name": "not-a-version", "draft": true, "assets": []any{}})
	f.writeFile(filepath.Join(filepath.Dir(f.exe), "skills", "notes", "SKILL.md"), skillMD("1.3.0"))
	r := f.h.run("update")
	mustJSON(t, "no Skill installed", updateData(t, r), `{"status":"up_to_date","current_version":"1.3.0","target_version":"1.3.0","update_available":false,"install_method":"binary","signature_status":"not_checked","skill_sync_status":"skipped","binary_replaced":false}`)
	f.installSkill(f.roots[0], "1.2.0")
	r = f.h.run("update")
	mustJSON(t, "sync", updateData(t, r), `{"status":"up_to_date","current_version":"1.3.0","target_version":"1.3.0","update_available":false,"install_method":"binary","signature_status":"not_checked","skill_sync_status":"synced","binary_replaced":false}`)
	if v, _ := skillVersions(f.readFile(filepath.Join(f.roots[0], "notes", "SKILL.md"))); v != "1.3.0" {
		t.Fatalf("not synced: %s", v)
	}
	r = f.h.run("update")
	if d := updateData(t, r); d["skill_sync_status"] != "not_needed" {
		t.Fatalf("an installed Skill at this version needs no sync: %s", r)
	}
	if f.downloads() != 0 || len(f.verifyCalls) != 0 || len(f.runCalls) != 0 {
		t.Fatal("an up-to-date run downloads and runs nothing")
	}
	// A linked Skill directory (a symlink, or a junction on Windows) is left alone.
	target := filepath.Join(f.dir, "linked-notes")
	f.writeFile(filepath.Join(target, "SKILL.md"), skillMD("1.0.0"))
	link := filepath.Join(f.roots[1], "notes")
	_ = os.MkdirAll(f.roots[1], 0o755)
	var linkErr error
	if runtime.GOOS == "windows" {
		linkErr = exec.Command("cmd", "/c", "mklink", "/J", link, target).Run()
	} else {
		linkErr = os.Symlink(target, link)
	}
	if linkErr == nil {
		if d := updateData(t, f.h.run("update")); d["skill_sync_status"] != "skipped" {
			t.Fatalf("only a stale link: %v", d)
		}
		if v, _ := skillVersions(f.readFile(filepath.Join(target, "SKILL.md"))); v != "1.0.0" {
			t.Fatal("a linked Skill must be left alone")
		}
		if _, err := os.Lstat(link); err != nil {
			t.Fatalf("the link must stay: %v", err)
		}
	} else {
		t.Logf("no link could be created here (%v); the link case is not exercised", linkErr)
	}
	// A sync-only run that fails is E_PARTIAL as well.
	f.installSkill(f.roots[0], "1.2.0")
	f.skillCopy = func(string, string) error { return errors.New("read-only file system") }
	if r := f.h.run("update"); r.code() != "E_PARTIAL" || r.details()["previous_version"] != "1.3.0" || r.details()["signature_status"] != "not_checked" {
		t.Fatalf("sync-only failure: %s", r)
	}
}

func TestUpdateBinaryPathRefusals(t *testing.T) {
	cases := []struct {
		name    string
		prepare func(f *updateFixture)
		code    string
		stage   string
		message string
	}{
		{"unsafe archive entry", func(f *updateFixture) {
			f.addRelease("1.3.1", "", archiveFor(t, "new notes 1.3.1", "1.3.1", map[string]string{"skills/../../evil.txt": "x"}))
		}, "E_INTEGRITY", "replace", "The release archive holds an unsafe entry `skills/../../evil.txt`, so it was not installed."},
		{"corrupt archive", func(f *updateFixture) {
			f.addRelease("1.3.1", "", []byte("not an archive at all"))
		}, "E_INTEGRITY", "verify_checksum", "The downloaded archive does not match its signed checksum, so it was not installed."},
		{"binary reports another version", func(f *updateFixture) {
			f.addRelease("1.3.1", "", archiveFor(t, "new notes 1.3.2", "1.3.1", nil))
		}, "E_INTEGRITY", "replace", "The downloaded binary reports version 1.3.2, not 1.3.1, so it was not installed."},
		{"binary does not run", func(f *updateFixture) {
			f.addRelease("1.3.1", "", archiveFor(t, "garbage", "1.3.1", nil))
		}, "E_IO", "replace", "The downloaded binary does not run on this machine, so it was not installed."},
		{"no build for this platform", func(f *updateFixture) {
			f.addRelease("1.3.1", "", archiveFor(t, "new notes 1.3.1", "1.3.1", nil))
			f.releases[0]["assets"] = f.releases[0]["assets"].([]any)[1:]
		}, "E_UNSUPPORTED", "download", fmt.Sprintf("Release v1.3.1 has no build for %s/%s.", runtime.GOOS, runtime.GOARCH)},
		{"no signed checksums", func(f *updateFixture) {
			f.addRelease("1.3.1", "", archiveFor(t, "new notes 1.3.1", "1.3.1", nil))
			f.releases[0]["assets"] = f.releases[0]["assets"].([]any)[:2]
		}, "E_INTEGRITY", "verify_signature", "Release v1.3.1 has no signed checksums, so it was not installed."},
		{"trust root unavailable", func(f *updateFixture) {
			f.addRelease("1.3.1", "", archiveFor(t, "new notes 1.3.1", "1.3.1", nil))
			f.verifyErr = fmt.Errorf("%w: refreshing TUF metadata: dial tcp: i/o timeout", ErrTrustRootUnavailable)
		}, "E_NETWORK", "verify_signature", "Could not load the Sigstore trust root, so the release signature could not be checked; nothing was installed."},
		{"interrupted", func(f *updateFixture) {
			f.addRelease("1.3.1", "", archiveFor(t, "new notes 1.3.1", "1.3.1", nil))
			f.verifyErr = context.Canceled
		}, "E_INTERRUPTED", "verify_signature", "The update was interrupted before anything was installed; notes is still 1.3.0."},
		{"no release with a semver tag", func(f *updateFixture) {
			f.addRelease("1.3.1", "", archiveFor(t, "new notes 1.3.1", "1.3.1", nil))
			f.releases[0]["tag_name"] = "nightly"
		}, "E_UNSUPPORTED", "discover", "No release of fatecannotbealtered/notes-cli-example has a semantic-version tag, so there is nothing to update to."},
		{"no release at all", func(*updateFixture) {},
			"E_UNSUPPORTED", "discover", "No release of fatecannotbealtered/notes-cli-example has a semantic-version tag, so there is nothing to update to."},
	}
	nextActions := map[string]string{
		"unsafe archive entry":         "Do not retry; tell the user the release archive is unsafe.",
		"no release with a semver tag": "Report this to the tool's maintainers; nothing was changed.",
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f := newUpdateFixture(t)
			f.method = "binary"
			c.prepare(f)
			r := f.h.run("update")
			if r.code() != c.code || r.details()["stage"] != c.stage || r.message() != c.message {
				t.Fatalf("got %s", r)
			}
			if next, ok := nextActions[c.name]; ok && r.details()["next_action"] != next {
				t.Fatalf("next_action: %v", r.details()["next_action"])
			}
			if r.details()["binary_replaced"] != false || f.readFile(f.exe) != "old notes 1.3.0" {
				t.Fatalf("the binary must be untouched: %s", r)
			}
			noTempDirsLeft(t, filepath.Dir(f.exe))
		})
	}
}

// A release whose tag is not a semantic version is skipped when choosing the latest
// release; a debug line names it.
func TestUpdateSkipsNonSemverTags(t *testing.T) {
	f := newUpdateFixture(t)
	f.method = "binary"
	f.addRelease("1.3.1", "", archiveFor(t, "new notes 1.3.1", "1.3.1", nil))
	f.releases = append(f.releases, map[string]any{"tag_name": "nightly", "assets": []any{}},
		map[string]any{"tag_name": "v9.0.0-rc.1", "prerelease": true, "assets": []any{}})
	f.h.env["NOTES_DEBUG"] = "1"
	r := f.h.run("update", "--check")
	if d := updateData(t, r); d["target_version"] != "1.3.1" {
		t.Fatalf("latest: %s", r)
	}
	if !strings.Contains(r.stderr, "debug: update: skipped release tag nightly (not a semantic version)\n") ||
		!strings.Contains(r.stderr, "debug: update stage: discover\n") {
		t.Fatalf("debug lines:\n%s", r.stderr)
	}
}

func TestUpdateRefusesConfirm(t *testing.T) {
	f := newUpdateFixture(t)
	r := f.h.run("update", "--confirm", "ct2_aaaaaaaaaaaaaaaaaaaaaaaaaa_00000000000000000000000000000000")
	if r.code() != "E_USAGE" || r.message() != "update takes no --confirm token; run `notes update`, or `notes update --check` to only look." {
		t.Fatalf("--confirm: %s", r)
	}
	mustJSON(t, "details", r.details(), `{"problem":"invalid_value","parameter":"--confirm","dispatch_state":"not_started","next_action":"Run `+"`notes update`"+`, or `+"`notes update --check`"+` to only look.","stage":"discover","current_version":"1.3.0","binary_replaced":false,"skill_sync_status":"skipped"}`)
	if f.requestCount() != 0 {
		t.Fatal("--confirm is refused before any request")
	}
}

// The Windows swap renames the running binary aside, uses <exe>.old-<n> when a
// stale .old cannot be removed, and renames the old binary back when the new one
// cannot be renamed in. It runs on every OS (only the rename-aside logic differs).
func TestSwapBinaryWindowsRenamesAside(t *testing.T) {
	dir := t.TempDir()
	write := func(p, s string) {
		if err := os.WriteFile(p, []byte(s), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	read := func(p string) string {
		b, err := os.ReadFile(p)
		if err != nil {
			return "<missing>"
		}
		return string(b)
	}
	exe, newBin := filepath.Join(dir, "notes.exe"), filepath.Join(dir, "new.exe")
	ops := fileOps{rename: os.Rename, remove: os.Remove}

	write(exe, "old")
	write(newBin, "new")
	write(exe+".old", "stale")
	if err := swapBinary("windows", newBin, exe, ops); err != nil {
		t.Fatal(err)
	}
	if read(exe) != "new" || read(exe+".old") != "old" {
		t.Fatalf("swap: exe %q, .old %q", read(exe), read(exe+".old"))
	}

	// a stale .old that cannot be removed: the running binary goes to .old-1
	write(newBin, "newer")
	locked := fileOps{rename: os.Rename, remove: func(string) error { return errors.New("in use") }}
	if err := swapBinary("windows", newBin, exe, locked); err != nil {
		t.Fatal(err)
	}
	if read(exe) != "newer" || read(exe+".old-1") != "new" || read(exe+".old") != "old" {
		t.Fatalf("rename aside to .old-1: exe %q .old-1 %q", read(exe), read(exe+".old-1"))
	}

	// the new binary cannot be renamed in: the old one is renamed back
	write(newBin, "newest")
	calls := 0
	failing := fileOps{remove: os.Remove, rename: func(from, to string) error {
		calls++
		if calls == 2 {
			return &os.LinkError{Op: "rename", Old: from, New: to, Err: errors.New("Access is denied.")}
		}
		return os.Rename(from, to)
	}}
	err := swapBinary("windows", newBin, exe, failing)
	if err == nil || failureReason(err) != "Access is denied" {
		t.Fatalf("a failed swap must be reported: %v", err)
	}
	if read(exe) != "newer" {
		t.Fatalf("the old binary was not restored: %q", read(exe))
	}

	// POSIX: rename into place, executable
	write(newBin, "posix")
	if err := swapBinary("linux", newBin, exe, ops); err != nil || read(exe) != "posix" {
		t.Fatalf("posix swap: %v %q", err, read(exe))
	}
}

func TestUpdateFailedSwapIsIO(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("a running binary's directory is made unwritable only on Windows here")
	}
	f := newUpdateFixture(t)
	f.method = "binary"
	f.addRelease("1.3.1", "", archiveFor(t, "new notes 1.3.1", "1.3.1", nil))
	// Hold the installed binary open without delete sharing: Windows refuses to
	// rename it aside.
	h, err := os.Open(f.exe)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = h.Close() }()
	r := f.h.run("update")
	if r.code() == "" {
		t.Skip("this file system allowed the rename")
	}
	if r.code() != "E_IO" || !strings.HasPrefix(r.message(), "Replacing "+f.exe+" failed: ") || !strings.HasSuffix(r.message(), "; notes is still 1.3.0.") ||
		r.details()["stage"] != "replace" || r.details()["binary_replaced"] != false {
		t.Fatalf("failed swap: %s", r)
	}
}

func TestUpdateIsATreeErrorWhenToolDefined(t *testing.T) {
	for _, withConfig := range []bool{false, true} {
		a := testApp(newUpstream())
		own := &cobra.Command{Use: "update", RunE: func(*cobra.Command, []string) error { return nil }}
		a.Root.AddCommand(own)
		a.Register(own, Meta{Summary: "Update", Effects: []Effect{Observe}, Output: "note", Examples: []string{"notes update"}})
		if withConfig {
			a.Update = &UpdateConfig{Repo: testRepo}
		}
		a.InstallBuiltins()
		a.prepareTree()
		if !contains(a.checkTree(), "`update` is aicli's built-in; set App.Update (CLI-070)") {
			t.Fatalf("a tool-defined update must be a tree error (App.Update set: %v): %v", withConfig, a.checkTree())
		}
		if a.updateCmd != nil {
			t.Fatal("the built-in must not replace the tool's command")
		}
	}
	a := testApp(newUpstream())
	a.Update = &UpdateConfig{Repo: "not a repo", Workflow: "release"}
	a.InstallBuiltins()
	a.prepareTree()
	all := strings.Join(a.checkTree(), "\n")
	if !strings.Contains(all, "App.Update.Repo") || !strings.Contains(all, "App.Update.Workflow") {
		t.Fatalf("bad UpdateConfig: %s", all)
	}
}

// The reference of a tool with the built-in update validates (R01): update is a
// read without --confirm examples, and the risk tier ignores it.
func TestUpdateReferenceValidates(t *testing.T) {
	f := newUpdateFixture(t)
	full := f.h.run("reference")
	if err := validateReferenceFull(full.data()); err != nil {
		t.Fatal(err)
	}
	one := f.h.run("reference", "--command", "update")
	if err := validateReferenceCommand(one.data()); err != nil {
		t.Fatal(err)
	}
	entry := one.data()["command"].(map[string]any)
	if entry["summary"] != "Update the tool and its Skills to the latest release, or to --target-version" ||
		fmt.Sprint(entry["effects"]) != "[observe]" || entry["output"] != "aicli_update" ||
		fmt.Sprint(entry["examples"]) != "[notes update --check notes update notes update --target-version 2.1.0]" {
		t.Fatalf("update entry: %v", entry)
	}
	for _, code := range updateErrorCodes {
		if !listHasString(entry["error_codes"], code) {
			t.Fatalf("update must list %s: %v", code, entry["error_codes"])
		}
	}
	if idx := f.h.run("reference", "--index"); idx.exit != 0 || validateReferenceIndex(idx.data()) != nil {
		t.Fatalf("index: %s", idx)
	}
	tb := &recordTB{}
	h, _ := testHarness(func(u *upstream) *App {
		a := testApp(u)
		a.Update = &UpdateConfig{Repo: testRepo, APIBase: f.server.URL}
		return a
	})
	RunConformance(tb, h)
	if len(tb.errors)+len(tb.fatal) > 0 {
		t.Fatalf("conformance with the built-in update:\n%s", strings.Join(append(tb.errors, tb.fatal...), "\n"))
	}
}

// doctor refreshes the notice cache from the latest release (3 s, failures ignored).
func TestDoctorRefreshesTheUpdateNotice(t *testing.T) {
	f := newUpdateFixture(t)
	f.addRelease("2.0.0", "", archiveFor(t, "new notes 2.0.0", "2.0.0", nil))
	r := f.h.run("doctor")
	if r.exit != 0 || validateDoctor(r.data()) != nil {
		t.Fatalf("doctor: %s", r)
	}
	notices, _ := r.env["meta"].(map[string]any)["notices"].([]any)
	if len(notices) != 1 || notices[0].(map[string]any)["latest_version"] != "2.0.0" || notices[0].(map[string]any)["severity"] != "warning" {
		t.Fatalf("doctor should carry the refreshed notice: %s", r)
	}
	f.server.Close()
	if r := f.h.run("doctor"); r.exit != 0 {
		t.Fatalf("a failed refresh is ignored: %s", r)
	}
}

func TestDetectInstallMethod(t *testing.T) {
	cases := map[string]string{
		"/usr/lib/node_modules/notes-cli-example/bin/notes":                                           "npm",
		`C:\Users\me\AppData\Roaming\npm\node_modules\notes-cli-example\bin\notes.exe`:                "npm",
		"/home/me/.local/share/pnpm/global/5/node_modules/.pnpm/notes@1/node_modules/notes/bin/notes": "pnpm",
		"/home/me/.config/yarn/global/node_modules/notes/bin/notes":                                   "yarn",
		"/home/me/project/.yarn/unplugged/node_modules/notes/bin/notes":                               "yarn",
		"/usr/local/Cellar/notes/1.3.0/bin/notes":                                                     "brew",
		"/opt/homebrew/bin/notes":                                                                     "brew",
		"/home/linuxbrew/.linuxbrew/bin/notes":                                                        "brew",
		"/usr/local/bin/notes":                                                                        "binary",
		"":                                                                                            "binary",
	}
	for exe, want := range cases {
		if got := DetectInstallMethod(exe); got != want {
			t.Errorf("%q: got %s, want %s", exe, got, want)
		}
	}
}

func TestChecksumsAndArchiveNames(t *testing.T) {
	sum := strings.Repeat("a", 64)
	if d, ok := checksumFor([]byte(sum+"  notes.zip\n"+strings.Repeat("b", 64)+"  other.zip\n"), "notes.zip"); !ok || d != sum {
		t.Fatal("one matching line")
	}
	for _, bad := range []string{"", sum + " notes.zip\n", strings.ToUpper(sum) + "  notes.zip\n",
		sum + "  notes.zip\n" + sum + "  notes.zip\n", sum[:63] + "  notes.zip\n", sum + "  *notes.zip\n"} {
		if _, ok := checksumFor([]byte(bad), "notes.zip"); ok {
			t.Errorf("accepted %q", bad)
		}
	}
	for name, want := range map[string]bool{"notes": true, "./skills/notes/SKILL.md": true, "skills/": true,
		"/etc/passwd": false, "../x": false, "skills/../../x": false, `C:\x`: false, "skills/a:b": false, "a\x00b": false} {
		if _, ok := cleanArchiveName(name); ok != want {
			t.Errorf("%q: got %v", name, ok)
		}
	}
	if v := versionFromOutput([]byte("debug: state directory: x\n{\"ok\":true,\"data\":{\"version\":\"v1.3.1\"}}\n")); v != "1.3.1" {
		t.Fatalf("version from output: %q", v)
	}
	if updateSeverity("1.3.0", Version{Major: 1, Minor: 4}, []ghRelease{{TagName: "v1.3.5", Body: "#### SECURITY\n"}}) != "warning" ||
		updateSeverity("1.3.0", Version{Major: 1, Minor: 4}, []ghRelease{{TagName: "v1.3.0", Body: "## Security"}}) != "info" {
		t.Fatal("severity")
	}
}

func TestVerifySigstoreBundleRefusesBadInput(t *testing.T) {
	ctx := context.Background()
	if err := VerifySigstoreBundle(ctx, []byte("x"), []byte(`{}`), "", githubActionsIssuer); err == nil {
		t.Fatal("an empty identity must be refused")
	}
	err := VerifySigstoreBundle(ctx, []byte("x"), []byte(`not json`), testIdentity, githubActionsIssuer)
	if err == nil || errors.Is(err, ErrTrustRootUnavailable) {
		t.Fatalf("a malformed bundle is a failed verification: %v", err)
	}
}

// U1: the post-install check reads a plain-text version (a 1.x release prints
// "gitlab-cli version 1.2.17"); the JSON envelope keeps priority. A binary that
// runs but reports no version is told apart from one that does not run.
func TestU1PlainTextVersionIsRead(t *testing.T) {
	for out, want := range map[string]string{
		"gitlab-cli version 1.2.17\n": "1.2.17", "v1.2.17\r\n": "1.2.17", "1.2.17": "1.2.17",
		"starting\nnotes version 1.3.1-rc.1+b2\n":                               "1.3.1-rc.1+b2",
		"notes version 9.9.9\n{\"ok\":true,\"data\":{\"version\":\"1.3.1\"}}\n": "1.3.1",
		"notes version 1.3\n": "", "version 01.2.3\n": "", "1.2.17 (abc)\n": "", "": "",
	} {
		if got := versionFromOutput([]byte(out)); got != want {
			t.Errorf("versionFromOutput(%q) = %q, want %q", out, got, want)
		}
	}
	// A fake binary whose content starts with "plain:" prints the rest and exits 0.
	plain := func(f *updateFixture) {
		f.h.build = func(build func(*upstream) *App) func(*upstream) *App {
			return func(u *upstream) *App {
				a := build(u)
				a.Update.Run = func(ctx context.Context, argv []string) ([]byte, error) {
					if b, err := os.ReadFile(argv[0]); err == nil && len(argv) == 2 && strings.HasPrefix(string(b), "plain:") {
						return b[len("plain:"):], nil
					}
					return f.run(ctx, argv)
				}
				return a
			}
		}(f.h.build)
	}
	f := newUpdateFixture(t)
	f.method = "binary"
	plain(f)
	f.addRelease("1.3.1", "", archiveFor(t, "plain:notes version 1.3.1\n", "1.3.1", nil))
	if r := f.h.run("update"); r.exit != 0 || updateData(t, r)["status"] != "updated" || f.readFile(f.exe) != "plain:notes version 1.3.1\n" {
		t.Fatalf("a plain-text version: %s", r)
	}
	for _, c := range []struct{ content, code, message string }{
		{"plain:notes version v1.3.2\n", "E_INTEGRITY", "The downloaded binary reports version 1.3.2, not 1.3.1, so it was not installed."},
		{"plain:notes (no version)\n", "E_IO", "The downloaded binary did not report its version, so it was not installed."},
	} {
		f := newUpdateFixture(t)
		f.method = "binary"
		plain(f)
		f.addRelease("1.3.1", "", archiveFor(t, c.content, "1.3.1", nil))
		r := f.h.run("update")
		if r.code() != c.code || r.details()["stage"] != "replace" || r.message() != c.message || f.readFile(f.exe) != "old notes 1.3.0" {
			t.Fatalf("%q: %s", c.content, r)
		}
		if c.code == "E_IO" && r.details()["next_action"] != "Install notes with the package-manager command in its README instead." {
			t.Fatalf("next_action: %v", r.details()["next_action"])
		}
	}
}
