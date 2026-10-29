// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/spf13/cobra"
)

// Regression tests for the fixes before v2.0.0-rc.2 (FIXES-RC2.md, decision 47),
// found by the gitlab-cli pilot's command groups; one or more per item, named after
// it.

// ---------------------------------------------------------------- Q1 ResolveTarget

// removeByTitle adds `note remove --title T`, whose target is the note with that
// title: found with ResolveTarget, which counts its reads.
func removeByTitle(h *tharness, lookups *int) {
	h.build = func(u *upstream) *App {
		a := testApp(u)
		rm := &cobra.Command{Use: "remove", RunE: func(cmd *cobra.Command, _ []string) error {
			title, _ := cmd.Flags().GetString("title")
			var id string
			return a.Write(cmd, WriteSpec{
				ResolveTarget: func(context.Context) (Target, error) {
					*lookups++
					u.mu.Lock()
					defer u.mu.Unlock()
					for _, n := range u.notes {
						if n.Title == title {
							id = n.ID
							return Target{Key: "notes/" + n.ID, Kind: "note"}, nil
						}
					}
					return Target{}, NotFound("No note has that title.", "Run `notes note list` to see the titles.")
				},
				Preview: func(context.Context) (Preview, error) {
					return Preview{Action: "Remove the note titled " + title, CanApply: true, BaseVersion: "1",
						Changes: []FieldChange{{Field: "note", Before: title, Note: "removed"}}}, nil
				},
				Apply: func(context.Context, Op) (Result, error) {
					u.write(func() { delete(u.notes, id) })
					return Result{}, nil
				},
			})
		}}
		rm.Flags().String("title", "", "Title of the note")
		a.commandByPath("note").AddCommand(rm)
		a.Register(rm, Meta{Summary: "Remove a note by its title", Effects: []Effect{Change}, Output: "note_write",
			Examples: []string{"notes note remove --title Ideas --dry-run", "notes note remove --title Ideas --confirm <token>"}})
		return a
	}
}

// Q1: a target resolved with WriteSpec.ResolveTarget is looked up at the dry-run
// before the scope check, and at confirm only after the token checks, so a used
// token is token_used before any tool code runs.
func TestQ1ResolveTargetRunsInsideTheGate(t *testing.T) {
	lookups := 0
	h := newHarness(t)
	removeByTitle(h, &lookups)
	args := []string{"note", "remove", "--title", "Ideas"}
	dry := h.run(append(args, "--dry-run")...)
	if dry.exit != 0 || lookups != 1 || dry.data()["preview"].(map[string]any)["target"].(map[string]any)["key"] != "notes/2" {
		t.Fatalf("dry-run (lookups %d): %s", lookups, dry)
	}
	if r := h.run(append(args, "--confirm", dry.token())...); r.exit != 0 || lookups != 2 || h.up.Writes() != 1 {
		t.Fatalf("confirm (lookups %d): %s", lookups, r)
	}
	// The note is gone: a repeat is token_used, and the lookup never ran.
	r := h.run(append(args, "--confirm", dry.token())...)
	expectConflict(t, r, "token_used")
	if lookups != 2 {
		t.Fatalf("the repeated confirm ran the tool's lookup (%d)", lookups)
	}
	// The lookup's own error comes back as the tool's, nothing sent.
	if r := h.run("note", "remove", "--title", "Nope", "--dry-run"); r.code() != "E_NOT_FOUND" || r.details()["dispatch_state"] != "not_started" {
		t.Fatalf("a failed lookup: %s", r)
	}
	// A target that resolves differently at confirm is token_mismatch (target).
	tok := h.mustToken("note", "remove", "--title", "Travel")
	h.up.mu.Lock()
	h.up.notes["3"].Title, h.up.notes["9"] = "Elsewhere", &tnote{ID: "9", Ref: "notes/9", Title: "Travel", Version: 1}
	h.up.mu.Unlock()
	expectMismatch(t, h.run("note", "remove", "--title", "Travel", "--confirm", tok), "target")
	if h.up.Writes() != 1 {
		t.Fatalf("writes: %d", h.up.Writes())
	}
}

// Q1: BatchSpec.ResolveItems is resolved the same way.
func TestQ1ResolveItemsRunsInsideTheGate(t *testing.T) {
	lookups := 0
	h := newHarness(t)
	h.build = func(u *upstream) *App {
		a := testApp(u)
		c := a.commandByPath("note bulk-tag")
		c.RunE = func(cmd *cobra.Command, _ []string) error {
			ids, _ := cmd.Flags().GetStringSlice("ids")
			return a.WriteBatch(cmd, BatchSpec{ResolveItems: func(context.Context) ([]BatchItem, error) {
				lookups++
				var items []BatchItem
				for _, id := range ids {
					if _, ok := u.get(id); !ok {
						return nil, NotFound("No note has id "+id+".", "")
					}
					items = append(items, BatchItem{Target: Target{Key: "notes/" + id, Kind: "note"},
						Preview: func(context.Context) (Preview, error) { return Preview{Action: "Tag", CanApply: true}, nil },
						Apply: func(context.Context, Op) (Result, error) {
							u.write(func() { delete(u.notes, id) })
							return Result{}, nil
						}})
				}
				return items, nil
			}})
		}
		return a
	}
	args := []string{"note", "bulk-tag", "--ids", "1,2", "--tag", "x"}
	tok := h.mustToken(args...)
	if r := h.run(append(args, "--confirm", tok)...); r.exit != 0 || lookups != 2 {
		t.Fatalf("confirm (lookups %d): %s", lookups, r)
	}
	expectConflict(t, h.run(append(args, "--confirm", tok)...), "token_used")
	if lookups != 2 || h.up.Writes() != 2 {
		t.Fatalf("the repeat resolved again (%d) or wrote (%d)", lookups, h.up.Writes())
	}
}

// ---------------------------------------------------------------- Q2 proxy

// Q2: the transport's proxy comes from App.Env, not the process environment.
func TestQ2ProxyComesFromAppEnv(t *testing.T) {
	t.Setenv("HTTPS_PROXY", "http://process.example:1")
	env := map[string]string{}
	a := &App{Name: "notes", Env: func(k string) string { return env[k] }}
	proxyFor := func(target string) string {
		t.Helper()
		tr, e := a.HTTPTransport()
		if e != nil {
			t.Fatal(e)
		}
		u, _ := url.Parse(target)
		p, err := tr.Proxy(&http.Request{URL: u})
		if err != nil {
			t.Fatal(err)
		}
		if p == nil {
			return ""
		}
		return p.String()
	}
	if p := proxyFor("https://gitlab.example/api"); p != "" {
		t.Fatalf("the process environment leaked in: %s", p)
	}
	env["HTTPS_PROXY"] = "http://proxy.example:3128"
	if p := proxyFor("https://gitlab.example/api"); p != "http://proxy.example:3128" {
		t.Fatalf("HTTPS_PROXY from App.Env: %q", p)
	}
	env["NO_PROXY"] = "gitlab.example"
	if p := proxyFor("https://gitlab.example/api"); p != "" {
		t.Fatalf("NO_PROXY: %q", p)
	}
	env = map[string]string{"https_proxy": "http://lower.example:8080", "http_proxy": "http://plain.example:8080"}
	if p := proxyFor("https://gitlab.example/api"); p != "http://lower.example:8080" {
		t.Fatalf("https_proxy: %q", p)
	}
	if p := proxyFor("http://gitlab.example/api"); p != "http://plain.example:8080" {
		t.Fatalf("http_proxy: %q", p)
	}
}

// ---------------------------------------------------------------- Q3 profiles

// Q3: SetProfileSettings makes the file's first profile its default, keeps the
// file's order, and App.Profiles lists the profiles in file order.
func TestQ3DefaultProfileAndProfiles(t *testing.T) {
	h := settingsHarness(t)
	a := h.app()
	path := filepath.Join(h.state, "settings.json")
	if got := a.Profiles(); got == nil || len(got) != 0 {
		t.Fatalf("no file: %#v", got)
	}
	if e := a.SetProfileSettings("default", map[string]string{"host": "https://a.example"}); e != nil {
		t.Fatal(e)
	}
	if e := a.SetProfileSettings("work", map[string]string{"page_size": "50", "host": "https://w.example"}); e != nil {
		t.Fatal(e)
	}
	want := "{\n  \"default_profile\": \"default\",\n  \"profiles\": {\n    \"default\": {\n      \"host\": \"https://a.example\"\n    },\n" +
		"    \"work\": {\n      \"host\": \"https://w.example\",\n      \"page_size\": \"50\"\n    }\n  }\n}\n"
	if b, _ := os.ReadFile(path); string(b) != want {
		t.Fatalf("the written file:\n%s\nwant:\n%s", b, want)
	}
	if got := a.Profiles(); strings.Join(got, ",") != "default,work" {
		t.Fatalf("Profiles: %v", got)
	}
	// A file written by hand keeps its order, and its profiles without a default
	// stay without one (RC2-1).
	writeSettings(t, h, `{"profiles": {"zeta": {"page_size": 5}, "alpha": {"host": "https://al.example"}}}`)
	if got := a.Profiles(); strings.Join(got, ",") != "zeta,alpha" {
		t.Fatalf("file order: %v", got)
	}
	if e := a.SetProfileSettings("alpha", map[string]string{"page_size": "7"}); e != nil {
		t.Fatal(e)
	}
	want = "{\n  \"profiles\": {\n    \"zeta\": {\n      \"page_size\": 5\n    },\n" +
		"    \"alpha\": {\n      \"host\": \"https://al.example\",\n      \"page_size\": \"7\"\n    }\n  }\n}\n"
	if b, _ := os.ReadFile(path); string(b) != want {
		t.Fatalf("a rewritten file:\n%s\nwant:\n%s", b, want)
	}
	writeSettings(t, h, `{"profiles": {"Bad Name": {}}}`)
	if got := a.Profiles(); got == nil || len(got) != 0 {
		t.Fatalf("an invalid file: %#v", got)
	}
}

// RC2-1: default_profile is set only when SetProfileSettings creates the file's
// first profile (no file, or a file without profiles); profiles that exist without a
// default stay without one, the key null or absent as it was.
func TestRC21DefaultProfileOnlyForTheFirstProfile(t *testing.T) {
	h := settingsHarness(t)
	a := h.app()
	path := filepath.Join(h.state, "settings.json")
	for _, c := range []struct{ before, want string }{
		// no file: the first profile is the default
		{"", "{\n  \"default_profile\": \"lab\",\n  \"profiles\": {\n    \"lab\": {\n      \"host\": \"https://lab.example\"\n    }\n  }\n}\n"},
		// a file without profiles: the same
		{`{"profiles": {}}`, "{\n  \"default_profile\": \"lab\",\n  \"profiles\": {\n    \"lab\": {\n      \"host\": \"https://lab.example\"\n    }\n  }\n}\n"},
		{`{"default_profile": null, "profiles": null}`, "{\n  \"default_profile\": \"lab\",\n  \"profiles\": {\n    \"lab\": {\n      \"host\": \"https://lab.example\"\n    }\n  }\n}\n"},
		// profiles without a default: none is picked
		{`{"profiles": {"work": {}}}`, "{\n  \"profiles\": {\n    \"work\": {},\n    \"lab\": {\n      \"host\": \"https://lab.example\"\n    }\n  }\n}\n"},
		{`{"profiles": {"work": {}}, "default_profile": null}`,
			"{\n  \"default_profile\": null,\n  \"profiles\": {\n    \"work\": {},\n    \"lab\": {\n      \"host\": \"https://lab.example\"\n    }\n  }\n}\n"},
		// a default is kept, also when the written profile is new
		{`{"default_profile": "work", "profiles": {"work": {}}}`,
			"{\n  \"default_profile\": \"work\",\n  \"profiles\": {\n    \"work\": {},\n    \"lab\": {\n      \"host\": \"https://lab.example\"\n    }\n  }\n}\n"},
	} {
		_ = os.Remove(path)
		if c.before != "" {
			writeSettings(t, h, c.before)
		}
		if e := a.SetProfileSettings("lab", map[string]string{"host": "https://lab.example"}); e != nil {
			t.Fatalf("%s: %v", c.before, e)
		}
		if b, _ := os.ReadFile(path); string(b) != c.want {
			t.Fatalf("%s:\n%s\nwant:\n%s", c.before, b, c.want)
		}
	}
	// A second profile leaves the first one's default as it is.
	if e := a.SetProfileSettings("other", map[string]string{"host": "https://o.example"}); e != nil {
		t.Fatal(e)
	}
	if b, _ := os.ReadFile(path); !strings.HasPrefix(string(b), "{\n  \"default_profile\": \"work\",") {
		t.Fatalf("the default changed: %s", b)
	}
}

// ---------------------------------------------------------------- Q4 AllowUnknownProfile

// Q4: a command declared with Meta.AllowUnknownProfile runs under a profile the
// settings file does not have (auth logout cleaning up after a deleted one).
func TestQ4AllowUnknownProfile(t *testing.T) {
	h := settingsHarness(t)
	base := h.build
	h.build = func(u *upstream) *App {
		a := base(u)
		logout := &cobra.Command{Use: "logout", RunE: func(cmd *cobra.Command, _ []string) error {
			name, from := a.Profile()
			return a.OK(cmd, map[string]any{"profile": name, "source": from})
		}}
		a.Root.AddCommand(logout)
		a.Schema("logout_result", SchemaDef{Shape: "object", Fields: []string{"profile", "source"}})
		a.Register(logout, Meta{Summary: "Forget a profile's credentials", Effects: []Effect{Observe}, Output: "logout_result",
			AllowUnknownProfile: true, Examples: []string{"notes logout --profile gone"}})
		return a
	}
	writeSettings(t, h, `{"profiles": {"work": {"host": "https://w.example"}}}`)
	if r := h.run("logout", "--profile", "gone"); r.exit != 0 || r.data()["profile"] != "gone" || r.data()["source"] != "flag" {
		t.Fatalf("logout under a deleted profile: %s", r)
	}
	if r := h.run("probe", "--profile", "gone"); r.code() != "E_CONFIG" {
		t.Fatalf("other commands still refuse it: %s", r)
	}
}

// ---------------------------------------------------------------- Q5 default fields

// Q5: a command whose Meta.DefaultFields differ from its schema's shows them in
// reference (a copy of the schema under <output>_for_<path>) and in help.
func TestQ5ReferenceShowsTheCommandsDefaultFields(t *testing.T) {
	h := newHarness(t)
	h.build = func(u *upstream) *App {
		a := testApp(u)
		search := &cobra.Command{Use: "search", RunE: func(cmd *cobra.Command, _ []string) error {
			return a.List(cmd, Page{Items: []tnote{}})
		}}
		a.commandByPath("note").AddCommand(search)
		a.Register(search, Meta{Summary: "Search notes", Effects: []Effect{Observe}, Output: "note_page",
			DefaultFields: []string{"ref", "title"}, Examples: []string{"notes note search"}})
		return a
	}
	one := h.run("reference", "--command", "note search")
	entry, _ := one.data()["command"].(map[string]any)
	schemas, _ := one.data()["schemas"].(map[string]any)
	own, _ := schemas["note_page_for_note_search"].(map[string]any)
	if one.exit != 0 || entry["output"] != "note_page_for_note_search" || fmt.Sprint(own["default_fields"]) != "[ref title]" ||
		fmt.Sprint(own["fields"]) != fmt.Sprint(noteFields()) || own["shape"] != "page" {
		t.Fatalf("reference --command: %s", one)
	}
	if err := validateReferenceCommand(one.data()); err != nil {
		t.Fatal(err)
	}
	full := h.run("reference")
	if err := validateReferenceFull(full.data()); err != nil {
		t.Fatal(err)
	}
	fullSchemas, _ := full.data()["schemas"].(map[string]any)
	if fullSchemas["note_page_for_note_search"] == nil || fmt.Sprint(fullSchemas["note_page"].(map[string]any)["default_fields"]) != "[ref title tags]" {
		t.Fatalf("full reference schemas: %v", fullSchemas)
	}
	if list := h.run("reference", "--command", "note list"); list.data()["command"].(map[string]any)["output"] != "note_page" {
		t.Fatalf("a command on the schema's defaults keeps its name: %s", list)
	}
	if help := h.run("note", "search", "--help"); !strings.Contains(help.stdout, "Fields: ref, title (default)") {
		t.Fatalf("help: %s", help.stdout)
	}
}

// ---------------------------------------------------------------- Q6 shortcuts

// Q6: a shortcut word maps to one path; a nested noun under several parents needs no
// root shortcut.
func TestQ6RootShortcutsNeverCollide(t *testing.T) {
	a := testApp(newUpstream())
	a.AddShortcut("comment", "note", "comment") // the same mapping again is harmless
	a.AddShortcut("talk", "note", "comment")
	a.AddShortcut("talk", "note", "list")
	if got := strings.Join(a.Shortcuts["talk"], " "); got != "note comment" {
		t.Fatalf("the first mapping stays: %q", got)
	}
	problems := strings.Join(a.checkTree(), "\n")
	want := `App.AddShortcut("talk") maps it to "note list", but it already maps to "note comment"; a shortcut word names one path (the first is kept)`
	if !strings.Contains(problems, want) || strings.Contains(problems, `App.AddShortcut("comment")`) {
		t.Fatalf("tree check:\n%s", problems)
	}
	// comment under note and under issue: no root word could name both.
	b := testApp(newUpstream())
	b.Shortcuts = nil
	issue := &cobra.Command{Use: "issue"}
	b.Group(issue, "Work with issues")
	b.Root.AddCommand(issue)
	comment := &cobra.Command{Use: "comment"}
	b.Group(comment, "Comments on issues")
	issue.AddCommand(comment)
	list := &cobra.Command{Use: "list", RunE: func(cmd *cobra.Command, _ []string) error { return b.OK(cmd, map[string]any{}) }}
	comment.AddCommand(list)
	b.Schema("comment_page", SchemaDef{Shape: "object"})
	b.Register(list, Meta{Summary: "List comments", Effects: []Effect{Observe}, Output: "comment_page", Examples: []string{"notes issue comment list"}})
	if problems := strings.Join(b.checkTree(), "\n"); strings.Contains(problems, "not reachable from the root") {
		t.Fatalf("a shared noun needs no root shortcut:\n%s", problems)
	}
	// A nested noun under one parent still needs one, and a qualified word serves.
	c := testApp(newUpstream())
	c.Shortcuts = nil
	if problems := strings.Join(c.checkTree(), "\n"); !strings.Contains(problems, `nested group "note comment" is not reachable from the root; add App.Shortcuts["comment"] (CLI-022)`) {
		t.Fatalf("a unique nested noun:\n%s", problems)
	}
	c.AddShortcut("note-comment", "note", "comment")
	if problems := strings.Join(c.checkTree(), "\n"); strings.Contains(problems, "not reachable") {
		t.Fatalf("a qualified shortcut:\n%s", problems)
	}
}

// ---------------------------------------------------------------- Q7 command budget

// Q7: reference --command has 6,000 characters; a write with many flags fits.
func TestQ7ReferenceCommandBudget(t *testing.T) {
	if commandBudgetChars != 6000 {
		t.Fatalf("reference_command_chars is %d", commandBudgetChars)
	}
	h := newHarness(t)
	h.build = func(u *upstream) *App {
		a := testApp(u)
		c := a.commandByPath("note create")
		for i := 0; i < 15; i++ {
			c.Flags().String(fmt.Sprintf("option-%02d", i), "", strings.Repeat("A sentence about this option. ", 4))
		}
		return a
	}
	r := h.run("reference", "--command", "note create")
	if n := runeLen([]byte(r.stdout)); r.exit != 0 || n <= 4000 || n > commandBudgetChars {
		t.Fatalf("the entry is %d characters: %s", n, r)
	}
}

// ---------------------------------------------------------------- Q8 OpenOutput

// archiveOpts shapes `note archive --output <file>`, which streams the notes into
// the file with App.OpenOutput.
type archiveOpts struct {
	persist, overwrite bool
	open               func(path string) string // the path Apply opens (default: the previewed one)
	abort              bool                     // Apply aborts instead of committing
}

func archiveApp(h *tharness, o archiveOpts) {
	h.build = func(u *upstream) *App {
		a := testApp(u)
		c := &cobra.Command{Use: "archive", RunE: func(cmd *cobra.Command, _ []string) error {
			out, _ := cmd.Flags().GetString("output")
			path, _ := filepath.Abs(out)
			return a.Write(cmd, WriteSpec{
				Target: Target{Key: "notes", Kind: "collection"},
				Preview: func(context.Context) (Preview, error) {
					return Preview{Action: "Archive the notes", CanApply: true, Outputs: []string{path},
						Changes: []FieldChange{{Field: "path", After: path}}}, nil
				},
				Apply: func(context.Context, Op) (Result, error) {
					target := path
					if o.open != nil {
						target = o.open(path)
					}
					f, e := a.OpenOutput(target, o.overwrite)
					if e != nil {
						return Result{}, NotDispatched(e)
					}
					for i := 0; i < 3; i++ {
						_, _ = fmt.Fprintf(f, "line %d\n", i)
					}
					if o.abort {
						f.Abort()
						return Result{}, NotDispatched(errors.New("the archive was abandoned"))
					}
					if e := f.Commit(); e != nil {
						return Result{}, NotDispatched(e)
					}
					return Result{Data: map[string]any{"path": f.Path(), "bytes": f.Size(), "sha256": f.SHA256()}}, nil
				},
			})
		}}
		c.Flags().String("output", "", "File to write")
		a.commandByPath("note").AddCommand(c)
		effects := []Effect{Artifact}
		if o.persist {
			effects = append(effects, Persist)
		}
		a.Register(c, Meta{Summary: "Archive every note to a local file", Effects: effects, Output: "export_result",
			Examples: []string{"notes note archive --output notes.txt --dry-run", "notes note archive --output notes.txt --confirm <token>"}})
		return a
	}
}

// grantOutputs grants the local-file effects with dir as outputs and roots.
func grantOutputs(h *tharness, dir string) {
	h.grant(map[string]any{"effects": []string{"change", "deliver", "artifact", "persist"}, "outputs": []string{dir}, "roots": []string{dir}})
}

func noTempOutputs(t *testing.T, dir string) {
	t.Helper()
	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".aicli-output-") {
			t.Fatalf("a temporary output file was left: %s", e.Name())
		}
	}
}

// Q8: App.OpenOutput streams a file inside Apply: Commit puts a new file in place,
// overwrites only with overwrite and persist, checks the scope at that moment, and
// Abort leaves nothing.
func TestQ8OpenOutputStreamsAFile(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "notes.txt")
	h := newHarness(t)
	grantOutputs(h, dir)
	archiveApp(h, archiveOpts{})
	args := []string{"note", "archive", "--output", file}
	r := h.run(append(args, "--confirm", h.mustToken(args...))...)
	res, _ := r.data()["result"].(map[string]any)
	if b, _ := os.ReadFile(file); r.exit != 0 || string(b) != "line 0\nline 1\nline 2\n" || res["bytes"] != json.Number("21") || res["path"] != file {
		t.Fatalf("a new file: %s (content %q)", r, b)
	}
	if runtime.GOOS != "windows" {
		if info, err := os.Stat(file); err != nil || info.Mode().Perm() != 0o600 {
			t.Fatalf("mode: %v %v", info.Mode(), err)
		}
	}
	noTempOutputs(t, dir)

	// A file that appears before Commit: a command that only creates files refuses it
	// (Q9), even when it asked to overwrite.
	other := filepath.Join(dir, "other.txt")
	for _, o := range []archiveOpts{{}, {overwrite: true}} {
		_ = os.Remove(other)
		h2 := newHarness(t)
		grantOutputs(h2, dir)
		o.open = func(string) string {
			_ = os.WriteFile(other, []byte("theirs"), 0o600)
			return other
		}
		archiveApp(h2, o)
		args := []string{"note", "archive", "--output", filepath.Join(dir, "planned.txt")}
		r := h2.run(append(args, "--confirm", h2.mustToken(args...))...)
		if b, _ := os.ReadFile(other); r.code() != "E_VALIDATION" || string(b) != "theirs" || r.details()["path"] != other {
			t.Fatalf("%+v: a file that appeared: %s", o.overwrite, r)
		}
		noTempOutputs(t, dir)
	}

	// With persist and overwrite, an existing file under the scope's roots is replaced.
	h3 := newHarness(t)
	grantOutputs(h3, dir)
	archiveApp(h3, archiveOpts{persist: true, overwrite: true})
	_ = os.WriteFile(file, []byte("old"), 0o600)
	if r := h3.run(append(args, "--confirm", h3.mustToken(args...))...); r.exit != 0 {
		t.Fatalf("an overwrite: %s", r)
	}
	if b, _ := os.ReadFile(file); string(b) != "line 0\nline 1\nline 2\n" {
		t.Fatalf("the overwritten file: %q", b)
	}

	// The scope is checked at Commit: a path outside the scope's outputs is refused.
	outside := filepath.Join(t.TempDir(), "elsewhere.txt")
	h4 := newHarness(t)
	grantOutputs(h4, dir)
	archiveApp(h4, archiveOpts{open: func(string) string { return outside }})
	planned := []string{"note", "archive", "--output", filepath.Join(dir, "fine.txt")}
	r = h4.run(append(planned, "--confirm", h4.mustToken(planned...))...)
	if _, err := os.Stat(outside); r.code() != "E_FORBIDDEN" || err == nil {
		t.Fatalf("outside the outputs: %s", r)
	}

	// Abort removes the temporary file and writes nothing.
	h5 := newHarness(t)
	grantOutputs(h5, dir)
	archiveApp(h5, archiveOpts{abort: true})
	aborted := []string{"note", "archive", "--output", filepath.Join(dir, "aborted.txt")}
	r = h5.run(append(aborted, "--confirm", h5.mustToken(aborted...))...)
	if _, err := os.Stat(filepath.Join(dir, "aborted.txt")); r.exit == 0 || err == nil {
		t.Fatalf("abort: %s", r)
	}
	noTempOutputs(t, dir)
}

// ---------------------------------------------------------------- Q9 create-only

// saveApp adds `note save --output <file>`, which declares artifact only: it never
// overwrites. produce runs inside WriteOutput's Produce.
func saveApp(h *tharness, produce func()) {
	h.build = func(u *upstream) *App {
		a := testApp(u)
		c := &cobra.Command{Use: "save", RunE: func(cmd *cobra.Command, _ []string) error {
			out, _ := cmd.Flags().GetString("output")
			return a.WriteOutput(cmd, OutputSpec{Path: out, Produce: func(context.Context) ([]byte, error) {
				if produce != nil {
					produce()
				}
				return []byte("saved\n"), nil
			}})
		}}
		c.Flags().String("output", "", "File to write")
		a.commandByPath("note").AddCommand(c)
		a.Register(c, Meta{Summary: "Save the notes to a new local file", Effects: []Effect{Artifact}, Output: "export_result",
			Examples: []string{"notes note save --output notes.txt --dry-run", "notes note save --output notes.txt --confirm <token>"}})
		return a
	}
}

func expectCreateOnly(t *testing.T, r tout, path string) {
	t.Helper()
	if r.code() != "E_VALIDATION" || r.details()["problem"] != "invalid_value" || r.details()["path"] != path ||
		r.message() != "The output file already exists, and this command never overwrites files; nothing was written." ||
		r.details()["next_action"] != "Choose a new file name, or move the existing file away first." ||
		r.details()["dispatch_state"] != "not_started" {
		t.Fatalf("want the create-only refusal of %s: %s", path, r)
	}
}

// Q9: an existing output of a command that only creates files is E_VALIDATION, not
// a scope refusal asking for roots, at the dry-run, at the confirm and at write time.
func TestQ9CreateOnlyCommandAndAnExistingFile(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "notes.txt")
	h := newHarness(t)
	grantOutputs(h, dir)
	saveApp(h, nil)
	_ = os.WriteFile(file, []byte("mine"), 0o600)
	args := []string{"note", "save", "--output", file}
	expectCreateOnly(t, h.run(append(args, "--dry-run")...), file)
	// The file appears between the dry-run and the confirm.
	_ = os.Remove(file)
	tok := h.mustToken(args...)
	_ = os.WriteFile(file, []byte("mine"), 0o600)
	expectCreateOnly(t, h.run(append(args, "--confirm", tok)...), file)
	// The file appears while the content is produced.
	_ = os.Remove(file)
	h2 := newHarness(t)
	grantOutputs(h2, dir)
	saveApp(h2, func() { _ = os.WriteFile(file, []byte("mine"), 0o600) })
	expectCreateOnly(t, h2.run(append(args, "--confirm", h2.mustToken(args...))...), file)
	if b, _ := os.ReadFile(file); string(b) != "mine" {
		t.Fatalf("the existing file was changed: %q", b)
	}
	// A scope refusal for another reason does not ask for roots for it.
	h3 := newHarness(t)
	saveApp(h3, nil) // the default scope grants no artifact
	r := h3.run(append(args, "--dry-run")...)
	scope, _ := r.details()["scope"].(map[string]any)
	missing, _ := scope["missing"].(map[string]any)
	if r.code() != "E_FORBIDDEN" || missing["effects"] == nil || missing["roots"] != nil {
		t.Fatalf("the scope refusal: %s", r)
	}
}

// ---------------------------------------------------------------- Q10 renamed flags

// Q10: Meta.RenamedFlags keeps an old flag name working: it binds as the new name,
// adds the deprecated notice, refuses both names together, is listed in reference
// as a deprecated parameter and left out of help.
func TestQ10RenamedFlags(t *testing.T) {
	h := newHarness(t)
	h.build = func(u *upstream) *App {
		a := testApp(u)
		a.metaFor(a.commandByPath("note list")).RenamedFlags = map[string]string{"label": "tag"}
		a.metaFor(a.commandByPath("note update")).RenamedFlags = map[string]string{"name": "title"}
		return a
	}
	r := h.run("note", "list", "--label", "seed")
	if n := noticeOfType(r, "deprecated"); r.exit != 0 || r.data()["count"] != json.Number("3") || n == nil ||
		n["message"] != "`--label` is deprecated: use `--tag`." {
		t.Fatalf("the old name: %s", r)
	}
	r = h.run("note", "list", "--label", "seed", "--tag", "seed")
	if r.code() != "E_USAGE" || r.details()["problem"] != "conflicting_flags" || r.details()["parameter"] != "--label" ||
		r.message() != "--label is the old name of --tag; give only one of them." || r.details()["next_action"] != "Drop --label and keep --tag." {
		t.Fatalf("both names: %s", r)
	}
	ref := h.run("reference", "--command", "note list")
	if err := validateReferenceCommand(ref.data()); err != nil {
		t.Fatal(err)
	}
	var label map[string]any
	for _, p := range ref.data()["command"].(map[string]any)["params"].([]any) {
		if p.(map[string]any)["name"] == "label" {
			label = p.(map[string]any)
		}
	}
	if label == nil || label["description"] != "Deprecated: use `--tag`. Only notes with this tag" || label["type"] != "string" {
		t.Fatalf("reference lists the old name as deprecated: %v", label)
	}
	if help := h.run("note", "list", "--help"); strings.Contains(help.stdout, "--label") {
		t.Fatalf("help shows the old name: %s", help.stdout)
	}
	// The old name binds as the new one.
	tok := h.mustToken("note", "update", "1", "--name", "Renamed")
	if r := h.run("note", "update", "1", "--title", "Renamed", "--confirm", tok); r.exit != 0 {
		t.Fatalf("--name, then --title: %s", r)
	}
	// The tree check reports a rename it cannot apply.
	a := testApp(newUpstream())
	a.metaFor(a.commandByPath("note get")).RenamedFlags = map[string]string{"ident": "nope", "id": "title"}
	problems := strings.Join(a.checkTree(), "\n")
	for _, want := range []string{`"note get": Meta.RenamedFlags maps --ident to --nope, which the command does not define`,
		`"note get": Meta.RenamedFlags maps --id to --title, which the command does not define`} {
		if !strings.Contains(problems, want) {
			t.Fatalf("tree check: missing %q in\n%s", want, problems)
		}
	}
}

// ---------------------------------------------------------------- Q11 flag aliases

// Q11: Meta.FlagAliases accepts an alias on its command only, after App.FlagAliases.
func TestQ11PerCommandFlagAliases(t *testing.T) {
	h := newHarness(t)
	h.build = func(u *upstream) *App {
		a := testApp(u)
		a.metaFor(a.commandByPath("note list")).FlagAliases = map[string]string{"max": "limit"}
		a.metaFor(a.commandByPath("note update")).FlagAliases = map[string]string{"subject": "body"}
		return a
	}
	if r := h.run("note", "list", "--max", "1"); r.exit != 0 || r.data()["count"] != json.Number("1") {
		t.Fatalf("--max on note list: %s", r)
	}
	if r := h.run("note", "get", "1", "--max", "1"); r.code() != "E_USAGE" || r.details()["problem"] != "unknown_flag" {
		t.Fatalf("--max elsewhere: %s", r)
	}
	// App.FlagAliases (subject -> title) applies first.
	r := h.run("note", "update", "1", "--subject", "New", "--dry-run")
	changes, _ := r.data()["preview"].(map[string]any)["changes"].([]any)
	if r.exit != 0 || len(changes) != 1 || changes[0].(map[string]any)["field"] != "title" {
		t.Fatalf("the App-wide alias wins: %s", r)
	}
	a := testApp(newUpstream())
	a.metaFor(a.commandByPath("note get")).FlagAliases = map[string]string{"x": "nope", "id": "id"}
	problems := strings.Join(a.checkTree(), "\n")
	for _, want := range []string{`"note get": Meta.FlagAliases maps --x to --nope, which the command does not define`,
		`"note get": Meta.FlagAliases names --id, which the command already defines`} {
		if !strings.Contains(problems, want) {
			t.Fatalf("tree check: missing %q in\n%s", want, problems)
		}
	}
}

// ---------------------------------------------------------------- Q12 ApplyAll

// bulkAllApp replaces note bulk-tag with one that sends its items through
// BatchSpec.ApplyAll in calls of at most max items; mode picks the call's answer.
func bulkAllApp(h *tharness, max int, mode *string, calls *[]int) {
	h.build = func(u *upstream) *App {
		a := testApp(u)
		c := a.commandByPath("note bulk-tag")
		c.RunE = func(cmd *cobra.Command, _ []string) error {
			ids, _ := cmd.Flags().GetStringSlice("ids")
			var items []BatchItem
			for _, id := range ids {
				items = append(items, BatchItem{Target: Target{Key: "notes/" + id, Kind: "note"},
					Preview: func(context.Context) (Preview, error) { return Preview{Action: "Tag", CanApply: true}, nil }})
			}
			return a.WriteBatch(cmd, BatchSpec{Items: items, MaxPerCall: max, ApplyAll: func(_ context.Context, ops []Op) ([]ItemOutcome, error) {
				*calls = append(*calls, len(ops))
				switch *mode {
				case "not sent":
					return nil, NotDispatched(errors.New("the queue is full"))
				case "rejected":
					return nil, Rejected(FromHTTP(422, "Refused.", "", 0))
				case "lost":
					u.write(func() {})
					return nil, errors.New("the connection was reset")
				}
				var outs []ItemOutcome
				for _, op := range ops {
					u.write(func() {})
					outs = append(outs, ItemOutcome{Result: Result{Data: map[string]any{"id": op.ID}}})
				}
				if *mode == "short" {
					outs = outs[:len(outs)-1]
				}
				return outs, nil
			}})
		}
		return a
	}
}

// Q12: BatchSpec.ApplyAll sends the items in chunks of MaxPerCall, one call per
// chunk, and maps each outcome onto its item.
func TestQ12NativeBulkCalls(t *testing.T) {
	mode, calls := "ok", []int{}
	h := newHarness(t)
	bulkAllApp(h, 2, &mode, &calls)
	args := []string{"note", "bulk-tag", "--ids", "1,2,3,4,5", "--tag", "x"}
	h.up.mu.Lock()
	h.up.notes["4"] = &tnote{ID: "4", Ref: "notes/4", Title: "Four", Version: 1}
	h.up.notes["5"] = &tnote{ID: "5", Ref: "notes/5", Title: "Five", Version: 1}
	h.up.mu.Unlock()
	r := h.run(append(args, "--confirm", h.mustToken(args...))...)
	items, _ := r.data()["items"].([]any)
	if r.exit != 0 || fmt.Sprint(calls) != "[2 2 1]" || len(items) != 5 || h.up.Writes() != 5 {
		t.Fatalf("calls %v: %s", calls, r)
	}
	for _, it := range items {
		m := it.(map[string]any)
		res, _ := m["result"].(map[string]any)
		if m["ok"] != true || res["id"] != m["operation_id"] || m["receipt"].(map[string]any)["operation_id"] != m["operation_id"] {
			t.Fatalf("each outcome lands on its item: %v", m)
		}
	}
	summary := func(r tout, key string) string { return fmt.Sprint(r.details()["summary"].(map[string]any)[key]) }
	for _, c := range []struct{ mode, code, key, dispatch string }{
		{"short", "E_OUTCOME_UNKNOWN", "unknown", "unknown"},
		{"lost", "E_OUTCOME_UNKNOWN", "unknown", "unknown"},
		{"not sent", "", "failed", "not_started"},
		{"rejected", "", "failed", "dispatched"},
	} {
		mode, calls = c.mode, nil
		h := newHarness(t)
		bulkAllApp(h, 0, &mode, &calls)
		args := []string{"note", "bulk-tag", "--ids", "1,2", "--tag", "y"}
		r := h.run(append(args, "--confirm", h.mustToken(args...))...)
		if r.exit == 0 || fmt.Sprint(calls) != "[2]" || summary(r, c.key) != "2" || r.details()["dispatch_state"] != c.dispatch ||
			(c.code != "" && r.code() != c.code) {
			t.Fatalf("%s: %s", c.mode, r)
		}
	}
	// A call that returns the wrong number of outcomes is a defect (conformance
	// reports the App's defects).
	mode = "short"
	h2 := newHarness(t)
	bulkAllApp(h2, 0, &mode, &calls)
	short := []string{"note", "bulk-tag", "--ids", "1,2", "--tag", "z"}
	tok := h2.mustToken(short...)
	a := h2.app()
	a.Stdout, a.Stderr = &strings.Builder{}, &strings.Builder{}
	a.Execute(append(short, "--confirm", tok))
	if !contains(a.defects, "BatchSpec.ApplyAll returned 1 outcomes for 2 items; every item of that call is reported unknown.") {
		t.Fatalf("defects: %v", a.defects)
	}
}

// ---------------------------------------------------------------- Q13 dangerous plans

// wipeApp adds `note wipe <id>`, a command that is not dangerous whose plan is when
// *dangerous holds (a commit that deletes a file).
func wipeApp(h *tharness, dangerous *bool) {
	h.build = func(u *upstream) *App {
		a := testApp(u)
		c := &cobra.Command{Use: "wipe <id>", RunE: func(cmd *cobra.Command, _ []string) error {
			id, _ := cmd.Flags().GetString("id")
			return a.Write(cmd, WriteSpec{Target: Target{Key: "notes/" + id, Kind: "note"},
				Preview: func(context.Context) (Preview, error) {
					return Preview{Action: "Clear note " + id, CanApply: true, Dangerous: *dangerous,
						Changes: []FieldChange{{Field: "body", After: ""}}}, nil
				},
				Apply: func(context.Context, Op) (Result, error) {
					u.write(func() { u.notes[id].Body = "" })
					return Result{}, nil
				}})
		}}
		c.Flags().String("id", "", "Note id")
		a.commandByPath("note").AddCommand(c)
		a.Register(c, Meta{Summary: "Clear a note's body", Effects: []Effect{Change}, Output: "note_write", Positional: "id",
			Examples: []string{"notes note wipe 1 --dry-run", "notes note wipe 1 --confirm <token>"}})
		return a
	}
}

// Q13: Preview.Dangerous makes one plan dangerous: --dangerous in both steps, the
// preview says so, the scope must grant dangerous, and a plan whose danger changes
// is state_drift.
func TestQ13APlanCanBeDangerous(t *testing.T) {
	dangerous := true
	h := newHarness(t)
	wipeApp(h, &dangerous)
	r := h.run("note", "wipe", "1", "--dry-run")
	pv, _ := r.details()["preview"].(map[string]any)
	if r.code() != "E_CONFIRMATION_REQUIRED" || r.details()["problem"] != "dangerous_flag_required" || pv["dangerous"] != true ||
		r.details()["next_action"] != "details.preview shows the plan; run the same command again with --dangerous added to get its confirm token (keep --dry-run if you used it)." {
		t.Fatalf("without --dangerous: %s", r)
	}
	dry := h.run("note", "wipe", "1", "--dangerous", "--dry-run")
	if dry.token() == "" || dry.data()["preview"].(map[string]any)["dangerous"] != true {
		t.Fatalf("with --dangerous: %s", dry)
	}
	if r := h.run("note", "wipe", "1", "--confirm", dry.token()); r.details()["problem"] != "dangerous_flag_required" || h.up.Writes() != 0 {
		t.Fatalf("a confirm without --dangerous: %s", r)
	}
	if r := h.run("note", "wipe", "1", "--dangerous", "--confirm", dry.token()); r.exit != 0 || h.up.Writes() != 1 {
		t.Fatalf("the confirm: %s", r)
	}
	// The scope must grant dangerous.
	h.grant(map[string]any{"dangerous": false})
	if r := h.run("note", "wipe", "2", "--dangerous", "--dry-run"); r.code() != "E_FORBIDDEN" ||
		r.details()["scope"].(map[string]any)["missing"].(map[string]any)["dangerous"] != true {
		t.Fatalf("a scope without dangerous: %s", r)
	}
	h.grant(nil)
	// A plan that becomes dangerous after the dry-run is another plan.
	dangerous = false
	tok := h.mustToken("note", "wipe", "2")
	dangerous = true
	r = h.run("note", "wipe", "2", "--confirm", tok)
	expectConflict(t, r, "state_drift")
	if r.message() != "The target changed since the preview (it is now dangerous); nothing was sent." || h.up.Writes() != 1 {
		t.Fatalf("a plan that became dangerous: %s", r)
	}
	if ref := h.run("reference", "--command", "note wipe"); ref.data()["command"].(map[string]any)["dangerous"] != false {
		t.Fatalf("the command itself is not dangerous: %s", ref)
	}
}

// ---------------------------------------------------------------- Q14 output format

// Q14: OutputFormat tells a command the format of this run.
func TestQ14OutputFormat(t *testing.T) {
	h := newHarness(t)
	h.build = func(u *upstream) *App {
		a := testApp(u)
		c := &cobra.Command{Use: "show-format", RunE: func(cmd *cobra.Command, _ []string) error {
			if f := OutputFormat(cmd); f == "raw" {
				return a.OK(cmd, "raw:"+f)
			} else {
				return a.OK(cmd, map[string]any{"format": f})
			}
		}}
		a.Root.AddCommand(c)
		a.Schema("format_result", SchemaDef{Shape: "object", Fields: []string{"format"}})
		a.Register(c, Meta{Summary: "Show the output format", Effects: []Effect{Observe}, Output: "format_result", RawOutput: true,
			Examples: []string{"notes show-format"}})
		return a
	}
	for args, want := range map[string]string{"": "json", "--json": "json", "--format text": "format: text\n", "--format raw": "raw:raw"} {
		r := h.run(append([]string{"show-format"}, strings.Fields(args)...)...)
		got := r.stdout
		if r.env != nil {
			got = fmt.Sprint(r.data()["format"])
		}
		if r.exit != 0 || got != want {
			t.Fatalf("%q: got %q, want %q", args, got, want)
		}
	}
	c := &cobra.Command{Use: "x"}
	c.Flags().String("format", "", "")
	c.Flags().Bool("json", false, "")
	_ = c.Flags().Set("format", "TEXT")
	if f := OutputFormat(c); f != "text" {
		t.Fatalf("outside a run: %q", f)
	}
}

// ---------------------------------------------------------------- Q15 durations and times

// Q15: ParseDuration and ParseTime read CLI-028's forms.
func TestQ15ParseDurationAndTime(t *testing.T) {
	for in, want := range map[string]time.Duration{"500ms": 500 * time.Millisecond, "30s": 30 * time.Second, "5m": 5 * time.Minute,
		"2h": 2 * time.Hour, "7d": 7 * 24 * time.Hour, "0s": 0} {
		if got, err := ParseDuration(in); err != nil || got != want {
			t.Errorf("ParseDuration(%q) = %v, %v", in, got, err)
		}
	}
	for _, in := range []string{"", "1m30s", "1.5h", "-5m", "5", "5w", "99999999999999999999d"} {
		if _, err := ParseDuration(in); err == nil {
			t.Errorf("ParseDuration(%q) accepted", in)
		}
	}
	if _, err := ParseDuration("5w"); err == nil || err.Error() != `"5w" is not a duration: use a whole number and one unit, ms, s, m, h or d (500ms, 30s, 5m, 2h, 7d)` {
		t.Errorf("the duration error: %v", err)
	}
	now := time.Date(2026, 10, 14, 12, 0, 0, 0, time.UTC)
	for in, want := range map[string]string{"7d": "2026-10-07T12:00:00Z", "30m": "2026-10-14T11:30:00Z", "2026-06-01": "2026-06-01T00:00:00Z",
		"2026-06-01T08:00:00Z": "2026-06-01T08:00:00Z", "2026-06-01T10:00:00+02:00": "2026-06-01T08:00:00Z"} {
		if got, err := ParseTime(in, now); err != nil || got.Format(time.RFC3339) != want || got.Location() != time.UTC {
			t.Errorf("ParseTime(%q) = %v, %v", in, got, err)
		}
	}
	if _, err := ParseTime("last week", now); err == nil ||
		err.Error() != `"last week" is not a time: use an ISO 8601 date (2026-06-01), a date and time (2026-06-01T08:00:00Z) or a time ago (7d, 24h, 30m)` {
		t.Errorf("the time error: %v", err)
	}
}

// ---------------------------------------------------------------- Q16 write results

// Q16: a confirmed write's result takes its schema's default fields without --fields.
func TestQ16WriteResultUsesDefaultFields(t *testing.T) {
	h := newHarness(t)
	h.build = func(u *upstream) *App {
		a := testApp(u)
		a.Schema("note_brief", SchemaDef{Shape: "object", Fields: noteFields(), DefaultFields: []string{"ref", "title"}})
		a.metaFor(a.commandByPath("note update")).Output = "note_brief"
		return a
	}
	for fields, want := range map[string]string{"": "ref,title", "+body": "body,ref,title", "all": strings.Join(noteFields(), ","), "id": "id,ref"} {
		args := []string{"note", "update", "1", "--title", "T" + fields}
		confirm := append(append([]string{}, args...), "--confirm", h.mustToken(args...))
		if fields != "" {
			confirm = append(confirm, "--fields", fields)
		}
		r := h.run(confirm...)
		res, _ := r.data()["result"].(map[string]any)
		keys := sortedKeys(res)
		if r.exit != 0 || strings.Join(keys, ",") != sortedJoin(want) || r.data()["receipt"].(map[string]any)["outcome"] == nil {
			t.Fatalf("--fields %q: %v: %s", fields, keys, r)
		}
	}
}

func sortedJoin(csv string) string {
	set := map[string]bool{}
	for _, s := range strings.Split(csv, ",") {
		set[s] = true
	}
	return strings.Join(sortedKeys(set), ",")
}

// ---------------------------------------------------------------- Q17 WantsField

// Q17: WantsField answers whether the response keeps a field.
func TestQ17WantsField(t *testing.T) {
	h := newHarness(t)
	h.build = func(u *upstream) *App {
		a := testApp(u)
		c := &cobra.Command{Use: "peek", RunE: func(cmd *cobra.Command, _ []string) error {
			return a.List(cmd, Page{Items: []map[string]any{}, Scope: map[string]any{"wants": map[string]any{
				"body": WantsField(cmd, "body"), "title": WantsField(cmd, "title"), "ref": WantsField(cmd, "ref")}}})
		}}
		a.commandByPath("note").AddCommand(c)
		a.Register(c, Meta{Summary: "Peek at notes", Effects: []Effect{Observe}, Output: "note_page", Examples: []string{"notes note peek"}})
		return a
	}
	for fields, want := range map[string]string{"": "map[body:false ref:true title:true]", "body": "map[body:true ref:true title:false]",
		"+body": "map[body:true ref:true title:true]", "all": "map[body:true ref:true title:true]"} {
		args := []string{"note", "peek"}
		if fields != "" {
			args = append(args, "--fields", fields)
		}
		r := h.run(args...)
		if got := fmt.Sprint(r.data()["scope"].(map[string]any)["wants"]); r.exit != 0 || got != want {
			t.Fatalf("--fields %q: %s, want %s", fields, got, want)
		}
	}
	if !WantsField(&cobra.Command{Use: "x"}, "anything") {
		t.Fatal("outside a run every field is wanted")
	}
}

// ---------------------------------------------------------------- Q18 schemas once

// Q18: a schema name registered twice is a defect; the first definition stays.
func TestQ18SchemaRegisteredTwice(t *testing.T) {
	a := testApp(newUpstream())
	a.Schema("note", SchemaDef{Shape: "page"})
	if a.reg.schemas["note"].Shape != "object" {
		t.Fatal("the second definition replaced the first")
	}
	if problems := strings.Join(a.checkTree(), "\n"); !strings.Contains(problems,
		`App.Schema("note") was called twice; register each schema name once (the first definition is kept)`) {
		t.Fatalf("tree check:\n%s", problems)
	}
}

// ---------------------------------------------------------------- Q19 batch action

// Q19: BatchSpec.Action is the batch preview's action.
func TestQ19BatchAction(t *testing.T) {
	for action, want := range map[string]string{"Tag the notes": "Tag the notes", "": "Tag notes (2 targets)"} {
		h := newHarness(t)
		h.build = func(u *upstream) *App {
			a := testApp(u)
			c := a.commandByPath("note bulk-tag")
			c.RunE = func(cmd *cobra.Command, _ []string) error {
				ids, _ := cmd.Flags().GetStringSlice("ids")
				var items []BatchItem
				for _, id := range ids {
					items = append(items, BatchItem{Target: Target{Key: "notes/" + id},
						Preview: func(context.Context) (Preview, error) { return Preview{Action: "Tag notes", CanApply: true}, nil },
						Apply:   func(context.Context, Op) (Result, error) { return Result{}, nil }})
				}
				return a.WriteBatch(cmd, BatchSpec{Items: items, Action: action})
			}
			return a
		}
		r := h.run("note", "bulk-tag", "--ids", "1,2", "--tag", "x", "--dry-run")
		if got := r.data()["preview"].(map[string]any)["action"]; got != want {
			t.Fatalf("Action %q: %v", action, got)
		}
	}
}

// ---------------------------------------------------------------- Q20 untrusted

// Q20: meta.untrusted names only paths the projected response still has.
func TestQ20UntrustedFollowsTheProjection(t *testing.T) {
	h := newHarness(t)
	untrusted := func(r tout) string { return fmt.Sprint(r.env["meta"].(map[string]any)["untrusted"]) }
	if r := h.run("note", "list"); untrusted(r) != "[data.items[].title]" {
		t.Fatalf("default fields: %s", untrusted(r))
	}
	if r := h.run("note", "list", "--fields", "all"); untrusted(r) != "[data.items[].body data.items[].title]" {
		t.Fatalf("--fields all: %s", untrusted(r))
	}
	if r := h.run("note", "list", "--tag", "none"); r.env["meta"].(map[string]any)["untrusted"] != nil {
		t.Fatalf("an empty page: %s", untrusted(r))
	}
	args := []string{"note", "create", "--title", "Q20"}
	r := h.run(append(args, "--confirm", h.mustToken(args...), "--fields", "id")...)
	if u := untrusted(r); r.exit != 0 || strings.Contains(u, "data.result.title") || !strings.Contains(u, "data.receipt.observed") {
		t.Fatalf("a projected write result: %s", u)
	}
}

// ---------------------------------------------------------------- Q21 examples

// Q21: an example written with a root shortcut or an alias passes the tree check and
// R01, which resolve it as the parser does; an example of another command fails both.
func TestQ21ExamplesWithShortcutsAndAliases(t *testing.T) {
	build := func(u *upstream) *App {
		a := testApp(u)
		a.metaFor(a.commandByPath("note comment add")).Examples = []string{"notes comment add 1 --text Hi --dry-run",
			"notes comment create 1 --text Hi --confirm <token>"}
		a.metaFor(a.commandByPath("note delete")).Examples = []string{"notes note rm 1 --dangerous --dry-run",
			"notes notes delete 1 --dangerous --confirm <token>"}
		return a
	}
	if problems := build(newUpstream()).checkTree(); len(problems) > 0 {
		t.Fatalf("tree check: %v", problems)
	}
	h, _ := testHarness(build)
	rec := &recordTB{}
	RunConformance(rec, h)
	for _, e := range append(rec.errors, rec.fatal...) {
		if strings.HasPrefix(e, "R01") || strings.HasPrefix(e, "R02") {
			t.Fatalf("conformance: %s", e)
		}
	}
	a := testApp(newUpstream())
	a.metaFor(a.commandByPath("note get")).Examples = []string{"notes note list"}
	if problems := strings.Join(a.checkTree(), "\n"); !strings.Contains(problems,
		`"note get": example "notes note list" does not run this command; start it with "notes" and the command's path, a shortcut or an alias`) {
		t.Fatalf("tree check:\n%s", problems)
	}
}

// ---------------------------------------------------------------- Q22 Debugf

// Q22: Debugf is safe from several goroutines (run with -race): every line whole.
func TestQ22DebugfFromSeveralGoroutines(t *testing.T) {
	h := newHarness(t)
	h.env["NOTES_DEBUG"] = "1"
	h.build = func(u *upstream) *App {
		a := testApp(u)
		c := &cobra.Command{Use: "fan", RunE: func(cmd *cobra.Command, _ []string) error {
			var wg sync.WaitGroup
			for g := 0; g < 8; g++ {
				wg.Add(1)
				go func(g int) {
					defer wg.Done()
					for i := 0; i < 25; i++ {
						a.Debugf("fan %d.%d", g, i)
						a.AddSecret("secret-" + strconv.Itoa(g))
						a.Notice(Notice{Type: "fan", Message: "goroutine " + strconv.Itoa(g)})
					}
				}(g)
			}
			wg.Wait()
			return a.OK(cmd, map[string]any{})
		}}
		a.Root.AddCommand(c)
		a.Schema("fan_result", SchemaDef{Shape: "object"})
		a.Register(c, Meta{Summary: "Fan out", Effects: []Effect{Observe}, Output: "fan_result", Examples: []string{"notes fan"}})
		return a
	}
	r := h.run("fan")
	lines := strings.Split(strings.TrimSpace(r.stderr), "\n")
	count := 0
	for _, l := range lines {
		if strings.HasPrefix(l, "debug: fan ") {
			count++
		}
	}
	notices, _ := r.env["meta"].(map[string]any)["notices"].([]any)
	if r.exit != 0 || count != 200 || len(notices) != 8 {
		t.Fatalf("%d debug lines, %d notices: %s", count, len(notices), r)
	}
}

// ---------------------------------------------------------------- Q23 streams

// logApp adds `note log`: a stream of lines whose items have their own schema
// (Meta.StreamItem), and raw output; it fails after failAfter lines when set.
func logApp(h *tharness, failAfter int) {
	h.build = func(u *upstream) *App {
		a := testApp(u)
		c := &cobra.Command{Use: "log", RunE: func(cmd *cobra.Command, _ []string) error {
			s, err := a.Stream(cmd)
			if err != nil {
				return err
			}
			for i, line := range []string{"a", "b", "c"} {
				if failAfter > 0 && i == failAfter {
					return FromHTTP(503, "The log stopped.", "", 0)
				}
				var item any = map[string]any{"line": line}
				if OutputFormat(cmd) == "raw" {
					item = line + "\n"
				}
				if err := s.Item(item); err != nil {
					return err
				}
			}
			return nil
		}}
		a.commandByPath("note").AddCommand(c)
		a.Schema("note_log", SchemaDef{Shape: "object", Fields: []string{"log", "lines"}, Untrusted: []string{"log"}})
		a.Schema("log_line", SchemaDef{Shape: "object", Fields: []string{"line"}, Untrusted: []string{"line"}})
		a.Register(c, Meta{Summary: "Stream the notes' log as NDJSON, one line per entry", Effects: []Effect{Observe}, Output: "note_log",
			StreamItem: "log_line", RawOutput: true, Examples: []string{"notes note log"}})
		return a
	}
}

// Q23: stream items take their untrusted paths from the item schema; a raw stream
// writes the bytes only, never a JSON summary line.
func TestQ23RawStreamsAndItemSchemas(t *testing.T) {
	h := newHarness(t)
	logApp(h, 0)
	r := h.run("note", "log")
	lines := strings.Split(strings.TrimSpace(r.stdout), "\n")
	if r.exit != 0 || len(lines) != 4 {
		t.Fatalf("the stream: %s", r)
	}
	for _, l := range lines[:3] {
		var line map[string]any
		if err := json.Unmarshal([]byte(l), &line); err != nil || fmt.Sprint(line["meta"].(map[string]any)["untrusted"]) != "[data.line]" {
			t.Fatalf("an item takes the item schema's paths: %s", l)
		}
	}
	if raw := h.run("note", "log", "--format", "raw"); raw.exit != 0 || raw.stdout != "a\nb\nc\n" {
		t.Fatalf("a raw stream: %q", raw.stdout)
	}
	h2 := newHarness(t)
	logApp(h2, 1)
	raw := h2.run("note", "log", "--format", "raw")
	if raw.exit == 0 || raw.stdout != "a\n" || !strings.HasPrefix(raw.stderr, "error ") {
		t.Fatalf("a raw stream that fails: exit %d stdout %q stderr %q", raw.exit, raw.stdout, raw.stderr)
	}
	a := testApp(newUpstream())
	a.metaFor(a.commandByPath("note tail")).StreamItem = "nope"
	if problems := strings.Join(a.checkTree(), "\n"); !strings.Contains(problems, `"note tail" names stream item schema "nope", which is not registered`) {
		t.Fatalf("tree check:\n%s", problems)
	}
}

// ---------------------------------------------------------------- Q24 budgets

// Q24: ValidateEnvelope no longer holds a success to the success budget; R04 does,
// for the suite's default invocations.
func TestQ24TheSuccessBudgetIsR04s(t *testing.T) {
	big := map[string]any{"ok": true, "schema_version": SchemaVersion, "data": map[string]any{"text": strings.Repeat("x", 9000)},
		"meta": map[string]any{"duration_ms": 1}}
	b, _ := encodeJSON(big, false)
	if err := ValidateEnvelope(b); err != nil {
		t.Fatalf("ValidateEnvelope: %v", err)
	}
	if size, over := overSuccessBudget(decodeGeneric(b)); !over || size <= successBudgetChars {
		t.Fatalf("the R04 budget check: %d", size)
	}
	if defaultInvocation([]string{"note", "list", "--limit", "100"}) || defaultInvocation([]string{"note", "list", "--fields=all"}) ||
		!defaultInvocation([]string{"note", "list", "--tag", "x"}) {
		t.Fatal("defaultInvocation")
	}
	h, _ := testHarness(testApp)
	h.Samples["note create"] = []string{"--title", "Hello", "--body", strings.Repeat("long body ", 1000)}
	rec := &recordTB{}
	RunConformance(rec, h)
	if !strings.Contains(strings.Join(rec.errors, "\n"), "over the 8000 budget of a default invocation (CLI-042)") {
		t.Fatalf("R04 does not report the budget:\n%s", strings.Join(rec.errors, "\n"))
	}
}

// ---------------------------------------------------------------- Q25 E_CONFIG

// Q25: with settings declared, every tool command's error codes include E_CONFIG.
func TestQ25ConfigErrorInErrorCodes(t *testing.T) {
	codes := func(h *tharness, path string) []any {
		r := h.run("reference", "--command", path)
		list, _ := r.data()["command"].(map[string]any)["error_codes"].([]any)
		return list
	}
	h := settingsHarness(t)
	if !listHasString(codes(h, "probe"), "E_CONFIG") || !listHasString(codes(h, "note get"), "E_CONFIG") || listHasString(codes(h, "context"), "E_CONFIG") {
		t.Fatalf("probe %v, note get %v, context %v", codes(h, "probe"), codes(h, "note get"), codes(h, "context"))
	}
	if plain := newHarness(t); listHasString(codes(plain, "note get"), "E_CONFIG") {
		t.Fatal("no settings, no E_CONFIG")
	}
}

// ---------------------------------------------------------------- Q26 SetResolved

// Q26: SetResolved sets a flag the user did not give when its value came inside
// another input: inbox/1 and 1 --box inbox bind alike.
func TestQ26SetResolvedForAPartOfAnotherInput(t *testing.T) {
	h := newHarness(t)
	h.build = func(u *upstream) *App {
		a := testApp(u)
		c := &cobra.Command{Use: "file <id>", RunE: func(cmd *cobra.Command, _ []string) error {
			id, _ := cmd.Flags().GetString("id")
			if box, iid, ok := strings.Cut(id, "/"); ok {
				if err := SetResolved(cmd, "box", box); err != nil {
					return err
				}
				if err := SetResolved(cmd, "id", iid); err != nil {
					return err
				}
				id = iid
			}
			box, _ := cmd.Flags().GetString("box")
			return a.Write(cmd, WriteSpec{Target: Target{Key: "boxes/" + box + "/notes/" + id},
				Preview: func(context.Context) (Preview, error) {
					return Preview{Action: "File note " + id, CanApply: true, Changes: []FieldChange{{Field: "box", After: box}}}, nil
				},
				Apply: func(context.Context, Op) (Result, error) { u.write(func() {}); return Result{}, nil }})
		}}
		c.Flags().String("id", "", "Note id, or box/id")
		c.Flags().String("box", "", "Box")
		a.commandByPath("note").AddCommand(c)
		a.Register(c, Meta{Summary: "File a note in a box", Effects: []Effect{Change}, Output: "note_write", Positional: "id",
			Examples: []string{"notes note file inbox/1 --dry-run", "notes note file inbox/1 --confirm <token>"}})
		return a
	}
	dry := h.run("note", "file", "inbox/1", "--dry-run")
	inputs, _ := dry.data()["preview"].(map[string]any)["inputs"].(map[string]any)
	if dry.exit != 0 || inputs["box"] != "inbox" || inputs["id"] != "1" {
		t.Fatalf("the parts bind: %s", dry)
	}
	if r := h.run("note", "file", "1", "--box", "inbox", "--confirm", dry.token()); r.exit != 0 || h.up.Writes() != 1 {
		t.Fatalf("1 --box inbox confirms inbox/1's token: %s", r)
	}
}
