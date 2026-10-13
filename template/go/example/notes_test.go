package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/fatecannotbealtered/ai-native-cli-spec/template/go/aicli"
)

// coverageFile records every command the tests below execute (VER-003). TestMain
// checks it after a full run with aicli.CheckCoverage (R06).
var coverageFile string

// releaseAPI stands in for the GitHub API of the built-in update: doctor refreshes
// the update notice from it, and no test touches the network.
var releaseAPI = httptest.NewServer(http.NotFoundHandler())

// testApp is newApp with the release source pointed at releaseAPI.
func testApp(store Service, env map[string]string) *aicli.App {
	app := newApp(store, env)
	app.Update.APIBase = releaseAPI.URL
	return app
}

func TestMain(m *testing.M) {
	dir, err := os.MkdirTemp("", "notes-coverage-")
	if err != nil {
		panic(err)
	}
	coverageFile = filepath.Join(dir, "coverage.jsonl")
	_ = os.Setenv("NOTES_COVERAGE_FILE", coverageFile)
	code := m.Run()
	if code == 0 && fullRun() {
		tb := &mainTB{}
		h := harness(newMemStore())
		h.CoverageFile = coverageFile
		aicli.CheckCoverage(tb, h)
		if tb.failed {
			code = 1
		}
	}
	releaseAPI.Close()
	_ = os.RemoveAll(dir)
	os.Exit(code)
}

// fullRun is false when -run or -skip selects a subset of the tests.
func fullRun() bool {
	for _, name := range []string{"test.run", "test.skip"} {
		if f := flag.Lookup(name); f != nil && f.Value.String() != "" {
			return false
		}
	}
	return true
}

// mainTB reports conformance failures from TestMain.
type mainTB struct{ failed bool }

func (m *mainTB) Helper() {}
func (m *mainTB) Errorf(format string, args ...any) {
	m.failed = true
	fmt.Fprintf(os.Stderr, "FAIL: "+format+"\n", args...)
}
func (m *mainTB) Fatalf(format string, args ...any) { m.Errorf(format, args...) }
func (m *mainTB) Logf(string, ...any)               {}

// harness connects the conformance suite to the notes tool and its fake service.
func harness(store *memStore) aicli.Harness {
	return aicli.Harness{
		NewApp: func(env map[string]string) *aicli.App { return testApp(store, env) },
		Writes: store.Writes,
		Reset:  store.Reset,
		Samples: map[string][]string{
			"note create":   {"--title", "Groceries", "--body", "Milk", "--tag", "home"},
			"note update":   {"1", "--title", "Weekly groceries"},
			"note delete":   {"3"},
			"note share":    {"1", "--to", "bob@example.com", "--token", "share-token-123"},
			"note bulk-tag": {"--ids", "1,2", "--tag", "urgent"},
		},
		Scope: map[string]any{
			"scope_id": "scp_notes_tests", "tool": "notes", "granted_by": "notes tests", "granted_at": "2026-10-12T00:00:00Z",
			"expires_at": nil, "effects": []string{"change", "deliver"}, "dangerous": true,
			"targets": []any{map[string]any{"key": "notes"}}, "destinations": []string{"bob@example.com"},
		},
	}
}

func TestConformance(t *testing.T) {
	aicli.RunConformance(t, harness(newMemStore()))
}

// ---------------------------------------------------------------- focused tests

type env struct {
	t     *testing.T
	store *memStore
	dir   string
	vars  map[string]string
	stdin string
}

// newEnv gives a test its own service, state directory and scope.
func newEnv(t *testing.T, scope map[string]any) *env {
	t.Helper()
	dir := t.TempDir()
	e := &env{t: t, store: newMemStore(), dir: dir, vars: map[string]string{
		"NOTES_STATE_DIR": filepath.Join(dir, "state"),
		"NOTES_ACCOUNT":   "tester",
		// the settings' environment stays out of the tests
		"NOTES_HOST": "", "NOTES_PROFILE": "", "NOTES_SETTINGS_FILE": "",
	}}
	if scope != nil {
		b, _ := json.Marshal(scope)
		path := filepath.Join(dir, "scope.json")
		if err := os.WriteFile(path, b, 0o600); err != nil {
			t.Fatal(err)
		}
		e.vars["NOTES_SCOPE_FILE"] = path
	} else {
		e.vars["NOTES_SCOPE_FILE"] = ""
	}
	return e
}

func fullScope() map[string]any {
	return map[string]any{"scope_id": "scp_test", "tool": "notes", "granted_by": "tester", "granted_at": "2026-10-12T00:00:00Z",
		"expires_at": nil, "effects": []string{"change", "deliver"}, "dangerous": true, "destinations": []string{"bob@example.com"}}
}

type result struct {
	exit   int
	out    string
	errOut string
	doc    map[string]any
}

func (e *env) run(args ...string) result {
	e.t.Helper()
	app := testApp(e.store, e.vars)
	var out, errb bytes.Buffer
	app.Stdout, app.Stderr = &out, &errb
	app.Stdin = strings.NewReader(e.stdin)
	r := result{exit: app.Execute(args), out: out.String(), errOut: errb.String()}
	if strings.HasPrefix(strings.TrimSpace(r.out), "{") {
		dec := json.NewDecoder(strings.NewReader(r.out))
		dec.UseNumber()
		_ = dec.Decode(&r.doc)
		if err := aicli.ValidateEnvelope(out.Bytes()); err != nil {
			e.t.Errorf("%v: invalid envelope: %v\n%s", args, err, r.out)
		}
	}
	return r
}

func (r result) data() map[string]any {
	d, _ := r.doc["data"].(map[string]any)
	return d
}

func (r result) details() map[string]any {
	er, _ := r.doc["error"].(map[string]any)
	d, _ := er["details"].(map[string]any)
	if d == nil {
		return map[string]any{}
	}
	return d
}

func (r result) code() string {
	er, _ := r.doc["error"].(map[string]any)
	c, _ := er["code"].(string)
	return c
}

func (r result) token() string {
	if t, ok := r.data()["confirm_token"].(string); ok {
		return t
	}
	t, _ := r.details()["confirm_token"].(string)
	return t
}

func (e *env) dryRun(args ...string) string {
	e.t.Helper()
	r := e.run(append(args, "--dry-run")...)
	if r.exit != 0 || r.token() == "" {
		e.t.Fatalf("dry-run %v: exit %d %s", args, r.exit, r.out)
	}
	return r.token()
}

func TestListPagesAndDefaultFields(t *testing.T) {
	e := newEnv(t, nil)
	r := e.run("note", "list", "--limit", "2")
	if r.exit != 0 || r.data()["has_more"] != true || r.data()["next_offset"] == nil {
		t.Fatalf("first page: %s", r.out)
	}
	items := r.data()["items"].([]any)
	if first := items[0].(map[string]any); first["body"] != nil || first["ref"] != "notes/1" {
		t.Fatalf("default fields: %v", first)
	}
	r = e.run("note", "list", "--offset", "2", "--fields", "+body")
	if r.data()["has_more"] != false || r.data()["items"].([]any)[0].(map[string]any)["body"] == nil {
		t.Fatalf("second page with body: %s", r.out)
	}
	r = e.run("note", "list", "--tag", "home")
	if r.data()["scope"].(map[string]any)["tag"] != "home" || len(r.data()["items"].([]any)) != 2 {
		t.Fatalf("tag filter: %s", r.out)
	}
}

func TestGetAcceptsWhatTheAgentHas(t *testing.T) {
	e := newEnv(t, nil)
	for _, args := range [][]string{{"note", "get", "2"}, {"note", "get", "notes/2"}, {"note", "get", "https://notes.example/notes/2"},
		{"note", "get", "--id", "2"}, {"note", "view", "2"}, {"notes", "show", "2"}} {
		r := e.run(args...)
		if r.exit != 0 || r.data()["title"] != "Release plan" {
			t.Fatalf("%v: %s", args, r.out)
		}
	}
	r := e.run("note", "get", "99")
	if r.exit != 3 || r.code() != "E_NOT_FOUND" {
		t.Fatalf("missing note: %s", r.out)
	}
	r = e.run("note", "get", "abc")
	if r.exit != 2 || r.code() != "E_VALIDATION" {
		t.Fatalf("bad id: %s", r.out)
	}
	r = e.run("note", "get")
	if r.exit != 2 || r.details()["problem"] != "missing_parameter" || r.details()["parameter"] != "--id" {
		t.Fatalf("missing id: %s", r.out)
	}
}

func TestCreateBindsTheBodyContent(t *testing.T) {
	e := newEnv(t, fullScope())
	file := filepath.Join(e.dir, "body.txt")
	_ = os.WriteFile(file, []byte("Milk and eggs"), 0o600)
	tok := e.dryRun("note", "create", "--title", "Shopping", "--body", "Milk and eggs")
	r := e.run("note", "create", "--title", "Shopping", "--body-file", file, "--confirm", tok)
	if r.exit != 0 || e.store.Writes() != 1 {
		t.Fatalf("--body-file with the previewed content must confirm: %s", r.out)
	}
	tok = e.dryRun("note", "create", "--title", "Shopping 2", "--body-file", file)
	_ = os.WriteFile(file, []byte("Milk, eggs and a surprise"), 0o600)
	r = e.run("note", "create", "--title", "Shopping 2", "--body-file", file, "--confirm", tok)
	if r.code() != "E_CONFLICT" || r.details()["parameter"] != "--body" || e.store.Writes() != 1 {
		t.Fatalf("a changed file must be token_mismatch naming --body: %s", r.out)
	}
}

func TestUpdateRefusesADriftedNote(t *testing.T) {
	e := newEnv(t, fullScope())
	tok := e.dryRun("note", "update", "1", "--title", "Mine")
	if _, err := e.store.Update("1", nil, nil, []string{"edited-elsewhere"}, nil); err != nil {
		t.Fatal(err)
	}
	r := e.run("note", "update", "1", "--title", "Mine", "--confirm", tok)
	if r.code() != "E_CONFLICT" || r.details()["conflict"] != "state_drift" {
		t.Fatalf("got %s", r.out)
	}
	n, _ := e.store.Get("1")
	if n.Title != "Groceries" {
		t.Fatal("a drifted note was changed")
	}
}

func TestDeleteNeedsDangerousAndIsVerified(t *testing.T) {
	e := newEnv(t, fullScope())
	r := e.run("note", "delete", "3", "--dry-run")
	if r.exit != 5 || r.details()["problem"] != "dangerous_flag_required" {
		t.Fatalf("got %s", r.out)
	}
	tok := e.dryRun("note", "delete", "3", "--dangerous")
	r = e.run("note", "rm", "notes/3", "--dangerous", "--confirm", tok)
	if r.exit != 0 {
		t.Fatalf("an alias and a ref must bind like the original form: %s", r.out)
	}
	if v := r.data()["receipt"].(map[string]any)["outcome"].(map[string]any)["verification"]; v != "passed" {
		t.Fatalf("verification: %v", v)
	}
	if _, err := e.store.Get("3"); err == nil {
		t.Fatal("note 3 still exists")
	}
}

func TestShareChecksTheDestinationAndHidesTheToken(t *testing.T) {
	e := newEnv(t, fullScope())
	const secret = "s3cr3t-share-token"
	r := e.run("note", "share", "1", "--to", "mallory@example.com", "--token", secret, "--dangerous", "--dry-run")
	if r.code() != "E_FORBIDDEN" || r.details()["scope"].(map[string]any)["status"] != "insufficient" {
		t.Fatalf("an ungranted destination must be refused: %s", r.out)
	}
	tok := e.dryRun("note", "share", "1", "--to", "bob@example.com", "--token", secret, "--dangerous")
	r = e.run("note", "share", "1", "--to", "bob@example.com", "--token", secret, "--dangerous", "--confirm", tok)
	if r.exit != 0 || r.data()["receipt"].(map[string]any)["outcome"].(map[string]any)["delivery"] != "delivered" {
		t.Fatalf("share: %s", r.out)
	}
	if strings.Contains(r.out+r.errOut, secret) {
		t.Fatal("the token appeared in the output")
	}
	_ = filepath.Walk(e.vars["NOTES_STATE_DIR"], func(p string, info os.FileInfo, err error) error {
		if err == nil && !info.IsDir() {
			if b, _ := os.ReadFile(p); strings.Contains(string(b), secret) {
				t.Errorf("%s stores the token", p)
			}
		}
		return nil
	})
}

func TestRefusedWritesAreNotUnknown(t *testing.T) {
	e := newEnv(t, fullScope())
	tok := e.dryRun("note", "share", "1", "--to", "bob@example.com", "--token", "revoked-token", "--dangerous")
	r := e.run("note", "share", "1", "--to", "bob@example.com", "--token", "revoked-token", "--dangerous", "--confirm", tok)
	if r.exit != 4 || r.code() != "E_FORBIDDEN" || r.details()["dispatch_state"] != "dispatched" {
		t.Fatalf("a 403 is a definite refusal: %s", r.out)
	}
	if !strings.Contains(strings.Join(anyStrings(r.doc["meta"].(map[string]any)["untrusted"]), ","), "error.details.upstream_message") {
		t.Fatalf("upstream text must be marked untrusted: %s", r.out)
	}
}

func TestServerErrorDuringWriteIsUnknownUntilResolved(t *testing.T) {
	e := newEnv(t, fullScope())
	tok := e.dryRun("note", "update", "2", "--title", "Maybe")
	e.store.fail = func(op, id string) error { return &APIError{Status: 502, Message: "bad gateway"} }
	r := e.run("note", "update", "2", "--title", "Maybe", "--confirm", tok)
	if r.exit != 6 || r.code() != "E_OUTCOME_UNKNOWN" || !strings.Contains(r.details()["lookup"].(string), "operation show") {
		t.Fatalf("a 5xx during a write is an unknown outcome, not E_SERVER: %s", r.out)
	}
	e.store.fail = nil
	id := r.details()["operation_id"].(string)
	blocked := e.run("note", "update", "2", "--title", "Again", "--dry-run")
	if blocked.details()["conflict"] != "unresolved_operation" {
		t.Fatalf("the target must stay blocked: %s", blocked.out)
	}
	show := e.run("operation", "show", id)
	if show.data()["status"] != "unknown" {
		t.Fatalf("operation show: %s", show.out)
	}
	res := e.run("operation", "resolve", id, "--outcome", "not_applied", "--evidence", "note 2 still has its old title")
	if res.exit != 0 || res.data()["status"] != "resolved" {
		t.Fatalf("resolve: %s", res.out)
	}
	if again := e.run("note", "update", "2", "--title", "Again", "--dry-run"); again.exit != 0 {
		t.Fatalf("still blocked after resolve: %s", again.out)
	}
}

func TestNetworkFailureBeforeSendIsNotDispatched(t *testing.T) {
	e := newEnv(t, fullScope())
	tok := e.dryRun("note", "create", "--title", "Offline")
	e.store.fail = func(string, string) error { return &APIError{Status: 0, Message: "connection refused"} }
	r := e.run("note", "create", "--title", "Offline", "--confirm", tok)
	if r.exit != 7 || r.code() != "E_NETWORK" || r.details()["dispatch_state"] != "not_started" || e.store.Writes() != 0 {
		t.Fatalf("got %s", r.out)
	}
}

func TestBulkTagReportsEachItem(t *testing.T) {
	e := newEnv(t, fullScope())
	tok := e.dryRun("note", "bulk-tag", "--ids", "1,notes/2,3", "--tag", "urgent")
	e.store.fail = func(op, id string) error {
		if id == "2" {
			return &APIError{Status: 409, Message: "note is locked"}
		}
		return nil
	}
	r := e.run("note", "bulk-tag", "--ids", "1,notes/2,3", "--tag", "urgent", "--confirm", tok)
	if r.exit != 1 || r.code() != "E_PARTIAL" {
		t.Fatalf("a partly failed batch is E_PARTIAL: %s", r.out)
	}
	summary := r.details()["summary"].(map[string]any)
	if summary["succeeded"].(json.Number).String() != "2" || summary["failed"].(json.Number).String() != "1" {
		t.Fatalf("summary: %v", summary)
	}
}

func TestIdempotentCreateReturnsTheFirstResult(t *testing.T) {
	e := newEnv(t, fullScope())
	args := []string{"note", "create", "--title", "Once", "--idempotency-key", "create-once-1"}
	// The second token is issued before the first confirm; a dry-run after it is
	// answered with the replay at once (P4).
	tok1, tok2 := e.dryRun(args...), e.dryRun(args...)
	first := e.run(append(args, "--confirm", tok1)...)
	second := e.run(append(args, "--confirm", tok2)...)
	third := e.run(append(args, "--dry-run")...)
	if first.exit != 0 || second.exit != 0 || third.exit != 0 || e.store.Writes() != 1 {
		t.Fatalf("first %s second %s third %s writes %d", first.out, second.out, third.out, e.store.Writes())
	}
	for _, r := range []result{second, third} {
		if r.data()["receipt"].(map[string]any)["replayed"] != true || r.data()["operation_id"] != first.data()["operation_id"] {
			t.Fatalf("the repeat must be a replay of the first: %s", r.out)
		}
	}
}

// TestAgentGuessesAreAcceptedOrAnswered runs the example's guess list (CLI-022): the
// forms agents type first are accepted, and the rest get an exact did_you_mean and
// the corrected command.
func TestAgentGuessesAreAcceptedOrAnswered(t *testing.T) {
	e := newEnv(t, fullScope())
	accepted := [][]string{
		{"note", "ls"},
		{"note", "list", "--label", "home"},
		{"note", "list", "--labels", "home"},
		{"note", "list", "--tags", "home"},
		{"notes", "list"},
		{"note", "view", "1"},
		{"note", "show", "1"},
		{"note", "add", "--title", "x", "--dry-run"},
		{"note", "new", "--title", "x", "--text", "body", "--dry-run"},
		{"note", "create", "--title", "x", "--message", "body", "--dry-run"},
		{"note", "edit", "1", "--title", "y", "--dry-run"},
		{"note", "rm", "3", "--dangerous", "--dry-run"},
	}
	for _, args := range accepted {
		if r := e.run(args...); r.exit != 0 {
			t.Fatalf("%v should be accepted: %s", args, r.out)
		}
	}
	answered := []struct {
		args    []string
		problem string
		suggest string
		next    string
	}{
		{[]string{"note", "lst"}, "unknown_command", "notes note list", "`notes note list`"},
		{[]string{"note", "edti", "1"}, "unknown_command", "notes note update", "`notes note update 1`"},
		// list takes no positional argument, so the rewrite points to its flags rather
		// than to a command line that would be refused.
		{[]string{"note", "search", "groceries"}, "unknown_command", "notes note list", "`notes note list --help`"},
		{[]string{"note", "find"}, "unknown_command", "notes note list", "`notes note list`"},
		{[]string{"bulk-tag"}, "unknown_command", "notes note bulk-tag", "`notes note bulk-tag`"},
		{[]string{"get", "1"}, "unknown_command", "notes note get", "`notes note get 1`"},
		{[]string{"note", "list", "--all"}, "unknown_flag", "--limit", "--limit <n>"},
		{[]string{"note", "list", "--tagg", "home"}, "unknown_flag", "--tag", "`notes note list --tag home`"},
	}
	for _, c := range answered {
		r := e.run(c.args...)
		if r.exit != 2 || r.details()["problem"] != c.problem {
			t.Fatalf("%v: %s", c.args, r.out)
		}
		if dym := anyStrings(r.details()["did_you_mean"]); len(dym) == 0 || dym[0] != c.suggest {
			t.Fatalf("%v should suggest %q first, got %v", c.args, c.suggest, dym)
		}
		if next, _ := r.details()["next_action"].(string); !strings.Contains(next, c.next) {
			t.Fatalf("%v: next_action %q should name %s", c.args, next, c.next)
		}
	}
	r := e.run("note", "list", "extra")
	if r.details()["problem"] != "unexpected_argument" {
		t.Fatalf("an unexpected argument must be an error: %s", r.out)
	}
	r = e.run("note", "create", "Groceries")
	if !strings.Contains(r.details()["next_action"].(string), "`notes note create --title Groceries`") {
		t.Fatalf("a stray title: %s", r.out)
	}
	r = e.run("note", "update", "2")
	if r.exit != 2 || r.details()["problem"] != "missing_parameter" {
		t.Fatalf("an update with nothing to change is a usage error: %s", r.out)
	}
}

// TestCommandMapIsTheSkillBlock pins the Skill command map (decision 4): the block
// SKILL-SPEC and the Skill template show is this output.
func TestCommandMapIsTheSkillBlock(t *testing.T) {
	app := newApp(newMemStore(), nil)
	want := "- `note`: list, get, create, update, delete (dangerous), share (dangerous), bulk-tag\n"
	if got := app.CommandMap(); got != want {
		t.Fatalf("command map:\n%s\nwant:\n%s", got, want)
	}
}

// TestShareTokenComesFromTheEnvironment: NOTES_SHARE_TOKEN is the documented path
// (SEC-040), and an environment value is bound like a flag (CLI-054).
func TestShareTokenComesFromTheEnvironment(t *testing.T) {
	e := newEnv(t, fullScope())
	e.vars["NOTES_SHARE_TOKEN"] = "revoked-token"
	tok := e.dryRun("note", "share", "1", "--to", "bob@example.com", "--dangerous")
	e.vars["NOTES_SHARE_TOKEN"] = "another-token"
	r := e.run("note", "share", "1", "--to", "bob@example.com", "--dangerous", "--confirm", tok)
	if r.code() != "E_CONFLICT" || r.details()["parameter"] != "--token" || e.store.Writes() != 0 {
		t.Fatalf("a changed environment token must not confirm: %s", r.out)
	}
	e.vars["NOTES_SHARE_TOKEN"] = "revoked-token"
	tok = e.dryRun("note", "share", "1", "--to", "bob@example.com", "--dangerous")
	r = e.run("note", "share", "1", "--to", "bob@example.com", "--dangerous", "--confirm", tok)
	if r.code() != "E_FORBIDDEN" || r.details()["required"] == nil || !strings.Contains(r.details()["next_action"].(string), "NOTES_SHARE_TOKEN") {
		t.Fatalf("the upstream 403 names what is required: %s", r.out)
	}
	if strings.Contains(r.out+r.errOut, "revoked-token") {
		t.Fatal("the token appeared in the output")
	}
}

func TestBuiltInsDescribeTheTool(t *testing.T) {
	e := newEnv(t, fullScope())
	idx := e.run("reference", "--index")
	if idx.exit != 0 || len(idx.out) > 8000 {
		t.Fatalf("index: %d chars", len(idx.out))
	}
	ctx := e.run("context")
	if ctx.data()["account"] != "tester" || ctx.data()["authorization"].(map[string]any)["status"] != "granted" {
		t.Fatalf("context: %s", ctx.out)
	}
	doc := e.run("doctor")
	if doc.exit != 0 || len(doc.data()["checks"].([]any)) < 5 {
		t.Fatalf("doctor: %s", doc.out)
	}
	cl := e.run("changelog", "--since", "0.0.1")
	if cl.exit != 0 || len(cl.data()["entries"].([]any)) != 1 {
		t.Fatalf("changelog: %s", cl.out)
	}
	help := e.run("--help")
	if !strings.Contains(help.out, "note bulk-tag") || !strings.Contains(help.out, "--confirm <token>") {
		t.Fatalf("help: %s", help.out)
	}
}

// TestHostIsASettingWithProfiles: the host comes from NOTES_HOST, then the active
// profile of the settings file, then the default (CLI-100, CLI-101); context names the
// profile and doctor checks the file.
func TestHostIsASettingWithProfiles(t *testing.T) {
	e := newEnv(t, fullScope())
	ctx := e.run("context")
	if ctx.data()["host"] != "memory://notes" || ctx.data()["profile"].(map[string]any)["source"] != "none" {
		t.Fatalf("without a settings file: %s", ctx.out)
	}
	state := e.vars["NOTES_STATE_DIR"]
	file := filepath.Join(state, "settings.json")
	_ = os.MkdirAll(state, 0o700)
	_ = os.WriteFile(file, []byte(`{"default_profile": "work", "profiles": {
		"work": {"host": "https://notes.work.example"}, "home": {"host": "https://notes.home.example"}}}`), 0o600)
	ctx = e.run("context")
	if p := ctx.data()["profile"].(map[string]any); ctx.data()["host"] != "https://notes.work.example" || p["name"] != "work" ||
		p["source"] != "config" || p["file"] != file {
		t.Fatalf("default profile: %s", ctx.out)
	}
	if ctx = e.run("context", "--profile", "home"); ctx.data()["host"] != "https://notes.home.example" {
		t.Fatalf("--profile: %s", ctx.out)
	}
	e.vars["NOTES_HOST"] = "https://notes.env.example"
	if ctx = e.run("context"); ctx.data()["host"] != "https://notes.env.example" {
		t.Fatalf("NOTES_HOST wins over the profile: %s", ctx.out)
	}
	doc := e.run("doctor")
	for _, c := range doc.data()["checks"].([]any) {
		if m := c.(map[string]any); m["check"] == "settings" && m["message"] != file+" is valid; profile `work` is active (config)." {
			t.Fatalf("doctor settings check: %v", m)
		}
	}
	// every tool command refuses to run against an unknown profile; the name is in
	// details.profile, never in the message (CLI-101)
	for _, args := range [][]string{{"note", "create", "--title", "x", "--dry-run"}, {"note", "list"}} {
		r := e.run(append(args, "--profile", "nope")...)
		message, _ := r.doc["error"].(map[string]any)["message"].(string)
		if r.exit != 4 || r.code() != "E_CONFIG" || r.details()["profile"] != "nope" || message != "The requested profile is not in the settings file." {
			t.Fatalf("unknown profile: %s", r.out)
		}
	}
	// the profile is bound into a write's token, whatever chose it (CLI-054)
	tok := e.dryRun("note", "create", "--title", "x")
	r := e.run("note", "create", "--title", "x", "--profile", "home", "--confirm", tok)
	if r.code() != "E_CONFLICT" || r.details()["parameter"] != "profile" {
		t.Fatalf("a token made under work must not confirm under home: %s", r.out)
	}
}

// TestUpdateIsTheBuiltIn: update comes from App.Update; a crafted target version is
// refused before anything is contacted.
func TestUpdateIsTheBuiltIn(t *testing.T) {
	e := newEnv(t, nil)
	ref := e.run("reference", "--command", "update")
	if ref.exit != 0 || ref.data()["command"].(map[string]any)["output"] != "aicli_update" {
		t.Fatalf("reference --command update: %s", ref.out)
	}
	r := e.run("update", "--target-version", "1.0.12&calc")
	if r.exit != 2 || r.code() != "E_USAGE" || r.details()["stage"] != "discover" || r.details()["parameter"] != "--target-version" {
		t.Fatalf("crafted target: %s", r.out)
	}
}

func anyStrings(x any) []string {
	list, _ := x.([]any)
	var out []string
	for _, v := range list {
		if s, ok := v.(string); ok {
			out = append(out, s)
		}
	}
	return out
}
