// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/spf13/cobra"
)

func TestReferenceLayers(t *testing.T) {
	h := newHarness(t)
	full := h.run("reference")
	if full.exit != 0 {
		t.Fatalf("reference: %s", full)
	}
	if err := validateReferenceFull(full.data()); err != nil {
		t.Fatal(err)
	}
	if full.data()["spec_version"] != SpecVersion || full.data()["risk_tier"] != "T2" {
		t.Fatalf("reference header: %v", full.data())
	}
	idx := h.run("reference", "--index")
	if idx.exit != 0 || len([]rune(strings.TrimSpace(idx.stdout))) > indexBudgetChars {
		t.Fatalf("index: %s", idx)
	}
	if err := validateReferenceIndex(idx.data()); err != nil {
		t.Fatal(err)
	}
	cmds := idx.data()["commands"].([]any)
	var del map[string]any
	for _, c := range cmds {
		if c.(map[string]any)["path"] == "note delete" {
			del = c.(map[string]any)
		}
	}
	if del == nil || del["type"] != "write" || del["dangerous"] != true {
		t.Fatalf("index entry for note delete: %v", del)
	}
	one := h.run("reference", "--command", "note update")
	if one.exit != 0 || len([]rune(strings.TrimSpace(one.stdout))) > commandBudgetChars {
		t.Fatalf("command: %s", one)
	}
	if err := validateReferenceCommand(one.data()); err != nil {
		t.Fatal(err)
	}
	entry := one.data()["command"].(map[string]any)
	params := entry["params"].([]any)
	first := params[0].(map[string]any)
	if first["name"] != "id" || first["kind"] != "positional" {
		t.Fatalf("the positional comes first: %v", params)
	}
	var bodyFile map[string]any
	for _, p := range params {
		if p.(map[string]any)["name"] == "body-file" {
			bodyFile = p.(map[string]any)
		}
	}
	if bodyFile["type"] != "path" || bodyFile["file_input"] != true {
		t.Fatalf("file input param: %v", bodyFile)
	}
	codes := one.data()["error_codes"].(map[string]any)
	for _, c := range []string{"E_CONFIRMATION_REQUIRED", "E_CONFLICT", "E_FORBIDDEN", "E_OUTCOME_UNKNOWN", "E_NOT_FOUND"} {
		if codes[c] == nil {
			t.Fatalf("reference --command misses %s: %v", c, codes)
		}
	}
	if s := one.data()["schemas"].(map[string]any)["note_write"]; s == nil {
		t.Fatalf("output schema missing: %v", one.data()["schemas"])
	}
	// lenient spellings of --command
	for _, args := range [][]string{{"reference", "--command", "note", "update"}, {"reference", "note", "update"}} {
		if r := h.run(args...); r.exit != 0 || r.data()["command"].(map[string]any)["path"] != "note update" {
			t.Fatalf("%v: %s", args, r)
		}
	}
	// Suggestions are --command values: registry paths without the tool name.
	r := h.run("reference", "--command", "note updte")
	if r.exit != 2 || r.details()["problem"] != "unknown_command" || !listHasString(r.details()["did_you_mean"], "note update") ||
		!listHasString(r.details()["available"], "note list") {
		t.Fatalf("unknown --command: %s", r)
	}
	r = h.run("reference", "--index", "--command", "note get")
	if r.details()["problem"] != "conflicting_flags" {
		t.Fatalf("got %s", r)
	}
}

func TestReferenceIndexGroupsWhenLarge(t *testing.T) {
	h := newHarness(t)
	h.build = func(u *upstream) *App {
		a := testApp(u)
		for g := 0; g < 6; g++ {
			grp := &cobra.Command{Use: fmt.Sprintf("area%d", g)}
			a.Group(grp, fmt.Sprintf("Area %d", g))
			a.Root.AddCommand(grp)
			for i := 0; i < 20; i++ {
				c := &cobra.Command{Use: fmt.Sprintf("action%02d", i), RunE: func(cmd *cobra.Command, _ []string) error {
					return a.OK(cmd, map[string]any{})
				}}
				grp.AddCommand(c)
				a.Register(c, Meta{Summary: strings.Repeat("Does something useful with the area ", 2), Effects: []Effect{Observe},
					Output: "note", Examples: []string{"notes area action"}})
			}
		}
		return a
	}
	r := h.run("reference", "--index")
	groups, ok := r.data()["groups"].([]any)
	if r.exit != 0 || !ok || len(groups) < 7 {
		t.Fatalf("a large index must switch to groups: %s", r)
	}
	if len([]rune(strings.TrimSpace(r.stdout))) > indexBudgetChars {
		t.Fatal("the grouped index is over budget")
	}
	g := h.run("reference", "--index", "area3")
	if g.exit != 0 || len(g.data()["commands"].([]any)) != 20 {
		t.Fatalf("group index: %s", g)
	}
	if bad := h.run("reference", "--index", "nowhere"); bad.exit != 2 {
		t.Fatalf("unknown group: %s", bad)
	}
}

func TestContextAndDoctor(t *testing.T) {
	h := newHarness(t)
	r := h.run("context")
	if r.exit != 0 {
		t.Fatalf("context: %s", r)
	}
	if err := validateContext(r.data()); err != nil {
		t.Fatal(err)
	}
	d := r.data()
	if d["account"] != "tester" || d["host"] != "https://notes.example" || d["credentials"].(map[string]any)["source"] != "env" {
		t.Fatalf("context: %v", d)
	}
	if _, err := os.Stat(h.state); err == nil {
		t.Fatal("context must not create the state directory")
	}
	doc := h.run("doctor")
	if doc.exit != 0 {
		t.Fatalf("doctor: %s", doc)
	}
	if err := validateDoctor(doc.data()); err != nil {
		t.Fatal(err)
	}
	statuses := map[string]string{}
	for _, c := range doc.data()["checks"].([]any) {
		m := c.(map[string]any)
		statuses[m["check"].(string)] = m["status"].(string)
		if m["status"] == "pass" && m["fix"] != nil {
			t.Fatalf("a passing check has no fix: %v", m)
		}
	}
	// no Skill is installed in the test's skill directories (decision 22)
	if statuses["authorization"] != "pass" || statuses["version"] != "warn" || statuses["credentials"] != "pass" {
		t.Fatalf("doctor statuses: %v", statuses)
	}
	// tool checks replace library checks of the same name
	h.build = func(u *upstream) *App {
		a := testApp(u)
		a.MinSkillVersion = "9.0.0"
		a.DoctorChecks = func(context.Context) []Check {
			return []Check{{Check: "connectivity", Status: "pass", Message: "Reached the upstream."}, {Check: "quota", Status: "warn", Message: "Low."}}
		}
		return a
	}
	doc = h.run("doctor")
	statuses = map[string]string{}
	for _, c := range doc.data()["checks"].([]any) {
		m := c.(map[string]any)
		statuses[m["check"].(string)] = m["status"].(string)
	}
	if statuses["connectivity"] != "pass" || statuses["quota"] != "warn" || statuses["version"] != "fail" {
		t.Fatalf("doctor with tool checks: %v", statuses)
	}
}

// TestDoctorChecksTheInstalledSkill: the version check compares the tool with the
// Skill installed in App.SkillDirs (decision 22), not with a build-time constant.
func TestDoctorChecksTheInstalledSkill(t *testing.T) {
	versionCheck := func(t *testing.T, skill string, dirs ...string) map[string]any {
		t.Helper()
		h := newHarness(t)
		skills := filepath.Join(h.dir, "skills")
		if skill != "" {
			mustWrite(t, filepath.Join(skills, "notes", "SKILL.md"), []byte(skill))
		}
		h.build = func(u *upstream) *App {
			a := testApp(u)
			a.MinSkillVersion = ""
			a.SkillDirs = append([]string{skills}, dirs...)
			return a
		}
		r := h.run("doctor")
		if err := validateDoctor(r.data()); err != nil {
			t.Fatal(err)
		}
		for _, c := range r.data()["checks"].([]any) {
			if m := c.(map[string]any); m["check"] == "version" {
				return m
			}
		}
		t.Fatalf("no version check: %s", r)
		return nil
	}
	skill := func(version, minVersion string) string {
		return "---\nname: notes\ndescription: Notes.\nmetadata:\n  version: \"" + version + "\"\n  min_version: \"" + minVersion + "\"\n---\n# Notes\n"
	}
	cases := []struct {
		name, skill, status string
	}{
		{"not installed", "", "warn"},
		{"in step", skill("1.2.3", "1.2.3"), "pass"},
		{"skill newer, tool still enough", skill("1.3.0", "1.2.0"), "pass"},
		{"tool too old for the skill", skill("2.0.0", "2.0.0"), "fail"},
		{"skill older than the tool", skill("1.1.0", "1.1.0"), "warn"},
		{"not semver", skill("one", "1.0.0"), "warn"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := versionCheck(t, c.skill)
			if got["status"] != c.status {
				t.Fatalf("want %s, got %v", c.status, got)
			}
			if c.status == "fail" && got["fix"] != "Run `notes update`." {
				t.Fatalf("a too-old tool is fixed by updating it: %v", got)
			}
			// update syncs an installed Skill; it does not install a missing one (F10).
			switch {
			case c.name == "not installed" && got["fix"] != "Install the Skill with the install command in the tool's README; update does not install a missing Skill.":
				t.Fatalf("a missing Skill is installed from the README: %v", got)
			case c.name != "not installed" && c.status == "warn" && !strings.Contains(fmt.Sprint(got["fix"]), "notes update"):
				t.Fatalf("an old Skill is synced by update: %v", got)
			}
		})
	}
	// several installed copies: the worst one decides
	other := t.TempDir()
	mustWrite(t, filepath.Join(other, "notes", "SKILL.md"), []byte(skill("9.0.0", "9.0.0")))
	if got := versionCheck(t, skill("1.2.3", "1.2.3"), other); got["status"] != "fail" {
		t.Fatalf("worst copy: %v", got)
	}
	if v, mv := skillVersions(skill("1.2.3", "1.0.0")); v != "1.2.3" || mv != "1.0.0" {
		t.Fatalf("frontmatter: %q %q", v, mv)
	}
}

// TestContextStateIsHonest: an unknown state directory is null, protection is
// null when the store is unavailable, and on Windows a directory outside the user
// profile is windows-unverified (decisions 15 and 30).
func TestContextStateIsHonest(t *testing.T) {
	h := newHarness(t)
	r := h.run("context")
	state := r.data()["state"].(map[string]any)
	if state["available"] != true || state["dir"] != h.state || state["protection"] != protection(h.state) {
		t.Fatalf("state: %v", state)
	}
	if runtime.GOOS == "windows" {
		home, err := os.UserHomeDir()
		if err != nil {
			t.Fatal(err)
		}
		if got := protection(filepath.Join(home, ".notes-aicli-test")); got != "windows-profile-acl" {
			t.Fatalf("inside the profile: %s", got)
		}
		for _, outside := range []string{`Z:\aicli-state`, `\\server\share\state`} {
			if got := protection(outside); got != "windows-unverified" {
				t.Fatalf("%s: %s", outside, got)
			}
		}
	} else if got := protection(h.state); got != "posix-0700" {
		t.Fatalf("posix: %s", got)
	}
	if err := os.MkdirAll(h.state, 0o700); err != nil {
		t.Fatal(err)
	}
	mustWrite(t, filepath.Join(h.state, "confirm.key"), []byte("broken"))
	r = h.run("context")
	state = r.data()["state"].(map[string]any)
	if state["available"] != false || state["protection"] != nil || state["reason"] == nil {
		t.Fatalf("an unusable store claims protection: %v", state)
	}
	// no state directory can be resolved: dir is null, never ""
	t.Setenv("HOME", "")
	t.Setenv("USERPROFILE", "")
	t.Setenv("home", "")
	if _, err := os.UserHomeDir(); err == nil {
		t.Log("the home directory is still known here; skipping the unresolved-dir case")
		return
	}
	a := testApp(newUpstream())
	a.Env = func(string) string { return "" }
	var out strings.Builder
	a.Stdout, a.Stderr = &out, &out
	if code := a.Execute([]string{"context"}); code != 0 {
		t.Fatalf("context without a home: %d %s", code, out.String())
	}
	if err := ValidateEnvelope([]byte(out.String())); err != nil {
		t.Fatalf("%v\n%s", err, out.String())
	}
	if !strings.Contains(out.String(), `"dir":null`) || !strings.Contains(out.String(), `"protection":null`) {
		t.Fatalf("an unknown state dir must be null: %s", out.String())
	}
}

func TestContextRedactsHostCredentials(t *testing.T) {
	if got := redactHost("https://user:pw@host.example/path?token=abc#x"); got != "https://host.example/path" {
		t.Fatalf("got %q", got)
	}
}

func TestChangelog(t *testing.T) {
	h := newHarness(t)
	r := h.run("changelog")
	entries := r.data()["entries"].([]any)
	if r.exit != 0 || len(entries) != 3 || r.data()["current_version"] != "1.2.3" {
		t.Fatalf("changelog: %s", r)
	}
	e0 := entries[0].(map[string]any)
	if e0["version"] != "1.2.3" || e0["date"] != "2026-10-01" {
		t.Fatalf("entry: %v", e0)
	}
	changes := entries[1].(map[string]any)["changes"].(map[string]any)
	if got := changes["changed"].([]any)[0]; got != "Compact JSON by default. It is also documented." {
		t.Fatalf("continuation lines: %q", got)
	}
	r = h.run("changelog", "--since", "1.0.0")
	if len(r.data()["entries"].([]any)) != 2 || r.data()["since"] != "1.0.0" {
		t.Fatalf("--since: %s", r)
	}
	r = h.run("changelog", "--since", "1.0")
	if r.exit != 2 || r.details()["parameter"] != "--since" {
		t.Fatalf("bad --since: %s", r)
	}
}

// TestChangelogSkipsCommentsAndLinkReferences: HTML comments are skipped and a
// continuation ends at a blank line or a link-reference definition (decision 26).
func TestChangelogSkipsCommentsAndLinkReferences(t *testing.T) {
	text := "# Changelog\n\n## [1.1.0] - 2026-10-02\n### Added\n- First item\n" +
		"<!-- reviewers: keep this comment out of the output -->\n\nSome paragraph after a blank line.\n" +
		"- Second item\n  continued here\n<!--\nA comment over\nseveral lines\n-->\n### Fixed\n- Old fix\n\n" +
		"[1.1.0]: https://example.com/compare/v1.0.0...v1.1.0\n[1.0.0]: https://example.com/releases/v1.0.0\n"
	entries := parseChangelog(text)
	if len(entries) != 1 {
		t.Fatalf("entries: %+v", entries)
	}
	added, fixed := entries[0].changes["added"], entries[0].changes["fixed"]
	if len(added) != 2 || added[0] != "First item" || added[1] != "Second item continued here" {
		t.Fatalf("added: %q", added)
	}
	if len(fixed) != 1 || fixed[0] != "Old fix" {
		t.Fatalf("fixed: %q", fixed)
	}
	// an inline comment inside a bullet is removed too
	if e := parseChangelog("## 1.0.0\n### Added\n- Item <!-- note --> end\n"); e[0].changes["added"][0] != "Item  end" {
		t.Fatalf("inline comment: %q", e[0].changes["added"])
	}
}

func TestOperationBuiltins(t *testing.T) {
	h := newHarness(t)
	r := h.run("operation", "list")
	if r.exit != 0 || len(r.data()["items"].([]any)) != 0 {
		t.Fatalf("empty journal: %s", r)
	}
	if _, err := os.Stat(h.state); err == nil {
		t.Fatal("operation list must not create the state directory")
	}
	for i := 0; i < 3; i++ {
		tok := h.mustToken("note", "update", "1", "--title", fmt.Sprintf("T%d", i))
		h.run("note", "update", "1", "--title", fmt.Sprintf("T%d", i), "--confirm", tok)
	}
	r = h.run("operation", "list", "--state", "applied", "--limit", "2")
	if r.exit != 0 || len(r.data()["items"].([]any)) != 2 || r.data()["has_more"] != true || r.data()["next_offset"] == nil ||
		r.data()["total"] != json.Number("3") {
		t.Fatalf("paged list: %s", r)
	}
	r = h.run("operation", "show", "--id", "op_zzzzzzzzzzzzzzzzzzzzzzzzzz")
	if r.exit != 3 || r.code() != "E_NOT_FOUND" {
		t.Fatalf("unknown id: %s", r)
	}
	r = h.run("operation", "show", "--id", "../../etc/passwd")
	if r.exit != 2 || r.details()["parameter"] != "--id" {
		t.Fatalf("bad id: %s", r)
	}
	r = h.run("operation", "resolve", "--id", "op_zzzzzzzzzzzzzzzzzzzzzzzzzz", "--outcome", "maybe", "--evidence", "x")
	if r.exit != 2 || r.details()["parameter"] != "--outcome" {
		t.Fatalf("bad outcome: %s", r)
	}
}

// operation show takes what the agent holds after a lost reply: the confirm token it
// sent, or the id in any case.
func TestOperationShowAcceptsTheTokenAndUpperCase(t *testing.T) {
	h := newHarness(t)
	dry := h.run("note", "update", "1", "--title", "Lost", "--dry-run")
	id, tok := dry.data()["operation_id"].(string), dry.token()
	h.run("note", "update", "1", "--title", "Lost", "--confirm", tok)
	for _, given := range []string{id, tok, strings.ToUpper(id)} {
		r := h.run("operation", "show", "--id", given)
		if r.exit != 0 || r.data()["operation_id"] != id || r.data()["status"] != "applied" {
			t.Fatalf("operation show --id %s: %s", given, r)
		}
	}
	r := h.run("operation", "show")
	if r.exit != 2 || !strings.Contains(r.details()["next_action"].(string), "operation list") {
		t.Fatalf("a missing id should point to operation list: %s", r)
	}
}

// The default operation list shows what the agent looks for: dispatched top-level
// operations, a batch as one row with its item count.
func TestOperationListShowsTopLevelOperations(t *testing.T) {
	h := newHarness(t)
	tok := h.mustToken("note", "bulk-tag", "--ids", "1,2,3", "--tag", "x")
	h.run("note", "bulk-tag", "--ids", "1,2,3", "--tag", "x", "--confirm", tok)
	h.run("note", "update", "1", "--title", "Planned only", "--dry-run")
	r := h.run("operation", "list")
	items := r.data()["items"].([]any)
	if len(items) != 1 || r.data()["total"] != json.Number("1") {
		t.Fatalf("want only the batch row: %s", r)
	}
	row := items[0].(map[string]any)
	if !strings.HasPrefix(row["operation_id"].(string), "opb_") || row["item_count"] != json.Number("3") {
		t.Fatalf("batch row: %v", row)
	}
	if r := h.run("operation", "list", "--items"); len(r.data()["items"].([]any)) != 4 || r.data()["total"] != json.Number("4") {
		t.Fatalf("--items lists the batch and its items: %s", r)
	}
	if r := h.run("operation", "list", "--state", "planned"); len(r.data()["items"].([]any)) != 1 || r.data()["total"] != json.Number("1") {
		t.Fatalf("--state planned: %s", r)
	}
	if r := h.run("operation", "list", "--state", "all"); len(r.data()["items"].([]any)) != 5 || r.data()["total"] != json.Number("5") {
		t.Fatalf("--state all: %s", r)
	}
	for i := 0; i < 11; i++ {
		tk := h.mustToken("note", "update", "2", "--title", fmt.Sprintf("N%d", i))
		h.run("note", "update", "2", "--title", fmt.Sprintf("N%d", i), "--confirm", tk)
	}
	// total counts the whole filtered list, so "how many" needs no paging (CLI-044).
	if r := h.run("operation", "list"); len(r.data()["items"].([]any)) != 10 || r.data()["has_more"] != true || r.data()["total"] != json.Number("12") {
		t.Fatalf("the default limit is 10: %s", r)
	}
	if r := h.run("operation", "list", "--offset", "10"); len(r.data()["items"].([]any)) != 2 || r.data()["has_more"] != false || r.data()["total"] != json.Number("12") {
		t.Fatalf("the last page: %s", r)
	}
}

func TestUpdateNoticeFromCacheOnly(t *testing.T) {
	h := newHarness(t)
	now := time.Now().UTC()
	h.now = func() time.Time { return now }
	a := h.app()
	if err := a.RecordUpdateCheck("1.3.0", "warning"); err != nil {
		t.Fatal(err)
	}
	r := h.run("note", "get", "1")
	notices, _ := r.env["meta"].(map[string]any)["notices"].([]any)
	if len(notices) != 1 || notices[0].(map[string]any)["type"] != "update_available" || notices[0].(map[string]any)["latest_version"] != "1.3.0" {
		t.Fatalf("notice: %s", r)
	}
	r = h.run("note", "get", "1")
	if _, has := r.env["meta"].(map[string]any)["notices"]; has {
		t.Fatalf("the notice must appear at most once a day: %s", r)
	}
	now = now.Add(25 * time.Hour)
	r = h.run("note", "get", "1")
	if _, has := r.env["meta"].(map[string]any)["notices"]; !has {
		t.Fatalf("the notice should reappear after a day: %s", r)
	}
	// an installed version at or above the cached one never gets a notice
	b := h.app()
	if err := b.RecordUpdateCheck("1.2.3", "info"); err != nil {
		t.Fatal(err)
	}
	now = now.Add(48 * time.Hour)
	if r := h.run("note", "get", "1"); r.env["meta"].(map[string]any)["notices"] != nil {
		t.Fatalf("a current install got a notice: %s", r)
	}
	if err := b.RecordUpdateCheck("1.3.0;rm -rf", "info"); err == nil {
		t.Fatal("a non-semver latest version was cached")
	}
	_ = filepath.Join
}

// An update that crosses a major version is a warning, whatever the caller passed
// (CLI-071).
func TestMajorUpdateNoticeIsAWarning(t *testing.T) {
	h := newHarness(t)
	if err := h.app().RecordUpdateCheck("2.0.0", "info"); err != nil {
		t.Fatal(err)
	}
	r := h.run("note", "get", "1")
	notices, _ := r.env["meta"].(map[string]any)["notices"].([]any)
	if len(notices) != 1 || notices[0].(map[string]any)["severity"] != "warning" {
		t.Fatalf("notice: %s", r)
	}
	if got := noticeSeverity("info", "1.2.3", Version{Major: 1, Minor: 3}); got != "info" {
		t.Fatalf("a minor update keeps its severity: %s", got)
	}
}
