// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/spf13/cobra"
)

// The test tool: a small notes CLI over an in-memory upstream that counts the write
// requests it receives. Every library test drives it through App.Execute.

type tnote struct {
	ID        string   `json:"id"`
	Ref       string   `json:"ref"`
	Title     string   `json:"title"`
	Body      string   `json:"body"`
	Tags      []string `json:"tags"`
	Version   int      `json:"version"`
	UpdatedAt string   `json:"updated_at"`
}

type upstream struct {
	mu       sync.Mutex
	notes    map[string]*tnote
	next     int
	writes   int
	shared   []string
	comments map[string][]string
	// hook, when set, runs inside every Apply before the upstream changes; it can
	// return NotDispatched/Rejected/plain errors or panic.
	hook func(kind, id string) error
	// mangle, when set, changes titles as the upstream stores them (read-back mismatch).
	mangle func(string) string
}

type cobraCommand = cobra.Command

func newUpstream() *upstream {
	u := &upstream{}
	u.reset()
	return u
}

func (u *upstream) reset() {
	u.mu.Lock()
	defer u.mu.Unlock()
	u.notes = map[string]*tnote{}
	for i, title := range []string{"Groceries", "Ideas", "Travel"} {
		id := strconv.Itoa(i + 1)
		u.notes[id] = &tnote{ID: id, Ref: "notes/" + id, Title: title, Body: "body " + id, Tags: []string{"seed"}, Version: 1,
			UpdatedAt: "2026-10-01T00:00:00Z"}
	}
	u.next = 4
	u.writes = 0
	u.shared = nil
	u.comments = map[string][]string{}
}

func (u *upstream) Writes() int {
	u.mu.Lock()
	defer u.mu.Unlock()
	return u.writes
}

func (u *upstream) get(id string) (tnote, bool) {
	u.mu.Lock()
	defer u.mu.Unlock()
	n, ok := u.notes[id]
	if !ok {
		return tnote{}, false
	}
	c := *n
	c.Tags = append([]string{}, n.Tags...)
	return c, true
}

// bump changes a note behind the tool's back (state drift).
func (u *upstream) bump(id string) {
	u.mu.Lock()
	defer u.mu.Unlock()
	if n, ok := u.notes[id]; ok {
		n.Version++
		n.Title += " (edited elsewhere)"
	}
}

func (u *upstream) runHook(kind, id string) error {
	u.mu.Lock()
	hook := u.hook
	u.mu.Unlock()
	if hook != nil {
		return hook(kind, id)
	}
	return nil
}

func (u *upstream) write(fn func()) {
	u.mu.Lock()
	defer u.mu.Unlock()
	u.writes++
	fn()
}

const testChangelog = `# Changelog

## [Unreleased]
### Added
- Something not released yet.

## [1.2.3] - 2026-10-01
### Added
- Batch tagging.
### Fixed
- Share retries.

## [1.2.0] - 2026-09-01
### Changed
- Compact JSON by default.
  It is also documented.

## [1.0.0] - 2026-01-01
### Added
- First release.
`

func noteFields() []string {
	return []string{"id", "ref", "title", "body", "tags", "version", "updated_at"}
}

// testApp builds the test tool around u.
func testApp(u *upstream) *App {
	root := &cobra.Command{Use: "notes", Short: "A test notes tool"}
	a := &App{Name: "notes", Version: "1.2.3", RiskTier: "T2", EnvPrefix: "NOTES", Root: root, Changelog: testChangelog,
		MinSkillVersion: "1.0.0", SkillDirs: []string{},
		Shortcuts:    map[string][]string{"comment": {"note", "comment"}, "comments": {"note", "comment"}},
		ShortFlags:   map[string]string{"t": "title"},
		FlagAliases:  map[string]string{"subject": "title"},
		FlagSynonyms: map[string][]string{"all": {"limit"}, "text": {"body"}},
		VerbSynonyms: map[string][]string{"view": {"get"}, "show": {"get"}, "remove": {"delete"}},
		Identity: func(context.Context) (Identity, error) {
			return Identity{Account: "tester", Host: "https://notes.example", Extra: map[string]string{"credentials_source": "env"}}, nil
		},
	}
	a.Schema("note", SchemaDef{Shape: "object", Fields: noteFields(), Untrusted: []string{"title", "body"}})
	a.Schema("note_page", SchemaDef{Shape: "page", Fields: noteFields(), DefaultFields: []string{"ref", "title", "tags"},
		Untrusted: []string{"title", "body"}})
	a.Schema("note_write", SchemaDef{Shape: "object", Fields: noteFields(), Untrusted: []string{"title", "body"}})
	a.Schema("share_result", SchemaDef{Shape: "object", Fields: []string{"to"}})
	a.Schema("tag_result", SchemaDef{Shape: "object", Fields: []string{"id", "tags"}})
	a.Schema("comment_result", SchemaDef{Shape: "object", Fields: []string{"id", "text"}})

	noteCmd := &cobra.Command{Use: "note", Aliases: []string{"notes"}}
	a.Group(noteCmd, "Work with notes")
	root.AddCommand(noteCmd)

	list := &cobra.Command{Use: "list", RunE: func(cmd *cobra.Command, _ []string) error {
		limit, _ := cmd.Flags().GetInt("limit")
		tag, _ := cmd.Flags().GetString("tag")
		u.mu.Lock()
		var items []tnote
		for _, n := range u.notes {
			if tag == "" || containsStr(n.Tags, tag) {
				items = append(items, *n)
			}
		}
		u.mu.Unlock()
		sort.Slice(items, func(i, j int) bool { return items[i].ID < items[j].ID })
		p := Page{Items: items, Scope: map[string]any{"tag": tag}, Sort: "id_asc"}
		if len(items) > limit {
			p.Items = items[:limit]
			off := limit
			p.HasMore, p.NextOffset = true, &off
		}
		return a.List(cmd, p)
	}}
	list.Flags().Int("limit", 20, "Maximum notes to return")
	list.Flags().String("tag", "", "Only notes with this tag")
	noteCmd.AddCommand(list)
	a.Register(list, Meta{Summary: "List notes", Effects: []Effect{Observe}, Output: "note_page", Maturity: Experimental,
		Examples: []string{"notes note list --tag seed"}})

	get := &cobra.Command{Use: "get <id>", RunE: func(cmd *cobra.Command, _ []string) error {
		id, _ := cmd.Flags().GetString("id")
		n, ok := u.get(id)
		if !ok {
			return NotFound("No note has that id.", "Run `notes note list` to find the note.")
		}
		return a.OK(cmd, n)
	}}
	get.Flags().String("id", "", "Note id")
	noteCmd.AddCommand(get)
	a.Register(get, Meta{Summary: "Show one note", Effects: []Effect{Observe}, Output: "note", Positional: "id",
		ErrorCodes: []string{"E_NOT_FOUND"}, Examples: []string{"notes note get 1"}})

	create := &cobra.Command{Use: "create", RunE: func(cmd *cobra.Command, _ []string) error {
		title, _ := cmd.Flags().GetString("title")
		body, _ := cmd.Flags().GetString("body")
		if b, err := a.ReadFileInput(cmd, "body-file"); err != nil {
			return err
		} else if b != nil {
			body = string(b)
		}
		tags, _ := cmd.Flags().GetStringSlice("tag")
		return a.Write(cmd, WriteSpec{
			Target: Target{Key: "notes", Kind: "collection", Label: "notes"},
			Preview: func(context.Context) (Preview, error) {
				return Preview{Action: "Create a note", CanApply: true, Changes: []FieldChange{
					{Field: "title", After: title}, {Field: "body", After: body}, {Field: "tags", After: tags}}}, nil
			},
			Apply: func(context.Context, Op) (Result, error) {
				if err := u.runHook("create", ""); err != nil {
					return Result{}, err
				}
				var n tnote
				u.write(func() {
					id := strconv.Itoa(u.next)
					u.next++
					nn := &tnote{ID: id, Ref: "notes/" + id, Title: title, Body: body, Tags: append([]string{}, tags...), Version: 1,
						UpdatedAt: "2026-10-12T00:00:00Z"}
					u.notes[id] = nn
					n = *nn
				})
				return Result{Observed: map[string]any{"title": n.Title, "body": n.Body, "tags": n.Tags}, Data: n,
					Untrusted: []string{"title", "body"}}, nil
			},
		})
	}}
	create.Flags().String("title", "", "Title")
	create.Flags().String("body", "", "Body text")
	create.Flags().String("body-file", "", "Read the body from a file (- for stdin)")
	create.Flags().StringSlice("tag", nil, "Tags")
	create.Flags().String("idempotency-key", "", "Repeat-safe key")
	_ = create.MarkFlagRequired("title")
	noteCmd.AddCommand(create)
	a.Register(create, Meta{Summary: "Create a note", Effects: []Effect{Change}, Output: "note_write",
		FileInputs: map[string]string{"body-file": "body"},
		Examples:   []string{"notes note create --title Hello --dry-run", "notes note create --title Hello --confirm <token>"}})

	update := &cobra.Command{Use: "update <id>", RunE: func(cmd *cobra.Command, _ []string) error {
		id, _ := cmd.Flags().GetString("id")
		title, _ := cmd.Flags().GetString("title")
		titleSet := cmd.Flags().Changed("title")
		body, _ := cmd.Flags().GetString("body")
		bodySet := cmd.Flags().Changed("body")
		if b, err := a.ReadFileInput(cmd, "body-file"); err != nil {
			return err
		} else if b != nil {
			body, bodySet = string(b), true
		}
		return a.Write(cmd, WriteSpec{
			Target:   Target{Key: "notes/" + id, Kind: "note"},
			ReadBack: "note get " + id,
			Preview: func(context.Context) (Preview, error) {
				n, ok := u.get(id)
				if !ok {
					return Preview{}, NotFound("No note has that id.", "Run `notes note list` to find the note.")
				}
				var ch []FieldChange
				if titleSet {
					ch = append(ch, FieldChange{Field: "title", Before: n.Title, After: title})
				}
				if bodySet {
					ch = append(ch, FieldChange{Field: "body", Before: n.Body, After: body})
				}
				pv := Preview{Action: "Update note " + id, Changes: ch, BaseVersion: strconv.Itoa(n.Version), BaseSource: "version",
					CanApply: true, Untrusted: []string{"changes[].before"}}
				if len(ch) == 0 {
					pv.BlockedBy = []Block{{Kind: "no_change", Message: "Give --title or --body to change."}}
				}
				return pv, nil
			},
			Apply: func(context.Context, Op) (Result, error) {
				if err := u.runHook("update", id); err != nil {
					return Result{}, err
				}
				var n tnote
				u.write(func() {
					nn := u.notes[id]
					if titleSet {
						nn.Title = title
						if u.mangle != nil {
							nn.Title = u.mangle(title)
						}
					}
					if bodySet {
						nn.Body = body
					}
					nn.Version++
					n = *nn
				})
				obs := map[string]any{}
				if titleSet {
					obs["title"] = n.Title
				}
				if bodySet {
					obs["body"] = n.Body
				}
				return Result{Observed: obs, Data: n}, nil
			},
		})
	}}
	update.Flags().String("id", "", "Note id")
	update.Flags().String("title", "", "New title")
	update.Flags().String("body", "", "New body")
	update.Flags().String("body-file", "", "Read the new body from a file (- for stdin)")
	noteCmd.AddCommand(update)
	a.Register(update, Meta{Summary: "Change a note", Effects: []Effect{Change}, Output: "note_write", Positional: "id",
		FileInputs: map[string]string{"body-file": "body"}, ErrorCodes: []string{"E_NOT_FOUND"},
		Examples: []string{"notes note update 1 --title New --dry-run", "notes note update 1 --title New --confirm <token>"}})

	del := &cobra.Command{Use: "delete <id>", RunE: func(cmd *cobra.Command, _ []string) error {
		id, _ := cmd.Flags().GetString("id")
		return a.Write(cmd, WriteSpec{
			Target: Target{Key: "notes/" + id, Kind: "note"},
			Preview: func(context.Context) (Preview, error) {
				n, ok := u.get(id)
				if !ok {
					return Preview{}, NotFound("No note has that id.", "")
				}
				return Preview{Action: "Delete note " + id, CanApply: true, BaseVersion: strconv.Itoa(n.Version),
					Changes: []FieldChange{{Field: "note", Before: n.Title, Note: "deleted permanently"}}}, nil
			},
			Apply: func(context.Context, Op) (Result, error) {
				if err := u.runHook("delete", id); err != nil {
					return Result{}, err
				}
				missing := false
				u.write(func() {
					if _, ok := u.notes[id]; !ok {
						missing = true
						return
					}
					delete(u.notes, id)
				})
				if missing {
					return Result{}, Rejected(FromHTTP(404, "No note has that id.", "", 0))
				}
				return Result{}, nil
			},
		})
	}}
	del.Flags().String("id", "", "Note id")
	noteCmd.AddCommand(del)
	a.Register(del, Meta{Summary: "Delete a note", Effects: []Effect{Change}, Dangerous: true, Output: "note_write", Positional: "id",
		Aliases:  []string{"rm"},
		Examples: []string{"notes note delete 1 --dangerous --dry-run", "notes note delete 1 --dangerous --confirm <token>"}})

	share := &cobra.Command{Use: "share <id>", RunE: func(cmd *cobra.Command, _ []string) error {
		id, _ := cmd.Flags().GetString("id")
		to, _ := cmd.Flags().GetString("to")
		return a.Write(cmd, WriteSpec{
			Target: Target{Key: "notes/" + id, Kind: "note"},
			Preview: func(context.Context) (Preview, error) {
				if _, ok := u.get(id); !ok {
					return Preview{}, NotFound("No note has that id.", "")
				}
				return Preview{Action: "Send note " + id, CanApply: true, Destinations: []string{to},
					Changes: []FieldChange{{Field: "recipient", After: to}}}, nil
			},
			Apply: func(context.Context, Op) (Result, error) {
				if err := u.runHook("share", id); err != nil {
					return Result{}, err
				}
				u.write(func() { u.shared = append(u.shared, id+"->"+to) })
				return Result{Data: map[string]any{"to": to}}, nil
			},
		})
	}}
	share.Flags().String("id", "", "Note id")
	share.Flags().String("to", "", "Recipient address")
	share.Flags().String("token", "", "Access token for the sharing service")
	_ = share.MarkFlagRequired("to")
	noteCmd.AddCommand(share)
	a.Register(share, Meta{Summary: "Send a note to someone", Effects: []Effect{Deliver}, Output: "share_result", Positional: "id",
		Secrets: []string{"token"},
		Examples: []string{"notes note share 1 --to bob@example.com --dangerous --dry-run",
			"notes note share 1 --to bob@example.com --dangerous --confirm <token>"}})

	bulk := &cobra.Command{Use: "bulk-tag", RunE: func(cmd *cobra.Command, _ []string) error {
		ids, _ := cmd.Flags().GetStringSlice("ids")
		tag, _ := cmd.Flags().GetString("tag")
		var items []BatchItem
		for _, id := range ids {
			id := id
			items = append(items, BatchItem{
				Target: Target{Key: "notes/" + id, Kind: "note"},
				Preview: func(context.Context) (Preview, error) {
					n, ok := u.get(id)
					if !ok {
						return Preview{}, NotFound("No note has that id.", "")
					}
					return Preview{Action: "Tag notes", CanApply: true, BaseVersion: strconv.Itoa(n.Version),
						Changes: []FieldChange{{Field: "tags", Add: []string{tag}}}}, nil
				},
				Apply: func(context.Context, Op) (Result, error) {
					if err := u.runHook("tag", id); err != nil {
						return Result{}, err
					}
					var tags []string
					u.write(func() {
						n := u.notes[id]
						if !containsStr(n.Tags, tag) {
							n.Tags = append(n.Tags, tag)
						}
						n.Version++
						tags = append([]string{}, n.Tags...)
					})
					return Result{Observed: map[string]any{"tags": tags}, Data: map[string]any{"id": id, "tags": tags}}, nil
				},
			})
		}
		return a.WriteBatch(cmd, BatchSpec{Items: items})
	}}
	bulk.Flags().StringSlice("ids", nil, "Note ids (comma-separated or repeated)")
	bulk.Flags().String("tag", "", "Tag to add")
	_ = bulk.MarkFlagRequired("tag")
	noteCmd.AddCommand(bulk)
	a.Register(bulk, Meta{Summary: "Add a tag to several notes", Effects: []Effect{Change}, Output: "tag_result", BatchFlag: "ids",
		Examples: []string{"notes note bulk-tag --ids 1,2 --tag work --dry-run", "notes note bulk-tag --ids 1,2 --tag work --confirm <token>"}})

	// note tail streams the notes as NDJSON (CLI-006); --fail-after ends the stream
	// with an error after that many items.
	tail := &cobra.Command{Use: "tail", RunE: func(cmd *cobra.Command, _ []string) error {
		failAfter, _ := cmd.Flags().GetInt("fail-after")
		s, err := a.Stream(cmd)
		if err != nil {
			return err
		}
		u.mu.Lock()
		var items []tnote
		for _, n := range u.notes {
			items = append(items, *n)
		}
		u.mu.Unlock()
		sort.Slice(items, func(i, j int) bool { return items[i].ID < items[j].ID })
		for i, n := range items {
			if failAfter > 0 && i == failAfter {
				return FromHTTP(503, "The notes service stopped answering.", "", 0)
			}
			if err := s.Item(n); err != nil {
				return err
			}
		}
		s.Summary(map[string]any{"count": s.Count(), "has_more": false})
		return nil
	}}
	tail.Flags().Int("fail-after", 0, "End with an error after this many notes (tests)")
	noteCmd.AddCommand(tail)
	a.Register(tail, Meta{Summary: "Stream every note as NDJSON, one line per note", Effects: []Effect{Observe}, Output: "note",
		Examples: []string{"notes note tail"}})

	// note export writes every note to a local file through the gate (CLI-042).
	export := &cobra.Command{Use: "export", RunE: func(cmd *cobra.Command, _ []string) error {
		out, _ := cmd.Flags().GetString("output")
		return a.WriteOutput(cmd, OutputSpec{Path: out, Produce: func(context.Context) ([]byte, error) {
			if err := u.runHook("export", ""); err != nil {
				return nil, err
			}
			u.mu.Lock()
			defer u.mu.Unlock()
			var ids []string
			for id := range u.notes {
				ids = append(ids, id)
			}
			sort.Strings(ids)
			var b strings.Builder
			for _, id := range ids {
				fmt.Fprintf(&b, "%s\t%s\n", id, u.notes[id].Title)
			}
			return []byte(b.String()), nil
		}, Summary: map[string]any{"format": "tsv"}})
	}}
	export.Flags().String("output", "", "File to write")
	_ = export.MarkFlagRequired("output")
	noteCmd.AddCommand(export)
	a.Schema("export_result", SchemaDef{Shape: "object", Fields: []string{"path", "bytes", "sha256", "format"}})
	a.Register(export, Meta{Summary: "Write every note to a local file", Effects: []Effect{Artifact, Persist}, Output: "export_result",
		FileInputs: nil, Examples: []string{"notes note export --output notes.tsv --dry-run", "notes note export --output notes.tsv --confirm <token>"}})

	comment := &cobra.Command{Use: "comment"}
	a.Group(comment, "Comments on notes")
	noteCmd.AddCommand(comment)
	add := &cobra.Command{Use: "add <id>", RunE: func(cmd *cobra.Command, _ []string) error {
		id, _ := cmd.Flags().GetString("id")
		text, _ := cmd.Flags().GetString("text")
		return a.Write(cmd, WriteSpec{
			Target: Target{Key: "notes/" + id + "#comments", Kind: "comments"},
			Preview: func(context.Context) (Preview, error) {
				if _, ok := u.get(id); !ok {
					return Preview{}, NotFound("No note has that id.", "")
				}
				return Preview{Action: "Comment on note " + id, CanApply: true, Changes: []FieldChange{{Field: "comments", Add: text}}}, nil
			},
			Apply: func(context.Context, Op) (Result, error) {
				u.write(func() { u.comments[id] = append(u.comments[id], text) })
				return Result{Data: map[string]any{"id": id, "text": text}, Untrusted: []string{"text"}}, nil
			},
		})
	}}
	add.Flags().String("id", "", "Note id")
	add.Flags().String("text", "", "Comment text")
	_ = add.MarkFlagRequired("text")
	comment.AddCommand(add)
	a.Register(add, Meta{Summary: "Comment on a note", Effects: []Effect{Change}, Output: "comment_result", Positional: "id",
		Aliases:  []string{"create"},
		Examples: []string{"notes note comment add 1 --text Hi --dry-run", "notes note comment add 1 --text Hi --confirm <token>"}})
	return a
}

func containsStr(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------- harness

type tharness struct {
	t       *testing.T
	up      *upstream
	dir     string
	state   string
	scope   string
	scopeOn bool
	now     func() time.Time
	stdin   string
	env     map[string]string
	build   func(*upstream) *App
}

func newHarness(t *testing.T) *tharness {
	t.Helper()
	dir := t.TempDir()
	h := &tharness{t: t, up: newUpstream(), dir: dir, state: filepath.Join(dir, "state"), scope: filepath.Join(dir, "scope.json"),
		scopeOn: true, env: map[string]string{}, build: testApp}
	h.grant(nil)
	return h
}

func defaultScope() map[string]any {
	now := time.Now().UTC()
	return map[string]any{
		"scope_id": "scp_test", "tool": "notes", "granted_by": "tester", "granted_at": timeString(now.Add(-time.Hour)),
		"expires_at": timeString(now.Add(time.Hour)), "effects": []string{"change", "deliver"}, "dangerous": true,
		"destinations": []string{"bob@example.com", "https://hooks.example.com/notes"},
	}
}

// grant writes the scope file: the default scope with change applied (nil values delete keys).
func (h *tharness) grant(change map[string]any) {
	h.t.Helper()
	obj := defaultScope()
	for k, v := range change {
		if v == nil {
			delete(obj, k)
		} else {
			obj[k] = v
		}
	}
	b, _ := json.MarshalIndent(obj, "", "  ")
	if err := os.WriteFile(h.scope, b, 0o600); err != nil {
		h.t.Fatal(err)
	}
}

type tout struct {
	exit   int
	stdout string
	stderr string
	env    map[string]any
}

func (h *tharness) app() *App {
	a := h.build(h.up)
	a.StateDir = h.state
	scope := ""
	if h.scopeOn {
		scope = h.scope
	}
	a.Env = func(k string) string {
		if k == "NOTES_SCOPE_FILE" {
			return scope
		}
		return h.env[k]
	}
	if h.now != nil {
		a.Now = h.now
	}
	return a
}

func (h *tharness) run(args ...string) tout {
	h.t.Helper()
	a := h.app()
	var o, e bytes.Buffer
	a.Stdout, a.Stderr = &o, &e
	a.Stdin = strings.NewReader(h.stdin)
	exit := a.Execute(args)
	r := tout{exit: exit, stdout: o.String(), stderr: e.String()}
	dumpOutput(args, o.Bytes())
	if t := strings.TrimSpace(r.stdout); strings.HasPrefix(t, "{") {
		dec := json.NewDecoder(strings.NewReader(r.stdout))
		dec.UseNumber()
		var m map[string]any
		if err := dec.Decode(&m); err == nil {
			r.env = m
			validate := ValidateEnvelope
			if isStreamOutput(o.Bytes()) {
				validate = ValidateStream // NDJSON (CLI-006)
			}
			if verr := validate(o.Bytes()); verr != nil {
				h.t.Errorf("output of %q does not validate: %v\n%s", strings.Join(args, " "), verr, r.stdout)
			}
		}
	}
	return r
}

func (r tout) code() string {
	e, _ := r.env["error"].(map[string]any)
	c, _ := e["code"].(string)
	return c
}

var dumpSeq struct {
	sync.Mutex
	n int
}

// dumpOutput writes each JSON output to $AICLI_TEST_DUMP (when set), so the
// contract's reference checker can validate everything the tests produce:
// check-contract.py --schema envelope <dir>/*.json, and --lines for *.ndjson.
func dumpOutput(args []string, out []byte) {
	dir := os.Getenv("AICLI_TEST_DUMP")
	if dir == "" || !bytes.HasPrefix(bytes.TrimSpace(out), []byte("{")) {
		return
	}
	dumpSeq.Lock()
	dumpSeq.n++
	n := dumpSeq.n
	dumpSeq.Unlock()
	ext := ".json"
	if isStreamOutput(out) {
		ext = ".ndjson"
	}
	name := filepath.Join(dir, fmt.Sprintf("%05d%s", n, ext))
	_ = os.WriteFile(name, out, 0o600)
	_ = os.WriteFile(name+".cmd", []byte(strings.Join(args, " ")), 0o600)
}

func (r tout) message() string {
	e, _ := r.env["error"].(map[string]any)
	m, _ := e["message"].(string)
	return m
}

func (r tout) details() map[string]any {
	e, _ := r.env["error"].(map[string]any)
	d, _ := e["details"].(map[string]any)
	if d == nil {
		return map[string]any{}
	}
	return d
}

func (r tout) data() map[string]any {
	d, _ := r.env["data"].(map[string]any)
	if d == nil {
		return map[string]any{}
	}
	return d
}

func (r tout) token() string {
	if t, ok := r.data()["confirm_token"].(string); ok {
		return t
	}
	t, _ := r.details()["confirm_token"].(string)
	return t
}

func (r tout) String() string {
	return fmt.Sprintf("exit=%d stdout=%s stderr=%s", r.exit, r.stdout, r.stderr)
}

// mustToken runs a dry-run and returns its token.
func (h *tharness) mustToken(args ...string) string {
	h.t.Helper()
	r := h.run(append(args, "--dry-run")...)
	tok := r.token()
	if r.exit != 0 || tok == "" {
		h.t.Fatalf("dry-run %v failed: %s", args, r)
	}
	return tok
}

func expectConflict(t *testing.T, r tout, conflict string) {
	t.Helper()
	if r.exit != 6 || r.code() != "E_CONFLICT" || r.details()["conflict"] != conflict {
		t.Fatalf("want E_CONFLICT %s, got %s", conflict, r)
	}
}

func expectMismatch(t *testing.T, r tout, param string) {
	t.Helper()
	expectConflict(t, r, "token_mismatch")
	if got := r.details()["parameter"]; got != param {
		t.Fatalf("token_mismatch should name %q, named %v", param, got)
	}
}
