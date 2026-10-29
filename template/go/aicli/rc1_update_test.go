// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"context"
	"errors"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// Regression tests for the update fixes before v2.0.0-rc.1 (decision 46, section E).

// E2: a release whose tag is a semver prerelease is never installed implicitly,
// whatever GitHub's prerelease flag says; --target-version reaches it.
func TestE2PrereleaseTagsAreNeverInstalledImplicitly(t *testing.T) {
	f := newUpdateFixture(t)
	f.method = "binary"
	f.addRelease("2.0.0-rc.1", "", archiveFor(t, "new notes 2.0.0-rc.1", "2.0.0-rc.1", nil)) // prerelease flag false
	f.addRelease("1.3.1", "", archiveFor(t, "new notes 1.3.1", "1.3.1", nil))
	f.h.env["NOTES_DEBUG"] = "1"
	r := f.h.run("update", "--check")
	if d := updateData(t, r); d["target_version"] != "1.3.1" {
		t.Fatalf("latest: %s", r)
	}
	if !strings.Contains(r.stderr, "debug: update: skipped release tag v2.0.0-rc.1 (a prerelease; --target-version installs it)\n") {
		t.Fatalf("debug lines:\n%s", r.stderr)
	}
	if r := f.h.run("update"); updateData(t, r)["target_version"] != "1.3.1" || f.readFile(f.exe) != "new notes 1.3.1" {
		t.Fatalf("update: %s", r)
	}
	if d := updateData(t, f.h.run("update", "--check", "--target-version", "2.0.0-rc.1")); d["target_version"] != "2.0.0-rc.1" {
		t.Fatalf("--target-version reaches a prerelease: %v", d)
	}
	// doctor's refresh asks for /releases/latest, which answers the rc here: ignored.
	g := newUpdateFixture(t)
	g.addRelease("2.0.0-rc.1", "", archiveFor(t, "new notes 2.0.0-rc.1", "2.0.0-rc.1", nil))
	if r := g.h.run("doctor"); r.exit != 0 || noticeOfType(r, "update_available") != nil {
		t.Fatalf("the notice must not offer a prerelease: %s", r)
	}
	if _, err := os.Stat(filepath.Join(g.h.state, "update-check.json")); err == nil {
		t.Fatal("a prerelease was recorded as the latest release")
	}
}

// E3: <exe dir>/skills may be shared by several tools: an update replaces and syncs
// only the Skills of its own release, one name at a time.
func TestE3SharedSkillsDirectoryIsLeftToItsOwners(t *testing.T) {
	f := newUpdateFixture(t)
	f.method = "binary"
	f.addRelease("1.3.1", "", archiveFor(t, "new notes 1.3.1", "1.3.1", nil))
	binSkills := filepath.Join(filepath.Dir(f.exe), "skills")
	other := "---\nname: other-cli\nmetadata:\n  version: \"0.9.0\"\n---\nOLD COPY\n"
	f.writeFile(filepath.Join(binSkills, "other-cli", "SKILL.md"), other)
	f.installSkill(f.roots[0], "1.3.0")
	users := "---\nname: other-cli\nmetadata:\n  version: \"2.0.0\"\n---\nTHE USER'S CURRENT COPY\n"
	f.writeFile(filepath.Join(f.roots[0], "other-cli", "SKILL.md"), users)
	if d := updateData(t, f.h.run("update")); d["status"] != "updated" || d["skill_sync_status"] != "synced" {
		t.Fatalf("update: %v", d)
	}
	if f.readFile(filepath.Join(binSkills, "other-cli", "SKILL.md")) != other {
		t.Fatal("another tool's Skill beside the binary was removed or replaced")
	}
	if v, _ := skillVersions(f.readFile(filepath.Join(binSkills, "notes", "SKILL.md"))); v != "1.3.1" {
		t.Fatalf("the release's own Skill: %s", v)
	}
	if f.readFile(filepath.Join(f.roots[0], "other-cli", "SKILL.md")) != users {
		t.Fatal("the sync overwrote another tool's installed Skill")
	}
	// A run at the current version syncs this tool's Skills only: the entry Skill and
	// the domain Skills whose requires.skills names it.
	f.h.build = func(build func(*upstream) *App) func(*upstream) *App {
		return func(u *upstream) *App {
			a := build(u)
			a.Version = "1.3.1"
			return a
		}
	}(f.h.build)
	domain := "---\nname: notes-extra\nmetadata:\n  version: \"1.3.1\"\n  requires.skills: \"notes\"\n---\n"
	f.writeFile(filepath.Join(binSkills, "notes-extra", "SKILL.md"), domain)
	f.writeFile(filepath.Join(f.roots[0], "notes-extra", "SKILL.md"), strings.Replace(domain, "1.3.1", "1.2.0", 1))
	f.installSkill(f.roots[0], "1.2.0")
	if d := updateData(t, f.h.run("update")); d["status"] != "up_to_date" || d["skill_sync_status"] != "synced" {
		t.Fatalf("up to date: %v", d)
	}
	if v, _ := skillVersions(f.readFile(filepath.Join(f.roots[0], "notes-extra", "SKILL.md"))); v != "1.3.1" {
		t.Fatalf("the domain Skill was not synced: %s", v)
	}
	if f.readFile(filepath.Join(f.roots[0], "other-cli", "SKILL.md")) != users {
		t.Fatal("an up-to-date run overwrote another tool's installed Skill")
	}
	// A release without skills/ leaves <exe dir>/skills alone.
	g := newUpdateFixture(t)
	g.method = "binary"
	g.addRelease("1.3.1", "", archiveFor(t, "new notes 1.3.1", "", nil))
	g.writeFile(filepath.Join(filepath.Dir(g.exe), "skills", "notes", "SKILL.md"), skillMD("1.3.0"))
	if d := updateData(t, g.h.run("update")); d["status"] != "updated" {
		t.Fatalf("update without skills/: %v", d)
	}
	if _, err := os.Stat(filepath.Join(filepath.Dir(g.exe), "skills", "notes", "SKILL.md")); err != nil {
		t.Fatal("a release without skills/ must leave the directory alone")
	}
}

// E6: pnpm 11's global store is the pnpm install method, and the version check
// after the manager ran finds the tool on PATH by its name, where pnpm put it.
func TestE6Pnpm11StoreAndThePathLookup(t *testing.T) {
	for _, exe := range []string{
		`C:\Users\me\AppData\Local\pnpm\store\v11\links\@\notes-win32-x64\1.3.0\82d0a6\node_modules\notes-win32-x64\bin\notes.exe`,
		"/home/me/.local/share/pnpm/store/v11/links/@/notes/1.3.0/82d0a6/node_modules/notes/bin/notes",
	} {
		if got := DetectInstallMethod(exe); got != "pnpm" {
			t.Errorf("%s: %s", exe, got)
		}
	}
	f := newUpdateFixture(t)
	f.method = "pnpm"
	f.addRelease("1.3.1", "", archiveFor(t, "new notes 1.3.1", "1.3.1", nil))
	installed := filepath.Join(f.dir, "store", "v11", "links", "notes", "1.3.1", "bin", binName())
	f.writeFile(installed, "new notes 1.3.1") // pnpm installs at a new path; f.exe still says 1.3.0
	found := lookPath
	lookPath = func(name string) (string, error) {
		if name == "notes" {
			return installed, nil
		}
		return "", exec.ErrNotFound
	}
	t.Cleanup(func() { lookPath = found })
	r := f.h.run("update")
	if d := r.data(); r.exit != 0 || d["status"] != "updated" || d["install_method"] != "pnpm" || d["current_version"] != "1.3.1" {
		t.Fatalf("pnpm update: %s", r)
	}
}

// E9: an interrupt does not kill a running package manager: it runs under a context
// that the run's cancellation does not reach, within its 10-minute cap.
func TestE9PackageManagerRunIsNotCancelled(t *testing.T) {
	f := newUpdateFixture(t)
	f.method = "npm"
	f.addRelease("1.3.1", "", archiveFor(t, "new notes 1.3.1", "1.3.1", nil))
	a := f.h.app()
	ctx, cancel := context.WithCancel(context.Background())
	cancel() // the user pressed Ctrl-C
	u := a.newUpdate(ctx)
	u.method, u.previous = "npm", "1.3.0"
	seen := errors.New("the manager did not run")
	u.cfg.Run = func(ctx context.Context, argv []string) ([]byte, error) {
		if argv[0] == "npm" {
			_, hasDeadline := ctx.Deadline()
			if seen = ctx.Err(); seen == nil && !hasDeadline {
				seen = errors.New("no 10-minute cap")
			}
			f.writeFile(f.exe, "new notes 1.3.1")
			return nil, nil
		}
		return f.run(ctx, argv)
	}
	if _, e := u.installWithManager(f.exe, Version{Major: 1, Minor: 3, Patch: 1}); e != nil || seen != nil {
		t.Fatalf("the manager must finish: %v %v", e, seen)
	}
}

// E10: when the old binary cannot be moved back after a failed swap, the error says
// the tool may be missing, names the .old file, and the new binary is kept.
func TestE10FailedRollbackIsReportedTruthfully(t *testing.T) {
	f := newUpdateFixture(t)
	f.method = "binary"
	f.addRelease("1.3.1", "", archiveFor(t, "new notes 1.3.1", "1.3.1", nil))
	os0, ops0 := swapOS, swapOps
	t.Cleanup(func() { swapOS, swapOps = os0, ops0 })
	swapOS = "windows"
	calls := 0
	swapOps = fileOps{remove: os.Remove, rename: func(from, to string) error {
		calls++
		if calls >= 2 { // the new binary cannot go in, nor the old one back
			return &os.LinkError{Op: "rename", Old: from, New: to, Err: errors.New("Access is denied.")}
		}
		return os.Rename(from, to)
	}}
	r := f.h.run("update")
	d := r.details()
	if r.code() != "E_IO" || d["stage"] != "replace" || d["dispatch_state"] != "unknown" ||
		r.message() != "The update failed and the old version could not be put back; notes may be missing." ||
		d["next_action"] != "Rename "+f.exe+".old back to "+f.exe+", then run notes --version." {
		t.Fatalf("failed rollback: %s", r)
	}
	if f.readFile(f.exe+".old") != "old notes 1.3.0" {
		t.Fatal("the old binary is at <exe>.old")
	}
	kept := false
	entries, _ := os.ReadDir(filepath.Dir(f.exe))
	for _, e := range entries {
		if strings.Contains(e.Name(), "-update-") {
			if b, err := os.ReadFile(filepath.Join(filepath.Dir(f.exe), e.Name(), "extracted", binName())); err == nil && string(b) == "new notes 1.3.1" {
				kept = true
			}
		}
	}
	if !kept {
		t.Fatal("the new binary must not be deleted when the old one could not be put back")
	}
}

// E13: release assets and redirects must be https.
func TestE13DownloadsAndRedirectsMustBeHTTPS(t *testing.T) {
	f := newUpdateFixture(t)
	f.method = "binary"
	f.addRelease("1.3.1", "", archiveFor(t, "new notes 1.3.1", "1.3.1", nil))
	asset := f.releases[0]["assets"].([]any)[0].(map[string]any)
	asset["browser_download_url"] = "http://" + strings.TrimPrefix(asset["browser_download_url"].(string), "https://")
	r := f.h.run("update")
	if r.code() != "E_UNSUPPORTED" || r.details()["stage"] != "download" || f.downloads() != 0 ||
		r.message() != "Release v1.3.1 names a download that is not https, so it was not installed." {
		t.Fatalf("an http asset: %s", r)
	}
	g := newUpdateFixture(t)
	g.method = "binary"
	g.override = func(w http.ResponseWriter, r *http.Request) bool {
		http.Redirect(w, r, "http://releases.example.invalid/list", http.StatusFound)
		return true
	}
	r = g.h.run("update", "--check")
	if r.code() != "E_NETWORK" || r.details()["stage"] != "discover" ||
		r.message() != "The release source redirected to a URL that is not https, so nothing was downloaded." {
		t.Fatalf("an http redirect: %s", r)
	}
}

// E13: extraction is capped in entries and in bytes.
func TestE13ExtractionIsCapped(t *testing.T) {
	entries0, total0 := maxArchiveEntries, maxExtractedTotal
	t.Cleanup(func() { maxArchiveEntries, maxExtractedTotal = entries0, total0 })
	archive := filepath.Join(t.TempDir(), "release.zip")
	if !strings.HasSuffix(assetName("1.3.1"), ".zip") {
		archive = strings.TrimSuffix(archive, ".zip") + ".tar.gz"
	}
	if err := os.WriteFile(archive, archiveFor(t, "new notes 1.3.1", "1.3.1", nil), 0o600); err != nil {
		t.Fatal(err)
	}
	var unsafe *unsafeEntryError
	maxArchiveEntries = 2 // the archive holds the binary, README.md and two Skill files
	if _, _, err := extractRelease(archive, filepath.Join(t.TempDir(), "x"), binName()); !errors.As(err, &unsafe) {
		t.Fatalf("too many entries: %v", err)
	}
	maxArchiveEntries, maxExtractedTotal = entries0, 10
	if _, _, err := extractRelease(archive, filepath.Join(t.TempDir(), "y"), binName()); !errors.As(err, &unsafe) {
		t.Fatalf("too many bytes: %v", err)
	}
	maxExtractedTotal = total0
	if _, _, err := extractRelease(archive, filepath.Join(t.TempDir(), "z"), binName()); err != nil {
		t.Fatalf("within the caps: %v", err)
	}
}

// E13: a package manager found only in the current directory (exec.ErrDot) never
// ran, so nothing was dispatched.
func TestE13ManagerInTheCurrentDirectoryNeverRan(t *testing.T) {
	f := newUpdateFixture(t)
	f.method = "npm"
	f.addRelease("1.3.1", "", archiveFor(t, "new notes 1.3.1", "1.3.1", nil))
	f.h.build = func(build func(*upstream) *App) func(*upstream) *App {
		return func(u *upstream) *App {
			a := build(u)
			a.Update.Run = func(ctx context.Context, argv []string) ([]byte, error) {
				if argv[0] == "npm" {
					return nil, &exec.Error{Name: "npm", Err: exec.ErrDot}
				}
				return f.run(ctx, argv)
			}
			return a
		}
	}(f.h.build)
	r := f.h.run("update")
	if r.code() != "E_CONFIG" || r.details()["dispatch_state"] != "not_started" || r.message() != "npm was not found on PATH." {
		t.Fatalf("ErrDot: %s", r)
	}
}

// E13: the notice cache records the real latest release from discovery, never a
// --target-version.
func TestE13TargetVersionIsNotRecordedAsTheLatest(t *testing.T) {
	f := newUpdateFixture(t)
	f.method = "binary"
	f.addRelease("1.3.1", "", archiveFor(t, "new notes 1.3.1", "1.3.1", nil))
	f.addRelease("1.4.0", "", archiveFor(t, "new notes 1.4.0", "1.4.0", nil))
	cache := filepath.Join(f.h.state, "update-check.json")
	if d := updateData(t, f.h.run("update", "--check", "--target-version", "1.3.1")); d["target_version"] != "1.3.1" {
		t.Fatalf("check: %v", d)
	}
	if _, err := os.Stat(cache); err == nil {
		t.Fatal("a --target-version was recorded as the latest release")
	}
	updateData(t, f.h.run("update", "--check"))
	if !strings.Contains(f.readFile(cache), `"latest_version":"1.4.0"`) {
		t.Fatalf("cache: %s", f.readFile(cache))
	}
}
