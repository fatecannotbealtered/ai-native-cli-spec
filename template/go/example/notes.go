package main

import (
	"context"
	_ "embed"
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/fatecannotbealtered/ai-native-cli-spec/template/go/aicli"
	"github.com/spf13/cobra"
)

// The example "notes" tool: a minimal AI-native CLI on the aicli library. It shows
// how a tool declares its commands (Register, Schema), reads (OK, List), writes
// (Write, WriteBatch), file and secret inputs, and maps upstream errors. Everything
// else (envelope, errors, reference, help, the confirm gate, scope, journal,
// built-ins) comes from the library.

//go:embed CHANGELOG.md
var changelog string

// version is set at build time (-ldflags "-X main.version=...").
var version = "0.1.0"

// noteFields are the fields a note has in every output.
var noteFields = []string{"id", "ref", "title", "body", "tags", "version", "updated_at"}

// newApp builds the CLI around svc. env overrides process environment variables
// (the conformance suite passes the state directory and scope file this way).
func newApp(svc Service, env map[string]string) *aicli.App {
	getenv := func(k string) string {
		if v, ok := env[k]; ok {
			return v
		}
		return os.Getenv(k)
	}
	root := &cobra.Command{Use: "notes", Short: "Read and change notes; built for AI agents"}
	app := &aicli.App{
		Name: "notes", Version: version, RiskTier: "T2", EnvPrefix: "NOTES", Root: root, Changelog: changelog,
		Env: getenv,
		// update is a library built-in (CLI-070): it finds the release on GitHub, installs
		// it with npm or, for a standalone binary, verifies its Sigstore signature and
		// checksum before the swap, then syncs the installed Skills.
		Update: &aicli.UpdateConfig{Repo: "fatecannotbealtered/notes-cli-example", NPMPackage: "notes-cli-example"},
		// Settings (CLI-100, CLI-101): host comes from NOTES_HOST, then the active
		// profile of the settings file (<state dir>/settings.json), then the default.
		Settings: []aicli.Setting{{Name: "host", Default: "memory://notes", Description: "The notes service to use"}},
		// Accept the forms agents type first, or answer them precisely (CLI-022).
		// Lossless renames are accepted: FlagAliases here, verb aliases in Meta.Aliases.
		// FlagSynonyms only suggest, for guesses that need more than a rename (--all
		// has no value; --limit needs one). VerbSynonyms suggest verbs that are close
		// but not the same command (search is a list with a query).
		ShortFlags:   map[string]string{"t": "title"},
		FlagAliases:  map[string]string{"label": "tag", "labels": "tag", "tags": "tag", "text": "body", "message": "body"},
		FlagSynonyms: map[string][]string{"all": {"limit"}},
		VerbSynonyms: map[string][]string{"search": {"list"}, "find": {"list"}},
	}
	// The account and host come from local configuration only; the host is a setting,
	// so a profile can point the tool at another notes service.
	app.Identity = func(context.Context) (aicli.Identity, error) {
		source := "env"
		account := getenv("NOTES_ACCOUNT")
		if account == "" {
			account, source = "demo", "default"
		}
		host, _, err := app.Setting(nil, "host")
		if err != nil {
			return aicli.Identity{}, err
		}
		return aicli.Identity{Account: account, Host: host, Extra: map[string]string{"credentials_source": source}}, nil
	}

	app.Schema("note", aicli.SchemaDef{Shape: "object", Fields: noteFields, Untrusted: []string{"title", "body"}})
	app.Schema("note_page", aicli.SchemaDef{Shape: "page", Fields: noteFields,
		DefaultFields: []string{"ref", "title", "tags", "updated_at"}, Untrusted: []string{"title", "body"}})
	app.Schema("share_result", aicli.SchemaDef{Shape: "object", Fields: []string{"ref", "to", "shared_at"}})
	app.Schema("tag_result", aicli.SchemaDef{Shape: "object", Fields: []string{"ref", "tags"}})
	app.Schema("delete_result", aicli.SchemaDef{Shape: "object", Fields: []string{"ref", "deleted"}})

	note := &cobra.Command{Use: "note", Aliases: []string{"notes"}}
	app.Group(note, "Work with notes")
	root.AddCommand(note)

	addList(app, note, svc)
	addGet(app, note, svc)
	addCreate(app, note, svc)
	addUpdate(app, note, svc)
	addDelete(app, note, svc)
	addShare(app, note, svc, getenv)
	addBulkTag(app, note, svc)
	return app
}

// noteID accepts what an agent has: an id ("5"), a ref ("notes/5") or a URL
// ("https://notes.example/notes/5") (CLI-048).
func noteID(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.LastIndex(s, "/notes/"); i >= 0 {
		s = s[i+len("/notes/"):]
	}
	s = strings.TrimPrefix(s, "notes/")
	return strings.Trim(s, "/")
}

// resolveID reads the note id (positional or --id) in any form the agent has and
// binds its canonical form, so `note get notes/5` and `note get 5` are one request.
func resolveID(cmd *cobra.Command) (string, error) {
	raw, _ := cmd.Flags().GetString("id")
	id := noteID(raw)
	if err := validID(id); err != nil {
		return "", err
	}
	if err := aicli.SetResolved(cmd, "id", id); err != nil {
		return "", err
	}
	return id, nil
}

func validID(id string) error {
	if _, err := strconv.Atoi(id); err != nil || id == "" {
		e := aicli.Validation("--id", "a note id like 5, a ref like notes/5, or a note URL", "That is not a note id.")
		e.Details["next_action"] = "Run `notes note list` to find the note."
		return e
	}
	return nil
}

// readError maps a service error on a read by HTTP status, never by message (CLI-016).
func readError(err error, what string) error {
	var api *APIError
	if errors.As(err, &api) {
		e := aicli.FromHTTP(api.Status, what, api.Message, 0)
		if api.Status == 404 {
			e.Details["next_action"] = "Run `notes note list` to find the note."
		}
		return e
	}
	return err
}

// writeError classifies a failed write for the gate: a request that never left the
// client is NotDispatched, a definite 4xx refusal is Rejected, and anything else
// (5xx, timeouts, resets) stays as it is, so the gate reports an unknown outcome
// instead of a retryable error (CLI-015).
func writeError(err error, what string) error {
	var api *APIError
	if errors.As(err, &api) {
		switch {
		case api.Status == 0:
			return aicli.NotDispatched(aicli.FromHTTP(0, what, api.Message, 0))
		case api.Status >= 400 && api.Status < 500:
			return aicli.Rejected(aicli.FromHTTP(api.Status, what, api.Message, 0))
		}
	}
	return err
}

func addList(app *aicli.App, parent *cobra.Command, svc Service) {
	c := &cobra.Command{Use: "list", RunE: func(cmd *cobra.Command, _ []string) error {
		tag, _ := cmd.Flags().GetString("tag")
		limit, _ := cmd.Flags().GetInt("limit")
		offset, _ := cmd.Flags().GetInt("offset")
		if limit < 1 || limit > 100 {
			e := aicli.Usage("invalid_value", "--limit", "--limit must be between 1 and 100.")
			e.Details["expected"] = "1-100"
			return e
		}
		if offset < 0 {
			e := aicli.Usage("invalid_value", "--offset", "--offset must not be negative.")
			e.Details["expected"] = "0 or more, from next_offset"
			return e
		}
		all, err := svc.List(tag)
		if err != nil {
			return readError(err, "The notes could not be listed.")
		}
		end := min(offset+limit, len(all))
		start := min(offset, len(all))
		// scope names what the list covers and only the filters actually applied.
		scope := map[string]any{"covers": "every note this account can read"}
		if tag != "" {
			scope["tag"] = tag
		}
		total := len(all)
		page := aicli.Page{Items: all[start:end], HasMore: end < len(all), Sort: "id_asc", Scope: scope, Total: &total}
		if page.HasMore {
			next := end
			page.NextOffset = &next
		}
		return app.List(cmd, page)
	}}
	c.Flags().String("tag", "", "Only notes with this tag")
	c.Flags().Int("limit", 20, "Notes per page (1-100)")
	c.Flags().Int("offset", 0, "Skip this many notes (from next_offset)")
	parent.AddCommand(c)
	app.Register(c, aicli.Meta{Summary: "List notes", Effects: []aicli.Effect{aicli.Observe},
		Output: "note_page", Aliases: []string{"ls"}, Maturity: aicli.Experimental, Evidence: []string{evidence},
		Examples: []string{"notes note list --tag work", "notes note list --fields +body"}})
}

// evidence names the tests behind each command's maturity (VER-005): the example
// runs against a fake upstream only, so every command is experimental.
const evidence = "simulated:template/go/example/notes_test.go"

func addGet(app *aicli.App, parent *cobra.Command, svc Service) {
	c := &cobra.Command{Use: "get <id>", RunE: func(cmd *cobra.Command, _ []string) error {
		id, err := resolveID(cmd)
		if err != nil {
			return err
		}
		n, err := svc.Get(id)
		if err != nil {
			return readError(err, "No note has that id.")
		}
		return app.OK(cmd, n)
	}}
	c.Flags().String("id", "", "Note id, ref (notes/5) or URL")
	_ = c.MarkFlagRequired("id")
	parent.AddCommand(c)
	app.Register(c, aicli.Meta{Summary: "Show one note", Effects: []aicli.Effect{aicli.Observe}, Output: "note",
		Positional: "id", Aliases: []string{"view", "show"}, ErrorCodes: []string{"E_NOT_FOUND", "E_VALIDATION"},
		Maturity: aicli.Experimental, Evidence: []string{evidence},
		Examples: []string{"notes note get 1", "notes note get notes/1"}})
}

func addCreate(app *aicli.App, parent *cobra.Command, svc Service) {
	c := &cobra.Command{Use: "create", RunE: func(cmd *cobra.Command, _ []string) error {
		title, _ := cmd.Flags().GetString("title")
		body, _ := cmd.Flags().GetString("body")
		// File inputs are read through the library, so the token binds these bytes.
		if b, err := app.ReadFileInput(cmd, "body-file"); err != nil {
			return err
		} else if b != nil {
			body = string(b)
		}
		tags, _ := cmd.Flags().GetStringSlice("tag")
		if strings.TrimSpace(title) == "" {
			return aicli.Validation("--title", "a non-empty title", "The title is empty.")
		}
		return app.Write(cmd, aicli.WriteSpec{
			Target: aicli.Target{Key: "notes", Kind: "collection", Label: "all notes"},
			Preview: func(context.Context) (aicli.Preview, error) {
				return aicli.Preview{Action: "Create a note", CanApply: true, Changes: []aicli.FieldChange{
					{Field: "title", After: title}, {Field: "body", After: body}, {Field: "tags", After: tags}}}, nil
			},
			Apply: func(_ context.Context, op aicli.Op) (aicli.Result, error) {
				n, err := svc.Create(title, body, tags, op.IdempotencyKey)
				if err != nil {
					return aicli.Result{}, writeError(err, "The note was not created.")
				}
				// Report what the service now holds; the gate compares it with the preview.
				// The new note's ref and id are observed too, so operation show finds
				// them after a lost reply (CLI-059).
				return aicli.Result{Observed: map[string]any{"title": n.Title, "body": n.Body, "tags": n.Tags, "ref": n.Ref, "id": n.ID},
					Data: n, Untrusted: []string{"title", "body"}}, nil
			},
		})
	}}
	c.Flags().String("title", "", "Title")
	c.Flags().String("body", "", "Body text")
	c.Flags().String("body-file", "", "Read the body from a file (- for stdin)")
	c.Flags().StringSlice("tag", nil, "Tags (comma-separated or repeated)")
	c.Flags().String("idempotency-key", "", "Repeat-safe key: the same request with the same key returns the first result")
	_ = c.MarkFlagRequired("title")
	parent.AddCommand(c)
	app.Register(c, aicli.Meta{Summary: "Create a note", Effects: []aicli.Effect{aicli.Change}, Output: "note",
		FileInputs: map[string]string{"body-file": "body"}, Aliases: []string{"add", "new"}, ErrorCodes: []string{"E_VALIDATION"},
		Maturity: aicli.Experimental, Evidence: []string{evidence},
		Examples: []string{"notes note create --title Groceries --body-file list.txt --dry-run",
			"notes note create --title Groceries --body-file list.txt --confirm <token>"}})
}

func addUpdate(app *aicli.App, parent *cobra.Command, svc Service) {
	c := &cobra.Command{Use: "update <id>", RunE: func(cmd *cobra.Command, _ []string) error {
		id, err := resolveID(cmd)
		if err != nil {
			return err
		}
		var title, body *string
		if cmd.Flags().Changed("title") {
			v, _ := cmd.Flags().GetString("title")
			title = &v
		}
		if cmd.Flags().Changed("body") {
			v, _ := cmd.Flags().GetString("body")
			body = &v
		}
		if b, err := app.ReadFileInput(cmd, "body-file"); err != nil {
			return err
		} else if b != nil {
			v := string(b)
			body = &v
		}
		add, _ := cmd.Flags().GetStringSlice("add-tag")
		remove, _ := cmd.Flags().GetStringSlice("remove-tag")
		return app.Write(cmd, aicli.WriteSpec{
			Target:   aicli.Target{Key: "notes/" + id, Kind: "note"},
			ReadBack: "note get " + id,
			Preview: func(context.Context) (aicli.Preview, error) {
				n, err := svc.Get(id)
				if err != nil {
					return aicli.Preview{}, readError(err, "No note has that id.")
				}
				var changes []aicli.FieldChange
				if title != nil {
					changes = append(changes, aicli.FieldChange{Field: "title", Before: n.Title, After: *title})
				}
				if body != nil {
					changes = append(changes, aicli.FieldChange{Field: "body", Before: n.Body, After: *body})
				}
				if len(add) > 0 || len(remove) > 0 {
					changes = append(changes, aicli.FieldChange{Field: "tags", Before: n.Tags, Add: nilIfEmpty(add), Remove: nilIfEmpty(remove)})
				}
				pv := aicli.Preview{Action: "Update note " + id, Changes: changes, CanApply: true,
					BaseVersion: strconv.Itoa(n.Version), BaseSource: "version",
					Untrusted: []string{"changes[].before"}}
				if len(changes) == 0 {
					pv.BlockedBy = []aicli.Block{{Kind: "no_change", Message: "Give --title, --body, --body-file, --add-tag or --remove-tag."}}
				}
				return pv, nil
			},
			Apply: func(context.Context, aicli.Op) (aicli.Result, error) {
				n, err := svc.Update(id, title, body, add, remove)
				if err != nil {
					return aicli.Result{}, writeError(err, "The note was not updated.")
				}
				observed := map[string]any{"tags": n.Tags}
				if title != nil {
					observed["title"] = n.Title
				}
				if body != nil {
					observed["body"] = n.Body
				}
				return aicli.Result{Observed: observed, Data: n, Untrusted: []string{"title", "body"}}, nil
			},
		})
	}}
	c.Flags().String("id", "", "Note id, ref (notes/5) or URL")
	_ = c.MarkFlagRequired("id")
	c.Flags().String("title", "", "New title")
	c.Flags().String("body", "", "New body")
	c.Flags().String("body-file", "", "Read the new body from a file (- for stdin)")
	c.Flags().StringSlice("add-tag", nil, "Tags to add")
	c.Flags().StringSlice("remove-tag", nil, "Tags to remove")
	// An update with nothing to change is a usage error before the gate, not a
	// blocked preview (exit 5 would ask for a token that can never exist).
	c.MarkFlagsOneRequired("title", "body", "body-file", "add-tag", "remove-tag")
	parent.AddCommand(c)
	app.Register(c, aicli.Meta{Summary: "Change a note's title, body or tags", Effects: []aicli.Effect{aicli.Change},
		Output: "note", Positional: "id", FileInputs: map[string]string{"body-file": "body"}, Aliases: []string{"edit"},
		ErrorCodes: []string{"E_NOT_FOUND", "E_VALIDATION"}, Maturity: aicli.Experimental,
		Evidence: []string{evidence},
		Examples: []string{"notes note update 1 --title \"Weekly groceries\" --dry-run",
			"notes note update 1 --title \"Weekly groceries\" --confirm <token>"}})
}

func nilIfEmpty(list []string) any {
	if len(list) == 0 {
		return nil
	}
	return list
}

func addDelete(app *aicli.App, parent *cobra.Command, svc Service) {
	c := &cobra.Command{Use: "delete <id>", RunE: func(cmd *cobra.Command, _ []string) error {
		id, err := resolveID(cmd)
		if err != nil {
			return err
		}
		return app.Write(cmd, aicli.WriteSpec{
			Target:   aicli.Target{Key: "notes/" + id, Kind: "note"},
			ReadBack: "note get " + id,
			Preview: func(context.Context) (aicli.Preview, error) {
				n, err := svc.Get(id)
				if err != nil {
					return aicli.Preview{}, readError(err, "No note has that id.")
				}
				return aicli.Preview{Action: "Delete note " + id + " permanently", CanApply: true, BaseVersion: strconv.Itoa(n.Version),
					BaseSource: "version", Changes: []aicli.FieldChange{{Field: "note", Before: n.Title, Note: "cannot be undone"}},
					Untrusted: []string{"changes[].before"}}, nil
			},
			Apply: func(context.Context, aicli.Op) (aicli.Result, error) {
				if err := svc.Delete(id); err != nil {
					return aicli.Result{}, writeError(err, "The note was not deleted.")
				}
				// Read back: the note must be gone; observed says what the read showed.
				if _, err := svc.Get(id); err == nil {
					return aicli.Result{Outcome: aicli.Outcome{Verification: "failed"}, Observed: map[string]any{"exists": true},
						Mismatched: []string{"exists"}}, nil
				}
				return aicli.Result{Outcome: aicli.Outcome{Verification: "passed"}, Observed: map[string]any{"exists": false},
					Data: map[string]any{"ref": "notes/" + id, "deleted": true}}, nil
			},
		})
	}}
	c.Flags().String("id", "", "Note id, ref (notes/5) or URL")
	_ = c.MarkFlagRequired("id")
	parent.AddCommand(c)
	app.Register(c, aicli.Meta{Summary: "Delete a note permanently", Effects: []aicli.Effect{aicli.Change}, Dangerous: true,
		Output: "delete_result", Positional: "id", Aliases: []string{"remove", "rm"}, ErrorCodes: []string{"E_NOT_FOUND"},
		Maturity: aicli.Experimental, Evidence: []string{evidence},
		Examples: []string{"notes note delete 3 --dangerous --dry-run", "notes note delete 3 --dangerous --confirm <token>"}})
}

func addShare(app *aicli.App, parent *cobra.Command, svc Service, getenv func(string) string) {
	c := &cobra.Command{Use: "share <id>", RunE: func(cmd *cobra.Command, _ []string) error {
		id, err := resolveID(cmd)
		if err != nil {
			return err
		}
		to, _ := cmd.Flags().GetString("to")
		// The sharing token comes from NOTES_SHARE_TOKEN (SEC-040); --token is for
		// one-off use. An input resolved from the environment is bound like a flag
		// (CLI-054): SetResolved records it, and as a secret it is bound by a keyed
		// digest and shown as ***.
		token, _ := cmd.Flags().GetString("token")
		if !cmd.Flags().Changed("token") {
			if token = getenv("NOTES_SHARE_TOKEN"); token != "" {
				if err := aicli.SetResolved(cmd, "token", token); err != nil {
					return err
				}
			}
		}
		if !strings.Contains(to, "@") {
			return aicli.Validation("--to", "an email address", "The recipient is not an email address.")
		}
		return app.Write(cmd, aicli.WriteSpec{
			Target: aicli.Target{Key: "notes/" + id, Kind: "note"},
			Preview: func(context.Context) (aicli.Preview, error) {
				n, err := svc.Get(id)
				if err != nil {
					return aicli.Preview{}, readError(err, "No note has that id.")
				}
				// Destinations are checked against the scope's destinations (SEC-012).
				return aicli.Preview{Action: fmt.Sprintf("Email note %s to %s", id, to), CanApply: true, Destinations: []string{to},
					Changes:   []aicli.FieldChange{{Field: "recipients", Add: []string{to}, Note: "sends the title and body: " + n.Title}},
					Untrusted: []string{"changes[].note"}}, nil
			},
			Apply: func(context.Context, aicli.Op) (aicli.Result, error) {
				at, err := svc.Share(id, to, token)
				var api *APIError
				if errors.As(err, &api) && api.Status == 403 {
					// The upstream refused this token: say what it needs (CLI-014).
					e := aicli.FromHTTP(403, "The sharing service refused the token, so the note was not sent.", api.Message, 0)
					e.Details["required"] = "a sharing token with share permission"
					e.Details["next_action"] = "Ask the user for a valid sharing token (NOTES_SHARE_TOKEN); do not retry with other credentials."
					return aicli.Result{}, aicli.Rejected(e)
				}
				if err != nil {
					return aicli.Result{}, writeError(err, "The note was not sent.")
				}
				return aicli.Result{Data: map[string]any{"ref": "notes/" + id, "to": to, "shared_at": at.UTC().Format(time.RFC3339)}}, nil
			},
		})
	}}
	c.Flags().String("id", "", "Note id, ref (notes/5) or URL")
	_ = c.MarkFlagRequired("id")
	c.Flags().String("to", "", "Recipient email address")
	c.Flags().String("token", "", "Sharing-service token for one-off use; set NOTES_SHARE_TOKEN instead (never echoed)")
	_ = c.MarkFlagRequired("to")
	parent.AddCommand(c)
	app.Register(c, aicli.Meta{Summary: "Email a note to someone", Effects: []aicli.Effect{aicli.Deliver}, Output: "share_result",
		Positional: "id", Secrets: []string{"token"}, Aliases: []string{"send"}, ErrorCodes: []string{"E_NOT_FOUND", "E_VALIDATION"},
		Maturity: aicli.Experimental, Evidence: []string{evidence},
		Examples: []string{"notes note share 1 --to bob@example.com --dangerous --dry-run",
			"notes note share 1 --to bob@example.com --dangerous --confirm <token>"}})
}

func addBulkTag(app *aicli.App, parent *cobra.Command, svc Service) {
	c := &cobra.Command{Use: "bulk-tag", RunE: func(cmd *cobra.Command, _ []string) error {
		raws, _ := cmd.Flags().GetStringSlice("ids")
		tag, _ := cmd.Flags().GetString("tag")
		var ids []string
		for _, raw := range raws {
			id := noteID(raw)
			if err := validID(id); err != nil {
				return err
			}
			ids = append(ids, id)
		}
		if len(ids) > 0 {
			if err := aicli.SetResolved(cmd, "ids", ids...); err != nil {
				return err
			}
		}
		var items []aicli.BatchItem
		for _, id := range ids {
			items = append(items, aicli.BatchItem{
				Target: aicli.Target{Key: "notes/" + id, Kind: "note"},
				Preview: func(context.Context) (aicli.Preview, error) {
					n, err := svc.Get(id)
					if err != nil {
						return aicli.Preview{}, readError(err, "No note has that id.")
					}
					return aicli.Preview{Action: "Tag notes " + tag, CanApply: true, BaseVersion: strconv.Itoa(n.Version),
						BaseSource: "version", Changes: []aicli.FieldChange{{Field: "tags", Add: []string{tag}}}}, nil
				},
				Apply: func(context.Context, aicli.Op) (aicli.Result, error) {
					n, err := svc.AddTag(id, tag)
					if err != nil {
						return aicli.Result{}, writeError(err, "The tag was not added.")
					}
					return aicli.Result{Observed: map[string]any{"tags": n.Tags}, Data: map[string]any{"ref": n.Ref, "tags": n.Tags}}, nil
				},
			})
		}
		return app.WriteBatch(cmd, aicli.BatchSpec{Items: items})
	}}
	c.Flags().StringSlice("ids", nil, "Note ids or refs (comma-separated or repeated)")
	c.Flags().String("tag", "", "Tag to add")
	_ = c.MarkFlagRequired("tag")
	parent.AddCommand(c)
	app.Register(c, aicli.Meta{Summary: "Add one tag to several notes", Effects: []aicli.Effect{aicli.Change}, Output: "tag_result",
		BatchFlag: "ids", ErrorCodes: []string{"E_VALIDATION"}, Maturity: aicli.Experimental,
		Evidence: []string{evidence},
		Examples: []string{"notes note bulk-tag --ids 1,2 --tag urgent --dry-run", "notes note bulk-tag --ids 1,2 --tag urgent --confirm <token>"}})
}
