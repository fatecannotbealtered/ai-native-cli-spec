// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
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

// Regression tests for round 2 of the fixes before v2.0.0-rc.1 (FIXES-RC1.md R2),
// found by migrating gitlab-cli to the library; one or more per item, named after
// it.

// ---------------------------------------------------------------- P1 aliases

// P1: aliases are per parent: label list and milestone list may both have the alias
// ls. An alias still may not take a sibling's name or alias, and a root alias may
// not take a root command's or a built-in's name.
func TestP1AliasesArePerParent(t *testing.T) {
	h := newHarness(t)
	h.build = func(u *upstream) *App {
		a := testApp(u)
		a.Schema("noun_result", SchemaDef{Shape: "object", Fields: []string{"noun"}})
		for _, noun := range []string{"label", "milestone"} {
			group := &cobra.Command{Use: noun}
			a.Group(group, "Work with "+noun+"s")
			a.Root.AddCommand(group)
			list := &cobra.Command{Use: "list", RunE: func(cmd *cobra.Command, _ []string) error {
				return a.OK(cmd, map[string]any{"noun": noun})
			}}
			group.AddCommand(list)
			a.Register(list, Meta{Summary: "List " + noun + "s", Effects: []Effect{Observe}, Output: "noun_result",
				Aliases: []string{"ls"}, Examples: []string{"notes " + noun + " list"}})
		}
		return a
	}
	ref := h.run("reference")
	if ref.exit != 0 {
		t.Fatalf("reference: %s", ref)
	}
	if err := validateReferenceFull(ref.data()); err != nil {
		t.Fatalf("two groups with the alias ls must validate: %v", err)
	}
	if r := h.run("milestone", "ls"); r.exit != 0 || r.data()["noun"] != "milestone" {
		t.Fatalf("milestone ls: %s", r)
	}
	for _, c := range []struct {
		paths   []string
		aliases map[string][]string
		bad     string
	}{
		{[]string{"label list", "milestone list"}, map[string][]string{"label list": {"ls"}, "milestone list": {"ls"}}, ""},
		{[]string{"label list", "label ls"}, map[string][]string{"label list": {"ls"}}, "label ls"},
		{[]string{"label list", "label search"}, map[string][]string{"label list": {"find"}, "label search": {"find"}}, "label find"},
		{[]string{"label list", "deploy"}, map[string][]string{"deploy": {"label"}}, "label"},
		{[]string{"deploy"}, map[string][]string{"deploy": {"context"}}, "context"},
	} {
		v := &validation{}
		validateTree(v, "data.commands", c.paths, c.aliases)
		want := ""
		if c.bad != "" {
			want = "data.commands: alias '" + c.bad + "' collides with a command path or another alias (CLI-021)"
		}
		if got := strings.Join(v.problems, "; "); got != want {
			t.Errorf("%v %v:\n got %q\nwant %q", c.paths, c.aliases, got, want)
		}
	}
}

// ---------------------------------------------------------------- P2 ids

// P2: an input named id, iid or *_id shows as a string in preview.inputs and in the
// operation record whatever the flag's type (CLI-007); other numbers stay numbers.
func TestP2IDInputsAreStrings(t *testing.T) {
	h := newHarness(t)
	h.build = func(u *upstream) *App {
		a := testApp(u)
		pin := &cobra.Command{Use: "pin <iid>", RunE: func(cmd *cobra.Command, _ []string) error {
			iid, _ := cmd.Flags().GetInt("iid")
			return a.Write(cmd, WriteSpec{
				Target: Target{Key: fmt.Sprintf("notes/%d", iid), Kind: "note"},
				Preview: func(context.Context) (Preview, error) {
					return Preview{Action: "Pin a note", CanApply: true, Changes: []FieldChange{{Field: "pinned", After: true}}}, nil
				},
				Apply: func(context.Context, Op) (Result, error) {
					u.write(func() {})
					return Result{}, nil
				},
			})
		}}
		pin.Flags().Int("iid", 0, "Note iid")
		pin.Flags().Int("label-id", 0, "Label id")
		pin.Flags().Int("weight", 0, "Pin weight")
		a.commandByPath("note").AddCommand(pin)
		a.Register(pin, Meta{Summary: "Pin a note", Effects: []Effect{Change}, Output: "note_write", Positional: "iid",
			Examples: []string{"notes note pin 5 --dry-run", "notes note pin 5 --confirm <token>"}})
		return a
	}
	args := []string{"note", "pin", "5", "--label-id", "7", "--weight", "3"}
	r := h.run(append(args, "--dry-run")...) // h.run validates the envelope (ids are strings)
	pv, _ := r.data()["preview"].(map[string]any)
	inputs, _ := pv["inputs"].(map[string]any)
	if r.exit != 0 || inputs["iid"] != "5" || inputs["label_id"] != "7" || inputs["weight"] != json.Number("3") {
		t.Fatalf("preview.inputs: %s", r)
	}
	id, _ := r.data()["operation_id"].(string)
	stored, _ := recordFile(t, h, id)["preview"].(map[string]any)
	if in, _ := stored["inputs"].(map[string]any); in["iid"] != "5" || in["label_id"] != "7" || in["weight"] != float64(3) {
		t.Fatalf("the operation record's inputs: %v", stored)
	}
	if show := h.run("operation", "show", "--id", id); show.exit != 0 {
		t.Fatalf("operation show: %s", show)
	}
	if c := h.run(append(args, "--confirm", r.token())...); c.exit != 0 {
		t.Fatalf("confirm: %s", c)
	}
}

// ---------------------------------------------------------------- P3 changelog

// P3: changelog without --since lists the newest entries that fit the success budget
// and says what it left out; --since is unchanged.
func TestP3ChangelogFitsTheBudget(t *testing.T) {
	var b strings.Builder
	b.WriteString("# Changelog\n\n## [Unreleased]\n\n")
	const releases = 60
	for minor := releases; minor >= 1; minor-- {
		fmt.Fprintf(&b, "## [1.%d.0] - 2026-01-01\n### Added\n- %s\n\n", minor, strings.Repeat("A long description of the change. ", 8))
	}
	h := newHarness(t)
	h.build = func(u *upstream) *App {
		a := testApp(u)
		a.Version = fmt.Sprintf("1.%d.0", releases)
		a.Changelog = b.String()
		return a
	}
	r := h.run("changelog")
	d := r.data()
	entries, _ := d["entries"].([]any)
	omitted, _ := intOf(d["omitted"])
	if r.exit != 0 || d["truncated"] != true || omitted < 1 || len(entries) < 1 || int(omitted)+len(entries) != releases ||
		d["truncated_reason"] != "Only the newest entries fit the size budget; run changelog --since <version> for the changes after a version." {
		t.Fatalf("a long changelog must be cut with truncated, truncated_reason and omitted: %s", r)
	}
	if n, _ := intOf(d["count"]); int(n) != len(entries) || entries[0].(map[string]any)["version"] != fmt.Sprintf("1.%d.0", releases) {
		t.Fatalf("count and the newest entry first: %s", r)
	}
	if size := len([]rune(strings.TrimSpace(r.stdout))); size > successBudgetChars {
		t.Fatalf("the changelog is %d characters, over the budget", size)
	}
	if err := validateChangelog(d); err != nil {
		t.Fatalf("the cut changelog does not validate: %v", err)
	}
	// --since is unchanged: every newer entry, no truncation keys
	since := h.run("changelog", "--since", fmt.Sprintf("1.%d.0", releases-3))
	if list, _ := since.data()["entries"].([]any); since.exit != 0 || len(list) != 3 || hasKey(since.data(), "truncated") {
		t.Fatalf("--since: %s", since)
	}
	// a changelog that fits is not marked
	if small := newHarness(t).run("changelog"); small.exit != 0 || hasKey(small.data(), "truncated") || hasKey(small.data(), "omitted") {
		t.Fatalf("a short changelog: %s", small)
	}
	// the validator's rules for the new keys
	for _, c := range []struct {
		extra map[string]any
		want  string
	}{
		{map[string]any{"truncated": false}, "data.truncated: is true when present"},
		{map[string]any{"truncated": true, "omitted": json.Number("2")}, "data.truncated_reason: a truncated changelog says why (CLI-041)"},
		{map[string]any{"truncated": true, "truncated_reason": "Cut.", "omitted": json.Number("0")}, "data.omitted: counts the entries left out, a positive integer"},
		{map[string]any{"omitted": json.Number("2")}, "data: truncated_reason and omitted come with truncated: true"},
	} {
		data := map[string]any{"current_version": "1.0.0", "entries": []any{}, "count": json.Number("0")}
		for k, v := range c.extra {
			data[k] = v
		}
		if err := validateChangelog(data); err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%v: want %q, got %v", c.extra, c.want, err)
		}
	}
}

// ---------------------------------------------------------------- P4 replay at dry-run

// createRefusingDuplicates replaces note create with one whose Preview blocks a
// title that exists (as GitLab refuses a duplicate label name), counting Previews.
func createRefusingDuplicates(h *tharness, previews *int) {
	h.build = func(u *upstream) *App {
		a := testApp(u)
		c := a.commandByPath("note create")
		c.RunE = func(cmd *cobra.Command, _ []string) error {
			title, _ := cmd.Flags().GetString("title")
			return a.Write(cmd, WriteSpec{
				Target: Target{Key: "notes", Kind: "collection", Label: "notes"},
				Preview: func(context.Context) (Preview, error) {
					*previews++
					pv := Preview{Action: "Create a note", CanApply: true, Changes: []FieldChange{{Field: "title", After: title}}}
					u.mu.Lock()
					for _, n := range u.notes {
						if n.Title == title {
							pv.CanApply = false
							pv.BlockedBy = []Block{{Kind: "already_exists", Message: "A note with this title exists."}}
						}
					}
					u.mu.Unlock()
					return pv, nil
				},
				Apply: func(context.Context, Op) (Result, error) {
					var n tnote
					u.write(func() {
						id := strconv.Itoa(u.next)
						u.next++
						nn := &tnote{ID: id, Ref: "notes/" + id, Title: title, Tags: []string{}, Version: 1, UpdatedAt: "2026-10-12T00:00:00Z"}
						u.notes[id] = nn
						n = *nn
					})
					return Result{Data: n, Untrusted: []string{"title", "body"}}, nil
				},
			})
		}
		return a
	}
}

// P4: with --idempotency-key, a dry-run or a bare call looks the key up before
// Preview: an applied operation answers with its replay, even when Preview would
// block the duplicate, and neither Preview nor Apply runs.
func TestP4ReplayAtDryRun(t *testing.T) {
	previews := 0
	h := newHarness(t)
	createRefusingDuplicates(h, &previews)
	args := []string{"note", "create", "--title", "Weekly", "--idempotency-key", "w-1"}
	first := h.run(append(args, "--confirm", h.mustToken(args...))...)
	if first.exit != 0 {
		t.Fatalf("create: %s", first)
	}
	ref := first.data()["result"].(map[string]any)["ref"]
	previews = 0
	for _, extra := range [][]string{{"--dry-run"}, {}} { // a dry-run and a bare call
		r := h.run(append(append([]string{}, args...), extra...)...)
		receipt, _ := r.data()["receipt"].(map[string]any)
		res, _ := r.data()["result"].(map[string]any)
		if r.exit != 0 || r.env["ok"] != true || receipt["replayed"] != true || r.data()["operation_id"] != first.data()["operation_id"] ||
			res["ref"] != ref || hasKey(r.data(), "confirm_token") {
			t.Fatalf("%v: want the replay of the original: %s", extra, r)
		}
	}
	if previews != 0 || h.up.Writes() != 1 {
		t.Fatalf("the replay ran Preview %d time(s) and wrote %d time(s)", previews, h.up.Writes())
	}
	// Without the key the duplicate meets the Preview's block.
	if r := h.run("note", "create", "--title", "Weekly", "--dry-run"); previews != 1 || r.token() != "" ||
		r.data()["preview"].(map[string]any)["can_apply"] != false {
		t.Fatalf("without a key: %s", r)
	}
	// Another request under the key is the mismatch, before Preview too.
	previews = 0
	expectConflict(t, h.run("note", "create", "--title", "Other", "--idempotency-key", "w-1", "--dry-run"), "idempotency_mismatch")
	if previews != 0 {
		t.Fatal("the mismatch ran Preview")
	}

	// A batch's key is looked up the same way.
	h2 := newHarness(t)
	batchPreviews := 0
	h2.build = func(u *upstream) *App {
		a := testApp(u)
		c := a.commandByPath("note bulk-tag")
		c.Flags().String("idempotency-key", "", "Repeat-safe key")
		c.RunE = func(cmd *cobra.Command, _ []string) error {
			ids, _ := cmd.Flags().GetStringSlice("ids")
			var items []BatchItem
			for _, id := range ids {
				items = append(items, BatchItem{
					Target: Target{Key: "notes/" + id, Kind: "note"},
					Preview: func(context.Context) (Preview, error) {
						batchPreviews++
						return Preview{Action: "Tag", CanApply: true}, nil
					},
					Apply: func(context.Context, Op) (Result, error) {
						u.write(func() {})
						return Result{Data: map[string]any{"id": id}}, nil
					},
				})
			}
			return a.WriteBatch(cmd, BatchSpec{Items: items})
		}
		return a
	}
	bargs := []string{"note", "bulk-tag", "--ids", "1,2", "--tag", "x", "--idempotency-key", "b-1"}
	bfirst := h2.run(append(bargs, "--confirm", h2.mustToken(bargs...))...)
	if bfirst.exit != 0 {
		t.Fatalf("batch: %s", bfirst)
	}
	batchPreviews = 0
	again := h2.run(append(bargs, "--dry-run")...)
	items, _ := again.data()["items"].([]any)
	if again.exit != 0 || again.data()["operation_id"] != bfirst.data()["operation_id"] || len(items) != 2 || batchPreviews != 0 || h2.up.Writes() != 2 {
		t.Fatalf("batch replay at dry-run (previews %d): %s", batchPreviews, again)
	}
	for _, it := range items {
		if it.(map[string]any)["receipt"].(map[string]any)["replayed"] != true {
			t.Fatalf("replayed items: %s", again)
		}
	}
}

// ---------------------------------------------------------------- P5 settings writer

// P5: SetProfileSettings validates the profile name and the keys, keeps everything
// else in the file, writes it atomically with mode 0600, and never overwrites an
// invalid file.
func TestP5SetProfileSettings(t *testing.T) {
	h := settingsHarness(t)
	a := h.app()
	path := filepath.Join(h.state, "settings.json")
	readBack := func() map[string]any {
		t.Helper()
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		dec := json.NewDecoder(bytes.NewReader(raw))
		dec.UseNumber()
		var m map[string]any
		if err := dec.Decode(&m); err != nil {
			t.Fatal(err)
		}
		return m
	}
	// no file yet: the state directory and the file are created
	if e := a.SetProfileSettings("lab", map[string]string{"host": "https://lab.example"}); e != nil {
		t.Fatal(e)
	}
	mustJSON(t, "new file", readBack(), `{"profiles":{"lab":{"host":"https://lab.example"}}}`)
	if runtime.GOOS != "windows" {
		if info, err := os.Stat(path); err != nil || info.Mode().Perm() != 0o600 {
			t.Fatalf("the settings file must be 0600: %v %v", info.Mode(), err)
		}
		if info, err := os.Stat(h.state); err != nil || info.Mode().Perm() != 0o700 {
			t.Fatalf("the state directory must be 0700: %v %v", info.Mode(), err)
		}
	}
	// every other key, profile, number and default_profile is kept
	writeSettings(t, h, `{"default_profile": "work", "profiles": {"work": {"host": "https://w.example", "page_size": 50}}}`)
	if e := a.SetProfileSettings("lab", map[string]string{"page_size": "10"}); e != nil {
		t.Fatal(e)
	}
	if e := a.SetProfileSettings("work", map[string]string{"host": "https://w2.example"}); e != nil {
		t.Fatal(e)
	}
	mustJSON(t, "merged", readBack(),
		`{"default_profile":"work","profiles":{"lab":{"page_size":"10"},"work":{"host":"https://w2.example","page_size":50}}}`)
	if r := h.run("probe", "--profile", "lab"); r.data()["page_size"] != "10" || r.data()["page_size_source"] != "profile" {
		t.Fatalf("the written profile is read back: %s", r)
	}
	// an invalid file is E_CONFIG and stays as it is
	bad := `{"profiles": {"Bad Name": {}}}`
	writeSettings(t, h, bad)
	if e := a.SetProfileSettings("lab", map[string]string{"host": "https://x.example"}); e == nil || e.Code != "E_CONFIG" {
		t.Fatalf("an invalid file: %v", e)
	}
	if raw, _ := os.ReadFile(path); string(raw) != bad {
		t.Fatalf("an invalid file was overwritten: %s", raw)
	}
	_ = os.Remove(path)
	// the profile name and the keys are checked first
	e := a.SetProfileSettings("Lab!", map[string]string{"host": "https://x.example"})
	if e == nil || e.Code != "E_VALIDATION" || e.Message != "The profile name is not allowed; nothing was written." ||
		e.Details["parameter"] != "--profile" ||
		e.Details["next_action"] != "Give a profile name of a-z, 0-9, _ and -, up to 64 characters, starting with a letter or a digit." {
		t.Fatalf("an invalid profile name: %v", e)
	}
	if e := a.SetProfileSettings("lab", map[string]string{"nope": "x"}); e == nil || e.Code != "E_UNKNOWN" ||
		e.Message != `App.SetProfileSettings: "nope" is not declared in App.Settings.` {
		t.Fatalf("an undeclared key: %v", e)
	}
	if e := a.SetProfileSettings("lab", map[string]string{"api_token": "x"}); e == nil || e.Code != "E_UNKNOWN" ||
		e.Message != `App.SetProfileSettings: "api_token" is a secret setting; secrets go in the OS keyring, not the settings file (SEC-040).` {
		t.Fatalf("a secret key: %v", e)
	}
	if _, err := os.Stat(path); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("a refused call wrote the file")
	}
	// a file that cannot be written is E_IO
	dir := filepath.Join(h.dir, "no-such-dir")
	h.env["NOTES_SETTINGS_FILE"] = filepath.Join(dir, "settings.json")
	if e := a.SetProfileSettings("lab", map[string]string{"host": "https://x.example"}); e == nil || e.Code != "E_IO" ||
		e.Message != "The settings file cannot be written; nothing was changed." || e.Details["next_action"] != "Check that "+dir+" is writable." {
		t.Fatalf("an unwritable file: %v", e)
	}
	// a relative <PREFIX>_SETTINGS_FILE is E_CONFIG (C4)
	h.env["NOTES_SETTINGS_FILE"] = "settings.json"
	if e := a.SetProfileSettings("lab", map[string]string{"host": "https://x.example"}); e == nil || e.Code != "E_CONFIG" {
		t.Fatalf("a relative settings path: %v", e)
	}
}

// ---------------------------------------------------------------- P6 new profiles

// P6: a command declared with Meta.NewProfile runs with a requested profile the
// settings file does not have: Setting reads the flag, the environment and the
// default; App.Profile and the binding name the requested profile; its Apply can
// create it with SetProfileSettings. Other commands still refuse the profile.
func TestP6NewProfileCommands(t *testing.T) {
	h := settingsHarness(t)
	h.grant(map[string]any{"effects": []string{"change", "deliver", "configure"}})
	base := h.build
	h.build = func(u *upstream) *App {
		a := base(u)
		login := &cobra.Command{Use: "login", RunE: func(cmd *cobra.Command, _ []string) error {
			host, hostFrom, e := a.Setting(cmd, "host")
			if e != nil {
				return e
			}
			size, sizeFrom, e := a.Setting(cmd, "page_size")
			if e != nil {
				return e
			}
			profile, profileFrom := a.Profile()
			return a.Write(cmd, WriteSpec{
				Target: Target{Key: "profiles/" + profile, Kind: "profile"},
				Preview: func(context.Context) (Preview, error) {
					return Preview{Action: "Sign in", CanApply: true, Changes: []FieldChange{{Field: "host", After: host}}}, nil
				},
				Apply: func(context.Context, Op) (Result, error) {
					if e := a.SetProfileSettings(profile, map[string]string{"host": host}); e != nil {
						return Result{}, NotDispatched(e)
					}
					return Result{Data: map[string]any{"profile": profile, "profile_source": profileFrom, "host": host,
						"host_source": hostFrom, "page_size": size, "page_size_source": sizeFrom}}, nil
				},
			})
		}}
		login.Flags().String("host", "", "Notes service URL")
		a.Root.AddCommand(login)
		a.Schema("login_result", SchemaDef{Shape: "object",
			Fields: []string{"profile", "profile_source", "host", "host_source", "page_size", "page_size_source"}})
		a.Register(login, Meta{Summary: "Sign in and keep the host in a profile", Effects: []Effect{Configure}, Output: "login_result",
			NewProfile: true, Examples: []string{"notes login --profile lab --host https://lab.example --dangerous --dry-run"}})
		return a
	}
	writeSettings(t, h, `{"profiles": {"work": {"host": "https://w.example"}}}`)
	h.env["NOTES_PAGE_SIZE"] = "30"
	args := []string{"login", "--profile", "lab", "--host", "https://lab.example", "--dangerous"} // configure is dangerous (CLI-056)
	dry := h.run(append(args, "--dry-run")...)
	pv, _ := dry.data()["preview"].(map[string]any)
	inputs, _ := pv["inputs"].(map[string]any)
	if dry.exit != 0 || inputs["profile"] != "lab" || dry.token() == "" {
		t.Fatalf("a NewProfile command runs with the new profile: %s", dry)
	}
	// the token binds the requested profile
	expectMismatch(t, h.run("login", "--profile", "lab2", "--host", "https://lab.example", "--dangerous", "--confirm", dry.token()), "profile")
	r := h.run(append(args, "--confirm", dry.token())...)
	mustJSON(t, "login result", r.data()["result"], `{"profile":"lab","profile_source":"flag","host":"https://lab.example",
		"host_source":"flag","page_size":"30","page_size_source":"env"}`)
	// the profile now exists
	if p := h.run("probe", "--profile", "lab"); p.exit != 0 || p.data()["host"] != "https://lab.example" || p.data()["host_source"] != "profile" {
		t.Fatalf("the created profile: %s", p)
	}
	// other commands still refuse a profile the file does not have
	if p := h.run("probe", "--profile", "nowhere"); p.code() != "E_CONFIG" || p.message() != "The requested profile is not in the settings file." {
		t.Fatalf("probe with an unknown profile: %s", p)
	}
	// the environment selects the new profile the same way
	h.env["NOTES_PROFILE"] = "home"
	env := h.run("login", "--host", "https://home.example", "--dangerous", "--dry-run")
	if in, _ := env.data()["preview"].(map[string]any)["inputs"].(map[string]any); env.exit != 0 || in["profile"] != "home" {
		t.Fatalf("NOTES_PROFILE names the new profile: %s", env)
	}
}

// ---------------------------------------------------------------- P7 local writes in conformance

// P7: a command whose only effects are configure, persist or artifact may change only
// local things: G04 takes its receipt (change applied or persistence saved) as its
// one write. A confirm that applied nothing is still reported.
func TestP7LocalConfigureCommandsInConformance(t *testing.T) {
	var mu sync.Mutex
	var saved []string
	build := func(fail bool) func(*upstream) *App {
		return func(u *upstream) *App {
			a := testApp(u)
			login := &cobra.Command{Use: "login", RunE: func(cmd *cobra.Command, _ []string) error {
				host, _ := cmd.Flags().GetString("host")
				return a.Write(cmd, WriteSpec{
					Target: Target{Key: "profiles/default", Kind: "profile"},
					Preview: func(context.Context) (Preview, error) {
						return Preview{Action: "Keep the host", CanApply: true, Changes: []FieldChange{{Field: "host", After: host}}}, nil
					},
					Apply: func(context.Context, Op) (Result, error) {
						if fail {
							return Result{}, NotDispatched(errors.New("the keyring is locked"))
						}
						mu.Lock() // a local change: the fake upstream counts no write
						saved = append(saved, host)
						mu.Unlock()
						return Result{Data: map[string]any{"host": host}}, nil
					},
				})
			}}
			login.Flags().String("host", "", "Notes service URL")
			_ = login.MarkFlagRequired("host")
			a.Root.AddCommand(login)
			a.Schema("login_result", SchemaDef{Shape: "object", Fields: []string{"host"}})
			a.Register(login, Meta{Summary: "Keep the notes host", Effects: []Effect{Configure}, Output: "login_result",
				Examples: []string{"notes login --host https://lab.example --dangerous --dry-run",
					"notes login --host https://lab.example --dangerous --confirm <token>"}})
			return a
		}
	}
	harness := func(fail bool) Harness {
		h, _ := testHarness(build(fail))
		h.Samples["login"] = []string{"--host", "https://lab.example"}
		h.Scope["effects"] = []string{"change", "deliver", "configure", "artifact", "persist"}
		return h
	}
	RunConformance(t, harness(false))
	mu.Lock()
	n := len(saved)
	mu.Unlock()
	if n == 0 {
		t.Fatal("the suite never confirmed login")
	}
	rec := &recordTB{}
	RunConformance(rec, harness(true))
	if !rec.has("G04: login: the exact confirm sent no write, and its receipt shows no applied change or saved file") {
		t.Fatalf("G04 must still report a local write that applied nothing:\n%s", strings.Join(rec.errors, "\n"))
	}
}

// ---------------------------------------------------------------- P9 typed mutations

// P9: G03 changes a typed value within its type, so the tool's own validation lets
// it through to the gate.
func TestP9MutationsStayWithinTheType(t *testing.T) {
	c := &cobra.Command{Use: "x"}
	fs := c.Flags()
	fs.Int("count", 0, "")
	fs.Float64("ratio", 0, "")
	fs.Bool("draft", false, "")
	fs.Duration("timeout", 0, "")
	fs.String("state", "", "")
	fs.String("due-date", "", "")
	fs.String("at", "", "")
	fs.String("color", "", "")
	fs.String("title", "", "")
	fs.StringSlice("labels", nil, "")
	fs.IntSlice("ids", nil, "")
	m := &Meta{Params: map[string]ParamDoc{"state": {Enum: []string{"opened", "closed"}}}}
	for _, c := range []struct{ flag, in, want string }{
		{"count", "5", "6"},
		{"count", "-1", "0"},
		{"ratio", "1.5", "2.5"},
		{"draft", "true", "false"},
		{"draft", "false", "true"},
		{"timeout", "30s", "1m0s"},
		{"timeout", "0s", "1s"},
		{"state", "opened", "closed"},
		{"state", "closed", "opened"},
		{"due-date", "2026-12-31", "2027-01-01"},
		{"at", "2026-10-14T10:00:00Z", "2026-10-15T10:00:00Z"},
		{"color", "#ff0000", "#00ffff"},
		{"title", "Hello", "Hello-mutated"},
		{"labels", "a,b", "a,b,aicli-mutated"},
		{"ids", "1,2", "1,2,1002"},
	} {
		if got := mutateValue(fs.Lookup(c.flag), m, c.flag, c.in); got != c.want {
			t.Errorf("--%s %s: got %q, want %q", c.flag, c.in, got, c.want)
		}
	}
	// a positional argument has no flag: its shape decides
	for in, want := range map[string]string{"41": "42", "2026-02-28": "2026-03-01", "acme/web": "acme/web-mutated"} {
		if got := mutateValue(nil, m, "", in); got != want {
			t.Errorf("positional %s: got %q, want %q", in, got, want)
		}
	}
}

// ---------------------------------------------------------------- P10 transport

// P10: the CLI-110 transport and User-Agent are exported: TLS 1.2 or newer, the
// library's dial and handshake timeouts, the proxy from the environment, the system
// roots plus <PREFIX>_CA_FILE; an unusable CA file is E_CONFIG.
func TestP10HTTPTransportAndUserAgent(t *testing.T) {
	h := newHarness(t)
	a := h.app()
	tr, e := a.HTTPTransport()
	if e != nil {
		t.Fatal(e)
	}
	if tr.TLSClientConfig == nil || tr.TLSClientConfig.MinVersion != tls.VersionTLS12 || tr.TLSClientConfig.InsecureSkipVerify ||
		tr.TLSClientConfig.RootCAs != nil || tr.TLSHandshakeTimeout != 10*time.Second || tr.Proxy == nil || tr.DialContext == nil {
		t.Fatalf("transport: %+v", tr)
	}
	if ua := a.UserAgent(); ua != "notes/1.2.3 aicli/"+SpecVersion {
		t.Fatalf("User-Agent: %q", ua)
	}
	srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, r.UserAgent())
	}))
	srv.Config.ErrorLog = log.New(io.Discard, "", 0) // the refused handshake below is expected
	srv.StartTLS()
	defer srv.Close()
	get := func() (string, error) {
		tr, e := a.HTTPTransport()
		if e != nil {
			return "", e
		}
		req, _ := http.NewRequest(http.MethodGet, srv.URL, nil)
		req.Header.Set("User-Agent", a.UserAgent())
		resp, err := (&http.Client{Transport: tr, Timeout: 10 * time.Second}).Do(req)
		if err != nil {
			return "", err
		}
		defer func() { _ = resp.Body.Close() }()
		b, err := io.ReadAll(resp.Body)
		return string(b), err
	}
	if _, err := get(); err == nil {
		t.Fatal("a server whose CA is not trusted must be refused")
	}
	ca := filepath.Join(h.dir, "ca.pem")
	mustWrite(t, ca, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: srv.Certificate().Raw}))
	h.env["NOTES_CA_FILE"] = ca
	if body, err := get(); err != nil || body != a.UserAgent() {
		t.Fatalf("with NOTES_CA_FILE: %q %v", body, err)
	}
	bad := filepath.Join(h.dir, "bad.pem")
	mustWrite(t, bad, []byte("not a certificate"))
	h.env["NOTES_CA_FILE"] = bad
	_, e = a.HTTPTransport()
	if e == nil || e.Code != "E_CONFIG" || e.Message != "NOTES_CA_FILE names "+bad+", which is not a readable PEM certificate file." ||
		e.Details["next_action"] != "Point NOTES_CA_FILE at a PEM file of CA certificates, or unset it." || e.Details["dispatch_state"] != "not_started" {
		t.Fatalf("an unusable CA file: %v", e)
	}
}

// ---------------------------------------------------------------- P11 unknown verbs

// P11: after an unknown verb the corrected command line reads the remaining
// arguments as the suggested command does: a value of its flag is not a positional.
func TestP11UnknownVerbKeepsFlagValues(t *testing.T) {
	h := newHarness(t)
	r := h.run("note", "edit", "1", "--title", "Renamed")
	if r.code() != "E_USAGE" || r.details()["next_action"] != "Did you mean `notes note update`? Run `notes note update 1 --title Renamed`." {
		t.Fatalf("the corrected line: %s", r)
	}
	r = h.run("note", "edit", "1", "2", "--title", "Renamed")
	if r.details()["next_action"] != "Did you mean `notes note update`? It does not take those positional arguments; run `notes note update --help` for its flags." {
		t.Fatalf("extra positionals: %s", r)
	}
}

// ---------------------------------------------------------------- P12 --fields on writes

// P12: --fields on a confirmed write projects data.result with the command's output
// schema, as for a read; the receipt is never projected. Replays and batch items too.
func TestP12FieldsProjectTheWriteResult(t *testing.T) {
	h := newHarness(t)
	args := []string{"note", "update", "1", "--title", "Renamed"}
	r := h.run(append(args, "--confirm", h.mustToken(args...), "--fields", "title")...)
	res, _ := r.data()["result"].(map[string]any)
	receipt, _ := r.data()["receipt"].(map[string]any)
	if r.exit != 0 || len(res) != 2 || res["title"] != "Renamed" || res["ref"] != "notes/1" || receipt["outcome"] == nil || receipt["target"] == nil {
		t.Fatalf("--fields title on a confirm: %s", r)
	}
	// without --fields the whole result comes back
	args = []string{"note", "update", "2", "--title", "Whole"}
	if r := h.run(append(args, "--confirm", h.mustToken(args...))...); len(r.data()["result"].(map[string]any)) != len(noteFields()) {
		t.Fatalf("no --fields: %s", r)
	}
	// a replay's result is projected by the repeat's --fields
	create := []string{"note", "create", "--title", "Kept", "--idempotency-key", "p12"}
	if r := h.run(append(create, "--confirm", h.mustToken(create...))...); r.exit != 0 {
		t.Fatalf("create: %s", r)
	}
	again := h.run(append(create, "--dry-run", "--fields", "title")...)
	if res, _ := again.data()["result"].(map[string]any); len(res) != 2 || res["title"] != "Kept" || res["ref"] == nil ||
		again.data()["receipt"].(map[string]any)["replayed"] != true {
		t.Fatalf("a projected replay: %s", again)
	}
	// batch items
	bulk := []string{"note", "bulk-tag", "--ids", "1,2", "--tag", "work"}
	b := h.run(append(bulk, "--confirm", h.mustToken(bulk...), "--fields", "tags")...)
	items, _ := b.data()["items"].([]any)
	if b.exit != 0 || len(items) != 2 {
		t.Fatalf("batch: %s", b)
	}
	for _, it := range items {
		res, _ := it.(map[string]any)["result"].(map[string]any)
		if len(res) != 1 || res["tags"] == nil || it.(map[string]any)["receipt"] == nil {
			t.Fatalf("batch items are projected: %s", b)
		}
	}
}

// ---------------------------------------------------------------- P13 context --fields

// P13: context accepts as --fields every top-level key it returns, profile and the
// ContextExtra keys included.
func TestP13ContextFields(t *testing.T) {
	h := settingsHarness(t)
	base := h.build
	h.build = func(u *upstream) *App {
		a := base(u)
		a.ContextExtra = func(context.Context) map[string]any {
			return map[string]any{"git_remote": map[string]any{"host": "gitlab.example", "path": "acme/web"}}
		}
		return a
	}
	r := h.run("context", "--fields", "profile,git_remote")
	if r.exit != 0 || len(r.data()) != 2 || r.data()["profile"] == nil || r.data()["git_remote"] == nil {
		t.Fatalf("context --fields profile,git_remote: %s", r)
	}
	r = h.run("context", "--fields", "nope")
	expected := fmt.Sprint(r.details()["expected"])
	if r.code() != "E_USAGE" || !strings.Contains(expected, "profile") || !strings.Contains(expected, "git_remote") {
		t.Fatalf("an unknown context field: %s", r)
	}
}

// ---------------------------------------------------------------- P14 development builds

// P14: a development build (App.Version "dev" or empty) runs as the next patch after
// the newest changelog release with the prerelease dev, everywhere; update refuses it.
func TestP14DevelopmentBuilds(t *testing.T) {
	for _, version := range []string{"dev", ""} {
		h := newHarness(t)
		h.build = func(u *upstream) *App {
			a := testApp(u)
			a.Version = version
			return a
		}
		const want = "1.2.4-dev" // testChangelog's newest release is 1.2.3
		if r := h.run("--version"); r.exit != 0 || r.data()["version"] != want {
			t.Fatalf("%q: --version: %s", version, r)
		}
		ref := h.run("reference")
		if err := validateReferenceFull(ref.data()); ref.data()["version"] != want || err != nil {
			t.Fatalf("%q: reference: %v %s", version, err, ref)
		}
		cl := h.run("changelog")
		if err := validateChangelog(cl.data()); cl.data()["current_version"] != want || err != nil {
			t.Fatalf("%q: changelog: %v %s", version, err, cl)
		}
		if r := h.run("context"); r.data()["version"] != want {
			t.Fatalf("%q: context: %s", version, r)
		}
		args := []string{"note", "update", "1", "--title", "Dev"}
		if r := h.run(append(args, "--confirm", h.mustToken(args...))...); r.exit != 0 {
			t.Fatalf("%q: a write: %s", version, r)
		}
		if ua := h.app().UserAgent(); ua != "notes/"+want+" aicli/"+SpecVersion {
			t.Fatalf("%q: User-Agent %q", version, ua)
		}
	}
	for changelog, want := range map[string]string{
		"# Changelog\n\n## [Unreleased]\n### Added\n- Soon.\n": "0.0.0-dev",
		"## [1.2.17] - 2026-01-01\n### Fixed\n- A.\n":          "1.2.18-dev",
		"## [1.2.17]\n- A.\n## [1.10.0]\n- B.\n":               "1.10.1-dev",
	} {
		if got := devVersion(changelog); got != want {
			t.Errorf("devVersion(%q) = %q, want %q", changelog, got, want)
		}
	}
}

// P14: conformance passes on a development build without build flags.
func TestP14DevelopmentBuildPassesConformance(t *testing.T) {
	h, _ := testHarness(func(u *upstream) *App {
		a := testApp(u)
		a.Version = "dev"
		return a
	})
	RunConformance(t, h)
}

// P14: update on a development build is E_UNSUPPORTED at discover and contacts
// nothing; doctor does not compare the Skill with it.
func TestP14UpdateRefusesADevelopmentBuild(t *testing.T) {
	f := newUpdateFixture(t)
	f.method = "binary"
	f.addRelease("1.3.1", "### Fixed\n- Paging.", archiveFor(t, "new notes 1.3.1", "1.3.1", nil))
	build := f.h.build
	f.h.build = func(u *upstream) *App {
		a := build(u)
		a.Version = "dev"
		return a
	}
	for _, args := range [][]string{{"update"}, {"update", "--check"}} {
		r := f.h.run(args...)
		if r.code() != "E_UNSUPPORTED" || r.details()["stage"] != "discover" || r.details()["current_version"] != "1.2.4-dev" ||
			r.message() != "notes 1.2.4-dev is a development build; install a release to update." ||
			r.details()["next_action"] != "Install notes with the install command in its README." {
			t.Fatalf("%v: %s", args, r)
		}
	}
	if f.requestCount() != 0 || f.downloads() != 0 || f.readFile(f.exe) != "old notes 1.3.0" {
		t.Fatalf("a development build's update contacted the server (%d requests)", f.requestCount())
	}
	f.installSkill(f.roots[0], "1.2.3")
	d := f.h.run("doctor")
	if c := checkNamed(d, "version"); c["status"] != "warn" ||
		c["message"] != "notes 1.2.4-dev is a development build; the Skill at "+filepath.Join(f.roots[0], "notes", "SKILL.md")+" cannot be compared." {
		t.Fatalf("doctor on a development build: %v", c)
	}
}

// ---------------------------------------------------------------- P15 lock_held

// P15: lock_held names the running operation only when the lock's owner is known;
// unresolved_operation always names it.
func TestP15LockHeldWithoutAKnownOwner(t *testing.T) {
	envelope := func(conflict, extra string) []byte {
		return []byte(`{"ok":false,"schema_version":"2.0","error":{"code":"E_CONFLICT",` +
			`"message":"Another confirm on the same target is still running; nothing was sent.","retryable":false,` +
			`"details":{"conflict":"` + conflict + `","dispatch_state":"not_started","target":"notes/1",` +
			`"next_action":"Run the same command again in a few seconds; the confirm token stays valid until it expires."` + extra + `}},` +
			`"meta":{"duration_ms":1}}` + "\n")
	}
	if err := ValidateEnvelope(envelope("lock_held", "")); err != nil {
		t.Fatalf("lock_held without active_operation: %v", err)
	}
	if err := ValidateEnvelope(envelope("lock_held", `,"active_operation":"op_aaaaaaaaaaaaaaaaaaaaaaaaaa"`)); err != nil {
		t.Fatalf("lock_held with active_operation: %v", err)
	}
	if err := ValidateEnvelope(envelope("unresolved_operation", "")); err == nil || !strings.Contains(err.Error(), `missing key "active_operation"`) {
		t.Fatalf("unresolved_operation without active_operation: %v", err)
	}
	// the gate leaves the key out when the lock file names no operation
	if e := (&gateRun{}).lockHeld("notes/1", "not an operation id"); hasKey(e.Details, "active_operation") || e.Details["target"] != "notes/1" {
		t.Fatalf("lock_held from an unknown owner: %v", e.Details)
	}
}

// ---------------------------------------------------------------- P16 skill_sync_command

// P16: the validators enforce F10: skill_sync_command comes exactly when the Skill
// sync could not finish (skill_sync_status failed, or the E_PARTIAL at skill_sync).
func TestP16ValidatorsEnforceTheSyncCommandRule(t *testing.T) {
	updated := func(extra map[string]any) string {
		d := map[string]any{"status": "updated", "current_version": "1.3.1", "target_version": "1.3.1", "previous_version": "1.3.0",
			"update_available": false, "install_method": "npm", "signature_status": "not_checked", "signature_verified": false,
			"skill_sync_status": "synced", "binary_replaced": true}
		for k, v := range extra {
			d[k] = v
		}
		v := &validation{}
		validateUpdateData(v, "data", d)
		return strings.Join(v.problems, "; ")
	}
	if got := updated(nil); got != "" {
		t.Fatalf("a valid update: %s", got)
	}
	for _, ss := range []string{"synced", "not_needed", "skipped"} {
		want := "data.skill_sync_command: comes only when the Skill sync could not finish, not with skill_sync_status " + ss + " (F10)"
		if got := updated(map[string]any{"skill_sync_status": ss, "skill_sync_command": "notes update"}); !strings.Contains(got, want) {
			t.Errorf("%s with the command: %s", ss, got)
		}
	}
	failure := func(code, details string) error {
		return ValidateEnvelope([]byte(`{"ok":false,"schema_version":"2.0","error":{"code":"` + code + `","message":"The update failed.",` +
			`"retryable":false,"details":{` + details + `}},"meta":{"duration_ms":1}}` + "\n"))
	}
	partial := `"dispatch_state":"dispatched","next_action":"Run ` + "`notes update`" + ` again to finish the Skill sync.","status":"partial",` +
		`"stage":"skill_sync","current_version":"1.3.1","previous_version":"1.3.0","target_version":"1.3.1","binary_replaced":true,` +
		`"skill_sync_status":"failed","install_method":"binary","signature_status":"verified","update_available":false`
	if err := failure("E_PARTIAL", partial+`,"skill_sync_command":"notes update"`); err != nil {
		t.Fatalf("a partial update with the command: %v", err)
	}
	if err := failure("E_PARTIAL", partial); err == nil || !strings.Contains(err.Error(), `missing key "skill_sync_command": the Skill sync could not finish (F10)`) {
		t.Fatalf("a partial update without the command: %v", err)
	}
	before := `"dispatch_state":"not_started","next_action":"Check that the install directory is writable.","stage":"replace",` +
		`"current_version":"1.3.0","target_version":"1.3.1","binary_replaced":false,"skill_sync_status":"skipped","install_method":"binary"`
	if err := failure("E_IO", before); err != nil {
		t.Fatalf("a failure before the swap: %v", err)
	}
	if err := failure("E_IO", before+`,"skill_sync_command":"notes update"`); err == nil ||
		!strings.Contains(err.Error(), "error.details.skill_sync_command: comes only when the Skill sync could not finish, not with skill_sync_status skipped (F10)") {
		t.Fatalf("a failure before the swap with the command: %v", err)
	}
}

// ---------------------------------------------------------------- P17 redaction

// P17: a secret is also redacted in its path-escaped form that keeps "/" (Python's
// urllib.parse.quote), besides the raw, query-escaped and path-escaped forms.
func TestP17RedactionParity(t *testing.T) {
	const secret = "kr/SENTINEL 9?x"
	kept := strings.ReplaceAll(url.PathEscape(secret), "%2F", "/")
	h := newHarness(t)
	pingApp(h, func(a *App, cmd *cobra.Command) error {
		a.AddSecret(secret)
		a.Debugf("raw %s, query %s, path %s, kept %s", secret, url.QueryEscape(secret), url.PathEscape(secret), kept)
		return fmt.Errorf("GET https://h.example/files/%s failed", kept)
	})
	h.env["NOTES_DEBUG"] = "1"
	r := h.run("ping")
	if strings.Contains(r.stdout, "SENTINEL") || strings.Contains(r.stderr, "SENTINEL") ||
		!strings.Contains(r.stderr, "debug: raw ***, query ***, path ***, kept ***") {
		t.Fatalf("stdout %s\nstderr %s", r.stdout, r.stderr)
	}
}

// ---------------------------------------------------------------- P8 G03 and --profile

// P8: G03 never adds --profile: like the other global flags it is aicli's, bound as
// the active profile, and an unknown name stops the command before the gate.
func TestP8G03NeverAddsProfile(t *testing.T) {
	h, _ := testHarness(func(u *upstream) *App {
		a := testApp(u)
		a.Settings = []Setting{{Name: "host", Default: "https://notes.example"}}
		return a
	})
	rec := &recordTB{}
	RunConformance(rec, h)
	problems := append(append([]string{}, rec.errors...), rec.fatal...)
	for _, line := range append(append([]string{}, problems...), rec.logs...) {
		if strings.Contains(line, "--profile") {
			t.Fatalf("G03 used --profile: %s", line)
		}
	}
	if len(problems) > 0 {
		t.Fatalf("conformance with settings declared:\n%s", strings.Join(problems, "\n"))
	}
}
