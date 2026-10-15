// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/spf13/cobra"
)

// Regression tests for the fixes before v2.0.0-rc.1 (decision 46), one or more per
// item, named after it. The update items (E) are in rc1_update_test.go.

// hookedUpdate replaces note update with a write whose Preview, the first time it
// runs for a confirm, calls *onConfirm: the test acts while that confirm holds the
// target's lock (CLI-057 steps 9 to 14).
func hookedUpdate(h *tharness, onConfirm *func()) {
	h.build = func(u *upstream) *App {
		a := testApp(u)
		c := a.commandByPath("note update")
		c.RunE = func(cmd *cobra.Command, _ []string) error {
			id, _ := cmd.Flags().GetString("id")
			title, _ := cmd.Flags().GetString("title")
			confirming := cmd.Flags().Changed("confirm")
			return a.Write(cmd, WriteSpec{
				Target: Target{Key: "notes/" + id, Kind: "note"},
				Preview: func(context.Context) (Preview, error) {
					if f := *onConfirm; confirming && f != nil {
						*onConfirm = nil
						f()
					}
					n, _ := u.get(id)
					return Preview{Action: "Update note " + id, CanApply: true, BaseVersion: strconv.Itoa(n.Version),
						Changes: []FieldChange{{Field: "title", After: title}}}, nil
				},
				Apply: func(context.Context, Op) (Result, error) {
					u.write(func() { u.notes[id].Title = title; u.notes[id].Version++ })
					return Result{}, nil
				},
			})
		}
		return a
	}
}

// recordFile reads a stored operation record as JSON.
func recordFile(t *testing.T, h *tharness, id string) map[string]any {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(h.state, "operations", id+".json"))
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatal(err)
	}
	return m
}

func writeRecordFile(t *testing.T, h *tharness, id string, m map[string]any) {
	t.Helper()
	b, _ := json.Marshal(m)
	mustWrite(t, filepath.Join(h.state, "operations", id+".json"), b)
}

func scopeUsageOf(t *testing.T, h *tharness) map[string]any {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(h.state, "scope-usage.json"))
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatal(err)
	}
	return m
}

func noticeOfType(r tout, typ string) map[string]any {
	meta, _ := r.env["meta"].(map[string]any)
	list, _ := meta["notices"].([]any)
	for _, n := range list {
		if m, _ := n.(map[string]any); m["type"] == typ {
			return m
		}
	}
	return nil
}

// ---------------------------------------------------------------- A. the gate

// A1: a used token answers token_used whatever its age; it never comes back as
// token_expired with a fresh plan that would send the write again.
func TestA1UsedTokenAfterExpiry(t *testing.T) {
	h := newHarness(t)
	now := time.Now().UTC()
	h.now = func() time.Time { return now }
	args := []string{"note", "comment", "add", "1", "--text", "Ship it"}
	tok := h.mustToken(args...)
	if r := h.run(append(args, "--confirm", tok)...); r.exit != 0 || h.up.Writes() != 1 {
		t.Fatalf("first confirm: %s", r)
	}
	batch := []string{"note", "bulk-tag", "--ids", "1,2", "--tag", "x"}
	btok := h.mustToken(batch...)
	if r := h.run(append(batch, "--confirm", btok)...); r.exit != 0 {
		t.Fatalf("batch confirm: %s", r)
	}
	now = now.Add(tokenLifetime + time.Second)
	for _, again := range [][]string{append(args, "--confirm", tok), append(batch, "--confirm", btok)} {
		r := h.run(again...)
		expectConflict(t, r, "token_used")
		if hasKey(r.details(), "confirm_token") || hasKey(r.details(), "preview") {
			t.Fatalf("a used token must not come back with a fresh plan: %s", r)
		}
	}
	if h.up.Writes() != 3 {
		t.Fatalf("writes: %d", h.up.Writes())
	}
	// An unused token past its expiry is still token_expired with a fresh plan.
	tok2 := h.mustToken(args...)
	now = now.Add(tokenLifetime + time.Second)
	if r := h.run(append(args, "--confirm", tok2)...); r.details()["conflict"] != "token_expired" || !isToken(r.details()["confirm_token"]) {
		t.Fatalf("an unused expired token: %s", r)
	}
}

// panicky is a result whose JSON encoding panics.
type panicky struct{}

func (panicky) MarshalJSON() ([]byte, error) { panic("the result cannot be marshalled") }

// commentWith replaces note comment add with a write whose Apply returns data.
func commentWith(h *tharness, data func() any, app **App) {
	h.build = func(u *upstream) *App {
		a := testApp(u)
		if app != nil {
			*app = a
		}
		c := a.commandByPath("note comment add")
		c.RunE = func(cmd *cobra.Command, _ []string) error {
			id, _ := cmd.Flags().GetString("id")
			text, _ := cmd.Flags().GetString("text")
			return a.Write(cmd, WriteSpec{
				Target:  Target{Key: "notes/" + id + "#comments", Kind: "comments"},
				Preview: func(context.Context) (Preview, error) { return Preview{Action: "Comment", CanApply: true}, nil },
				Apply: func(context.Context, Op) (Result, error) {
					u.write(func() { u.comments[id] = append(u.comments[id], text) })
					return Result{Data: data()}, nil
				},
			})
		}
		return a
	}
}

// A2: after the dispatching record is written, nothing reports "nothing was sent".
// An applied write whose result cannot be encoded keeps ok and its receipt.
func TestA2UnencodableResultKeepsTheReceipt(t *testing.T) {
	h := newHarness(t)
	var app *App
	commentWith(h, func() any { return map[string]any{"id": "1", "score": math.NaN()} }, &app)
	args := []string{"note", "comment", "add", "1", "--text", "Hi"}
	r := h.run(append(args, "--confirm", h.mustToken(args...))...)
	if r.exit != 0 || h.up.Writes() != 1 || r.data()["receipt"] == nil {
		t.Fatalf("an applied write with an unencodable result: %s", r)
	}
	if res, ok := r.data()["result"].(map[string]any); !ok || len(res) != 0 {
		t.Fatalf("data.result must be {}: %s", r)
	}
	id := r.data()["operation_id"].(string)
	n := noticeOfType(r, "result_unencodable")
	want := "The change was dispatched, but the tool's result could not be encoded, so data.result is empty; run `notes operation show --id " + id + "` for the receipt."
	if n == nil || n["severity"] != "warning" || n["message"] != want {
		t.Fatalf("result_unencodable notice: %s", r)
	}
	if len(app.defects) != 1 || !strings.Contains(app.defects[0], "Result.Data") {
		t.Fatalf("the conformance suite must see a defect: %v", app.defects)
	}
	if show := h.run("operation", "show", "--id", id); show.data()["status"] != "applied" || show.data()["receipt"] == nil {
		t.Fatalf("the receipt is recorded: %s", show)
	}
}

// A2: a failure the library cannot classify after the dispatch is an unknown
// outcome with the operation id, never E_UNKNOWN not_started.
func TestA2PanicAfterDispatchIsOutcomeUnknown(t *testing.T) {
	h := newHarness(t)
	commentWith(h, func() any { return panicky{} }, nil)
	args := []string{"note", "comment", "add", "1", "--text", "Hi"}
	r := h.run(append(args, "--confirm", h.mustToken(args...))...)
	d := r.details()
	id, _ := d["operation_id"].(string)
	if r.code() != "E_OUTCOME_UNKNOWN" || d["dispatch_state"] != "unknown" || id == "" || h.up.Writes() != 1 {
		t.Fatalf("a panic after the dispatch: %s", r)
	}
	if !strings.Contains(fmt.Sprint(d["lookup"]), id) || !strings.Contains(fmt.Sprint(d["cause"]), "cannot be marshalled") {
		t.Fatalf("lookup and cause: %s", r)
	}
	if !listHasString(r.env["meta"].(map[string]any)["untrusted"], "error.details.cause") {
		t.Fatalf("the panic text is untrusted: %s", r)
	}
}

// A3: the confirm checks and the dispatching record of one target are serialised:
// a second confirm waits for the first, and after 10 s answers lock_held.
func TestA3ConfirmsOnOneTargetAreSerialised(t *testing.T) {
	wait := targetLockWait
	targetLockWait = 300 * time.Millisecond
	t.Cleanup(func() { targetLockWait = wait })
	h := newHarness(t)
	var onConfirm func()
	hookedUpdate(h, &onConfirm)
	tokA := h.mustToken("note", "update", "1", "--title", "A")
	tokB := h.mustToken("note", "update", "1", "--title", "B")
	var inner tout
	onConfirm = func() { inner = h.run("note", "update", "1", "--title", "B", "--confirm", tokB) }
	r := h.run("note", "update", "1", "--title", "A", "--confirm", tokA)
	if r.exit != 0 {
		t.Fatalf("first confirm: %s", r)
	}
	expectConflict(t, inner, "lock_held")
	d := inner.details()
	if d["target"] != "notes/1" || d["active_operation"] != r.data()["operation_id"] ||
		inner.message() != "Another confirm on the same target is still running; nothing was sent." ||
		d["next_action"] != "Run the same command again in a few seconds; the confirm token stays valid until it expires." {
		t.Fatalf("lock_held: %s", inner)
	}
	if h.up.Writes() != 1 {
		t.Fatalf("writes: %d", h.up.Writes())
	}
	// The waiting confirm's token was not used: it now finds the target changed.
	expectConflict(t, h.run("note", "update", "1", "--title", "B", "--confirm", tokB), "state_drift")

	// A lock file older than 30 s was left by a crashed process and is broken.
	lock := filepath.Join(h.state, "locks", sha256Hex([]byte("notes/2"))[:32]+".lock")
	mustWrite(t, lock, []byte("op_aaaaaaaaaaaaaaaaaaaaaaaaaa"))
	stale := time.Now().Add(-time.Minute)
	if err := os.Chtimes(lock, stale, stale); err != nil {
		t.Fatal(err)
	}
	tokC := h.mustToken("note", "update", "2", "--title", "C")
	if r := h.run("note", "update", "2", "--title", "C", "--confirm", tokC); r.exit != 0 {
		t.Fatalf("a stale lock must be broken: %s", r)
	}
	// A batch takes every item's lock; a live lock of another confirm names it.
	mustWrite(t, lock, []byte("op_bbbbbbbbbbbbbbbbbbbbbbbbbb"))
	batch := []string{"note", "bulk-tag", "--ids", "1,2", "--tag", "x"}
	btok := h.mustToken(batch...)
	r = h.run(append(batch, "--confirm", btok)...)
	expectConflict(t, r, "lock_held")
	if r.details()["target"] != "notes/2" || r.details()["active_operation"] != "op_bbbbbbbbbbbbbbbbbbbbbbbbbb" || h.up.Writes() != 2 {
		t.Fatalf("batch lock_held: %s", r)
	}
	if err := os.Remove(lock); err != nil {
		t.Fatal(err)
	}
	if r := h.run(append(batch, "--confirm", btok)...); r.exit != 0 || h.up.Writes() != 4 {
		t.Fatalf("the token stays valid while it waits: %s", r)
	}
}

// A3: concurrent confirms of one token: exactly one write, every loser token_used,
// never state_drift with a fresh plan.
func TestA3ConcurrentConfirmsOfOneTokenAreTokenUsed(t *testing.T) {
	h := newHarness(t)
	tok := h.mustToken("note", "update", "3", "--title", "Raced")
	const n = 8
	var wg sync.WaitGroup
	results := make([]tout, n)
	start := make(chan struct{})
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			results[i] = h.run("note", "update", "3", "--title", "Raced", "--confirm", tok)
		}(i)
	}
	close(start)
	wg.Wait()
	ok := 0
	for _, r := range results {
		if r.exit == 0 {
			ok++
			continue
		}
		expectConflict(t, r, "token_used")
		if hasKey(r.details(), "confirm_token") {
			t.Fatalf("a loser got a fresh plan: %s", r)
		}
	}
	if ok != 1 || h.up.Writes() != 1 {
		t.Fatalf("%d successes, %d writes", ok, h.up.Writes())
	}
}

// A4: the operation budget is a durable count (scope-usage.json) that pruning the
// journal never refills.
func TestA4BudgetSurvivesPrune(t *testing.T) {
	h := newHarness(t)
	now := time.Now().UTC()
	h.now = func() time.Time { return now }
	h.grant(map[string]any{"expires_at": timeString(now.Add(365 * 24 * time.Hour)), "limits": map[string]any{"max_operations": 2}})
	var first string
	confirmed := 0
	for i, title := range []string{"A", "B", "C"} {
		if i == 1 {
			now = now.Add(31 * 24 * time.Hour) // the next dry-run prunes A's record
		}
		args := []string{"note", "update", "1", "--title", title}
		r := h.run(append(args, "--dry-run")...)
		if r.exit != 0 {
			if i < 2 || r.details()["scope"].(map[string]any)["status"] != "budget_spent" {
				t.Fatalf("write %s: %s", title, r)
			}
			continue
		}
		c := h.run(append(args, "--confirm", r.token())...)
		if c.exit == 0 {
			confirmed++
		}
		if i == 0 {
			first = c.data()["operation_id"].(string)
		}
	}
	if confirmed != 2 || h.up.Writes() != 2 {
		t.Fatalf("%d confirmed and %d sent under max_operations 2", confirmed, h.up.Writes())
	}
	if r := h.run("operation", "show", "--id", first); r.code() != "E_NOT_FOUND" {
		t.Fatalf("the first record should have been pruned: %s", r)
	}
	mustJSON(t, "scope-usage.json", scopeUsageOf(t, h), `{"v":1,"scopes":{"scp_test":2}}`)
}

// A4: the count is checked and raised in the critical section that creates the
// single-use marker, so two confirms on different targets cannot both pass.
func TestA4BudgetIsCountedWithTheMarker(t *testing.T) {
	h := newHarness(t)
	h.grant(map[string]any{"limits": map[string]any{"max_operations": 1}})
	var onConfirm func()
	hookedUpdate(h, &onConfirm)
	tokA := h.mustToken("note", "update", "1", "--title", "A")
	tokB := h.mustToken("note", "update", "2", "--title", "B")
	var inner tout
	// B confirms while A is between its budget pre-check and its marker.
	onConfirm = func() { inner = h.run("note", "update", "2", "--title", "B", "--confirm", tokB) }
	r := h.run("note", "update", "1", "--title", "A", "--confirm", tokA)
	if inner.exit != 0 {
		t.Fatalf("B: %s", inner)
	}
	if r.code() != "E_FORBIDDEN" || r.details()["scope"].(map[string]any)["status"] != "budget_spent" || h.up.Writes() != 1 {
		t.Fatalf("A must find the budget spent: %s", r)
	}
	// A created no marker: its token is refused for the budget, not as used.
	if again := h.run("note", "update", "1", "--title", "A", "--confirm", tokA); again.code() != "E_FORBIDDEN" {
		t.Fatalf("A again: %s", again)
	}
}

// A4: a batch counts each dispatched item; the items skipped after a stop give
// their unit back.
func TestA4SkippedBatchItemsGiveTheirBudgetBack(t *testing.T) {
	h := newHarness(t)
	h.grant(map[string]any{"limits": map[string]any{"max_operations": 3}})
	h.up.hook = func(kind, id string) error {
		if kind == "tag" && id == "1" {
			return Rejected(FromHTTP(422, "The tag is not allowed.", "", 0))
		}
		return nil
	}
	args := []string{"note", "bulk-tag", "--ids", "1,2,3", "--tag", "x", "--continue-on-error=false"}
	r := h.run(append(args, "--confirm", h.mustToken(args...))...)
	summary, _ := r.details()["summary"].(map[string]any)
	if r.exit == 0 || fmt.Sprint(summary["failed"]) != "1" || fmt.Sprint(summary["skipped"]) != "2" {
		t.Fatalf("batch: %s", r)
	}
	mustJSON(t, "scope-usage.json", scopeUsageOf(t, h), `{"v":1,"scopes":{"scp_test":1}}`)
}

// A5: an operation resolved by hand as applied is never sent again under its key;
// one resolved as not applied may be.
func TestA5ResolvedByHandIsNotSentAgain(t *testing.T) {
	h := newHarness(t)
	lost := func(string, string) error {
		h.up.mu.Lock()
		h.up.writes++ // the request reached the upstream, the reply was lost
		h.up.mu.Unlock()
		return context.DeadlineExceeded
	}
	for _, c := range []struct {
		key, outcome string
		resent       bool
	}{{"inv-77", "applied", false}, {"inv-78", "partial", false}, {"inv-79", "not_applied", true}} {
		args := []string{"note", "create", "--title", "Invoice " + c.key, "--idempotency-key", c.key}
		tok := h.mustToken(args...)
		again := h.mustToken(args...) // issued before the first confirm: it meets the lookup at confirm
		h.up.hook = lost
		r := h.run(append(args, "--confirm", tok)...)
		h.up.hook = nil
		if r.code() != "E_OUTCOME_UNKNOWN" {
			t.Fatalf("want an unknown outcome: %s", r)
		}
		id := r.details()["operation_id"].(string)
		expectConflict(t, h.run(append(args, "--dry-run")...), "unresolved_operation")
		if rr := h.run("operation", "resolve", "--id", id, "--outcome", c.outcome, "--evidence", "looked upstream"); rr.exit != 0 {
			t.Fatalf("resolve: %s", rr)
		}
		before := h.up.Writes()
		dry := h.run(append(args, "--dry-run")...)
		r = h.run(append(args, "--confirm", again)...)
		switch {
		case c.resent && (dry.token() == "" || r.exit != 0 || h.up.Writes() != before+1):
			t.Fatalf("resolved %s: the request may be sent again:\n%s\n%s", c.outcome, dry, r)
		case !c.resent:
			// at the dry-run (P4) and at the confirm (A5) alike
			for _, r := range []tout{dry, r} {
				expectConflict(t, r, "idempotency_resolved")
				if r.message() != "A request with this idempotency key was already resolved by hand; nothing was sent." ||
					r.details()["operation_id"] != id || r.details()["dispatch_state"] != "not_started" || h.up.Writes() != before ||
					r.details()["next_action"] != "Run `notes operation show --id "+id+"` to see what was recorded; if the change is missing, repeat the request with a new --idempotency-key." {
					t.Fatalf("resolved %s: %s", c.outcome, r)
				}
			}
		}
	}
}

// A5: a key matches only the same command path, inputs, target, account and host.
func TestA5KeyMatchesTheWholeRequest(t *testing.T) {
	h := newHarness(t)
	h.build = func(u *upstream) *App {
		a := testApp(u)
		a.commandByPath("note comment add").Flags().String("idempotency-key", "", "Repeat-safe key")
		a.Identity = func(context.Context) (Identity, error) {
			return Identity{Account: a.env("NOTES_ACCOUNT"), Host: a.env("NOTES_HOST")}, nil
		}
		return a
	}
	h.env["NOTES_ACCOUNT"], h.env["NOTES_HOST"] = "alice", "https://a.example"
	create := []string{"note", "create", "--title", "Weekly", "--idempotency-key", "K"}
	first := h.run(append(create, "--confirm", h.mustToken(create...))...)
	if first.exit != 0 {
		t.Fatalf("create: %s", first)
	}
	id := first.data()["operation_id"]
	// another command under the same key
	r := h.run("note", "comment", "add", "1", "--text", "X", "--idempotency-key", "K", "--dry-run")
	expectConflict(t, r, "idempotency_mismatch")
	if r.details()["operation_id"] != id {
		t.Fatalf("the mismatch names the earlier operation: %s", r)
	}
	// the same request as another account and host is another request
	h.env["NOTES_ACCOUNT"], h.env["NOTES_HOST"] = "bob", "https://b.example"
	expectConflict(t, h.run(append(create, "--dry-run")...), "idempotency_mismatch")
	if h.up.Writes() != 1 {
		t.Fatalf("writes: %d", h.up.Writes())
	}
}

// A5: a replay of a replay answers with the original operation, receipt and result.
func TestA5ReplayOfAReplayReturnsTheOriginal(t *testing.T) {
	h := newHarness(t)
	args := []string{"note", "create", "--title", "R", "--idempotency-key", "k-r"}
	// The tokens are issued before the first confirm: a dry-run after it is answered
	// with the replay at once (P4), and this test needs the confirms' replays.
	tokens := []string{h.mustToken(args...), h.mustToken(args...), h.mustToken(args...)}
	var first tout
	for i := 0; i < 4; i++ {
		var r tout
		if i < len(tokens) {
			r = h.run(append(args, "--confirm", tokens[i])...)
		} else {
			r = h.run(append(args, "--dry-run")...) // the replay at dry-run (P4)
		}
		if r.exit != 0 {
			t.Fatalf("run %d: %s", i, r)
		}
		if i == 0 {
			first = r
			continue
		}
		res, _ := r.data()["result"].(map[string]any)
		if r.data()["operation_id"] != first.data()["operation_id"] || res["ref"] != first.data()["result"].(map[string]any)["ref"] ||
			r.data()["receipt"].(map[string]any)["replayed"] != true {
			t.Fatalf("run %d must answer with the original: %s", i, r)
		}
	}
	if h.up.Writes() != 1 {
		t.Fatalf("writes: %d", h.up.Writes())
	}
}

// A5: a batch's key is looked up like a single write's: a repeat replays the batch;
// item keys are stable per target; a batch that ended partial is not sent again.
func TestA5BatchKeyIsLookedUp(t *testing.T) {
	var mu sync.Mutex
	var keys []string
	h := newHarness(t)
	h.build = func(u *upstream) *App {
		a := testApp(u)
		c := a.commandByPath("note bulk-tag")
		c.Flags().String("idempotency-key", "", "Repeat-safe key")
		c.RunE = func(cmd *cobra.Command, _ []string) error {
			ids, _ := cmd.Flags().GetStringSlice("ids")
			var items []BatchItem
			for _, id := range ids {
				items = append(items, BatchItem{
					Target:  Target{Key: "notes/" + id, Kind: "note"},
					Preview: func(context.Context) (Preview, error) { return Preview{Action: "Tag", CanApply: true}, nil },
					Apply: func(_ context.Context, op Op) (Result, error) {
						if err := u.runHook("tag", id); err != nil {
							return Result{}, err
						}
						mu.Lock()
						keys = append(keys, op.IdempotencyKey)
						mu.Unlock()
						u.write(func() {})
						return Result{Data: map[string]any{"id": id}}, nil
					},
				})
			}
			return a.WriteBatch(cmd, BatchSpec{Items: items})
		}
		return a
	}
	args := []string{"note", "bulk-tag", "--ids", "1,2", "--tag", "x", "--idempotency-key", "K1"}
	// Both tokens come before the first confirm: a dry-run after it replays at once (P4).
	tok1, tok2 := h.mustToken(args...), h.mustToken(args...)
	first := h.run(append(args, "--confirm", tok1)...)
	second := h.run(append(args, "--confirm", tok2)...)
	if first.exit != 0 || second.exit != 0 || h.up.Writes() != 2 {
		t.Fatalf("first %s\nsecond %s\nwrites %d", first, second, h.up.Writes())
	}
	if second.data()["operation_id"] != first.data()["operation_id"] {
		t.Fatalf("the replay names the original batch: %s", second)
	}
	for _, it := range second.data()["items"].([]any) {
		if it.(map[string]any)["receipt"].(map[string]any)["replayed"] != true {
			t.Fatalf("replayed items: %s", second)
		}
	}
	want := []string{"K1/" + sha256Hex([]byte("notes/1"))[:16], "K1/" + sha256Hex([]byte("notes/2"))[:16]}
	if fmt.Sprint(keys) != fmt.Sprint(want) {
		t.Fatalf("item keys %v, want %v", keys, want)
	}
	// A batch that ended partial is resolved: repeating it would send its applied
	// items twice.
	h.up.hook = func(kind, id string) error {
		if id == "2" {
			return Rejected(FromHTTP(422, "Refused.", "", 0))
		}
		return nil
	}
	partial := []string{"note", "bulk-tag", "--ids", "1,2", "--tag", "y", "--idempotency-key", "K2"}
	ptok, repeat := h.mustToken(partial...), h.mustToken(partial...)
	p := h.run(append(partial, "--confirm", ptok)...)
	if p.code() != "E_PARTIAL" {
		t.Fatalf("partial: %s", p)
	}
	h.up.hook = nil
	before := h.up.Writes()
	// at the dry-run (P4) and at the confirm alike
	for _, r := range []tout{h.run(append(partial, "--dry-run")...), h.run(append(partial, "--confirm", repeat)...)} {
		expectConflict(t, r, "idempotency_resolved")
		if r.details()["operation_id"] != p.details()["operation_id"] || h.up.Writes() != before {
			t.Fatalf("partial repeat: %s", r)
		}
	}
}

// A6: the lease outlasts Apply's deadline by 5 s, and a record resolved while Apply
// still ran keeps its resolution; the confirm reports an unknown outcome.
func TestA6ResolutionDuringApplyWins(t *testing.T) {
	h := newHarness(t)
	now := time.Now().UTC()
	h.now = func() time.Time { return now }
	tok := h.mustToken("note", "update", "1", "--title", "X")
	var id string
	h.up.hook = func(string, string) error {
		list := h.run("operation", "list", "--state", "dispatching")
		items, _ := list.data()["items"].([]any)
		if len(items) != 1 {
			t.Errorf("one dispatching operation: %s", list)
			return nil
		}
		id = items[0].(map[string]any)["operation_id"].(string)
		rec := recordFile(t, h, id)
		dispatched, _ := parseTime(rec["dispatched_at"].(string))
		lease, _ := parseTime(rec["lease_until"].(string))
		if lease.Sub(dispatched) != defaultTimeout+leaseGrace {
			t.Errorf("lease %v after dispatch, want %v", lease.Sub(dispatched), defaultTimeout+leaseGrace)
		}
		now = now.Add(defaultTimeout + leaseGrace + time.Second) // Apply overran its deadline
		if r := h.run("operation", "resolve", "--id", id, "--outcome", "not_applied", "--evidence", "the title is unchanged"); r.exit != 0 {
			t.Errorf("resolve after the lease: %s", r)
		}
		return nil // and then Apply "succeeds"
	}
	r := h.run("note", "update", "1", "--title", "X", "--confirm", tok)
	d := r.details()
	if r.code() != "E_OUTCOME_UNKNOWN" || r.message() != "The operation was resolved by hand while it was still running; its outcome is unknown." ||
		d["operation_id"] != id || d["lookup"] != "notes operation show --id "+id ||
		d["next_action"] != "Run `notes operation show --id "+id+"` and re-read the target before anything else; do not resend." {
		t.Fatalf("resolved during Apply: %s", r)
	}
	rec := recordFile(t, h, id)
	if rec["status"] != "resolved" || rec["late_receipt"] == nil || rec["resolution"].(map[string]any)["outcome"] != "not_applied" {
		t.Fatalf("the resolution stands, with the late receipt beside it: %v", rec)
	}
}

// A7: a batch's items are pruned with the batch record, by the batch's finish time.
func TestA7BatchIsPrunedWithItsItems(t *testing.T) {
	h := newHarness(t)
	now := time.Now().UTC()
	h.now = func() time.Time { return now }
	h.grant(map[string]any{"expires_at": timeString(now.Add(365 * 24 * time.Hour))})
	args := []string{"note", "bulk-tag", "--ids", "1,2", "--tag", "x"}
	r := h.run(append(args, "--confirm", h.mustToken(args...))...)
	if r.exit != 0 {
		t.Fatalf("batch: %s", r)
	}
	batch := r.data()["operation_id"].(string)
	ids := []string{batch}
	for _, it := range r.data()["items"].([]any) {
		ids = append(ids, it.(map[string]any)["operation_id"].(string))
	}
	// The batch finished two days after its items.
	rec := recordFile(t, h, batch)
	rec["finished_at"] = timeString(now.Add(48 * time.Hour))
	writeRecordFile(t, h, batch, rec)
	exists := func(id string) bool {
		_, err := os.Stat(filepath.Join(h.state, "operations", id+".json"))
		return err == nil
	}
	now = now.Add(31 * 24 * time.Hour) // past the items' finish, not the batch's
	h.run("note", "update", "3", "--title", "P", "--dry-run")
	for _, id := range ids {
		if !exists(id) {
			t.Fatalf("%s was pruned before its batch", id)
		}
	}
	if show := h.run("operation", "show", "--id", batch); show.exit != 0 || strings.Contains(show.stdout, `"status":"unknown"`) {
		t.Fatalf("the batch still names its items: %s", show)
	}
	now = now.Add(3 * 24 * time.Hour)
	h.run("note", "update", "3", "--title", "P", "--dry-run")
	for _, id := range ids {
		if exists(id) {
			t.Fatalf("%s was not pruned with its batch", id)
		}
	}
}

// A8: a record missing a required key, or with a key spelled in another case, is
// unreadable: it blocks every target and operation show says so.
func TestA8MalformedRecordIsUnreadable(t *testing.T) {
	for name, change := range map[string]func(map[string]any){
		"no command":      func(m map[string]any) { delete(m, "command") },
		"no effects":      func(m map[string]any) { delete(m, "effects") },
		"effects string":  func(m map[string]any) { m["effects"] = "change" },
		"created_at null": func(m map[string]any) { m["created_at"] = nil },
		"case variant":    func(m map[string]any) { m["Status"] = "applied" },
	} {
		t.Run(name, func(t *testing.T) {
			h := newHarness(t)
			r := h.run("note", "update", "1", "--title", "X", "--dry-run")
			id := r.data()["operation_id"].(string)
			rec := recordFile(t, h, id)
			change(rec)
			writeRecordFile(t, h, id, rec)
			blocked := h.run("note", "update", "2", "--title", "Y", "--dry-run")
			expectConflict(t, blocked, "unresolved_operation")
			if blocked.details()["active_operation"] != id {
				t.Fatalf("the unreadable record blocks: %s", blocked)
			}
			show := h.run("operation", "show", "--id", id)
			if show.data()["status"] != "unknown" || show.data()["command"] != "unknown" {
				t.Fatalf("operation show reports it unreadable: %s", show)
			}
		})
	}
}

// ---------------------------------------------------------------- B. binding and secrets

// B1: the active profile, whatever chose it, and every setting's resolved value are
// bound into the token.
func TestB1ProfileAndSettingsAreBound(t *testing.T) {
	h := newHarness(t)
	h.build = func(u *upstream) *App {
		a := testApp(u)
		a.Settings = []Setting{{Name: "visibility", Default: "private"}, {Name: "api_token", Env: "TOKEN", Secret: true}}
		return a
	}
	settings := filepath.Join(h.dir, "settings.json")
	mustWrite(t, settings, []byte(`{"default_profile":"team","profiles":{"team":{"visibility":"private"},"blog":{"visibility":"public"}}}`))
	h.env["NOTES_SETTINGS_FILE"] = settings
	h.env["NOTES_TOKEN"] = "tok-SENTINEL-1"
	args := []string{"note", "update", "1", "--title", "X"}
	r := h.run(append(args, "--dry-run")...)
	inputs := r.data()["preview"].(map[string]any)["inputs"].(map[string]any)
	if inputs["profile"] != "team" || fmt.Sprint(inputs["setting"]) != "map[api_token:*** visibility:private]" ||
		strings.Contains(r.stdout, "tok-SENTINEL-1") {
		t.Fatalf("preview inputs: %s", r)
	}
	tok := r.token()
	h.env["NOTES_PROFILE"] = "blog"
	expectMismatch(t, h.run(append(args, "--confirm", tok)...), "profile")
	// The same profile chosen another way is the same request.
	tok = h.mustToken(append(args, "--profile", "blog")...)
	if c := h.run(append(args, "--confirm", tok)...); c.exit != 0 {
		t.Fatalf("--profile blog, then NOTES_PROFILE=blog: %s", c)
	}
	delete(h.env, "NOTES_PROFILE")
	tok = h.mustToken(args...)
	h.env["NOTES_VISIBILITY"] = "public"
	expectMismatch(t, h.run(append(args, "--confirm", tok)...), "setting.visibility")
	delete(h.env, "NOTES_VISIBILITY")
	tok = h.mustToken(args...)
	h.env["NOTES_TOKEN"] = "tok-SENTINEL-2"
	expectMismatch(t, h.run(append(args, "--confirm", tok)...), "setting.api_token")
	_ = filepath.Walk(h.state, func(p string, info os.FileInfo, err error) error {
		if err == nil && !info.IsDir() {
			if b, _ := os.ReadFile(p); strings.Contains(string(b), "SENTINEL") || strings.Contains(string(b), sha256Hex([]byte("tok-SENTINEL-1"))) {
				t.Errorf("%s stores the secret setting", p)
			}
		}
		return nil
	})
}

// B2: one notion of a secret flag: the literal twin of a secret file flag and the
// Flag of a Secret setting are secret everywhere.
func TestB2OneNotionOfASecretFlag(t *testing.T) {
	h := newHarness(t)
	h.build = func(u *upstream) *App {
		a := testApp(u)
		a.Settings = []Setting{{Name: "api_token", Flag: "token", Secret: true}}
		a.commandByPath("note update").Flags().String("token", "", "API token")
		login := &cobra.Command{Use: "login", RunE: func(cmd *cobra.Command, _ []string) error {
			return a.Write(cmd, WriteSpec{Target: Target{Key: "notes", Kind: "account"},
				Preview: func(context.Context) (Preview, error) {
					return Preview{Action: "Store a credential", CanApply: true}, nil
				},
				Apply: func(context.Context, Op) (Result, error) { return Result{}, nil }})
		}}
		login.Flags().String("password", "", "Password")
		login.Flags().String("password-file", "", "Read the password from a file")
		a.commandByPath("note").AddCommand(login)
		a.Register(login, Meta{Summary: "Store a credential", Effects: []Effect{Change}, Output: "note_write",
			FileInputs: map[string]string{"password-file": "password"}, Secrets: []string{"password-file"},
			Examples: []string{"notes note login --password-file p --dry-run", "notes note login --password-file p --confirm <token>"}})
		return a
	}
	for _, c := range []struct {
		args   []string
		secret string
		input  string
	}{
		{[]string{"note", "login", "--password", "hunter2-SENTINEL"}, "hunter2-SENTINEL", "password"},
		{[]string{"note", "update", "1", "--title", "X", "--token", "SETTINGFLAG-SENTINEL"}, "SETTINGFLAG-SENTINEL", "token"},
	} {
		r := h.run(append(c.args, "--dry-run")...)
		inputs, _ := r.data()["preview"].(map[string]any)["inputs"].(map[string]any)
		if r.exit != 0 || inputs[c.input] != "***" || strings.Contains(r.stdout+r.stderr, c.secret) {
			t.Fatalf("%v: %s", c.args, r)
		}
		_ = filepath.Walk(h.state, func(p string, info os.FileInfo, err error) error {
			if err == nil && !info.IsDir() {
				if b, _ := os.ReadFile(p); strings.Contains(string(b), c.secret) || strings.Contains(string(b), sha256Hex([]byte(c.secret))) {
					t.Errorf("%s stores %s or its unsalted digest", p, c.input)
				}
			}
			return nil
		})
	}
	for path, flag := range map[string]string{"note login": "password", "note update": "token"} {
		ref := h.run("reference", "--command", path)
		params, _ := ref.data()["command"].(map[string]any)["params"].([]any)
		found := false
		for _, p := range params {
			if m := p.(map[string]any); m["name"] == flag {
				found = m["secret"] == true
			}
		}
		if !found {
			t.Fatalf("reference must show --%s of %s as secret: %s", flag, path, ref)
		}
	}
}

// B4: two file flags for one bound name, or a file flag mapped onto something that
// cannot be its literal twin, are registration defects.
func TestB4SharedBoundNamesAreDefects(t *testing.T) {
	a := testApp(newUpstream())
	c := &cobra.Command{Use: "import", RunE: func(*cobra.Command, []string) error { return nil }}
	for _, f := range []string{"a-file", "b-file", "c-file", "d-file"} {
		c.Flags().String(f, "", f)
	}
	c.Flags().Bool("force", false, "Force")
	a.commandByPath("note").AddCommand(c)
	a.Register(c, Meta{Summary: "Import", Effects: []Effect{Observe}, Output: "note", Examples: []string{"notes note import"},
		FileInputs: map[string]string{"a-file": "body", "b-file": "body", "c-file": "d-file", "d-file": "force"}})
	a.InstallBuiltins()
	a.prepareTree()
	all := strings.Join(a.checkTree(), "\n")
	for _, want := range []string{
		`"note import": Meta.FileInputs maps both --a-file and --b-file to "body"; a bound name has one file flag`,
		`"note import": Meta.FileInputs maps --c-file onto --d-file, which is itself a file input`,
		`"note import": Meta.FileInputs maps --d-file onto --force, which is not a string flag and so cannot be its literal twin`,
	} {
		if !strings.Contains(all, want) {
			t.Errorf("tree check misses %q:\n%s", want, all)
		}
	}
}

// ---------------------------------------------------------------- C. scope and local files

func artifactScope(outputs, roots []string) map[string]any {
	s := map[string]any{"effects": []string{"artifact", "persist"}}
	if outputs != nil {
		s["outputs"] = outputs
	}
	if roots != nil {
		s["roots"] = roots
	}
	return s
}

// C1: a new file must be inside outputs, an existing one (an overwrite) inside
// roots; absent roots, nothing may be overwritten.
func TestC1OutputsForNewFilesRootsForExisting(t *testing.T) {
	h := newHarness(t)
	exports := filepath.Join(h.dir, "exports")
	existing := filepath.Join(exports, "report.tsv")
	mustWrite(t, existing, []byte("the user's own report\n"))
	h.grant(artifactScope([]string{exports}, nil))
	r := h.run("note", "export", "--output", existing, "--dry-run")
	scope, _ := r.details()["scope"].(map[string]any)
	missing, _ := scope["missing"].(map[string]any)
	example, _ := scope["grant_example"].(map[string]any)
	if r.code() != "E_FORBIDDEN" || !listHasString(missing["roots"], existing) || hasKey(missing, "outputs") ||
		!listHasString(example["roots"], exports) {
		t.Fatalf("an overwrite under an outputs-only grant: %s", r)
	}
	h.grant(artifactScope(nil, []string{exports}))
	if r := h.run("note", "export", "--output", existing, "--confirm", h.mustToken("note", "export", "--output", existing)); r.exit != 0 {
		t.Fatalf("roots allow the overwrite: %s", r)
	}
	if b, _ := os.ReadFile(existing); strings.Contains(string(b), "the user's own report") {
		t.Fatal("the file was not overwritten")
	}
	fresh := filepath.Join(exports, "new.tsv")
	r = h.run("note", "export", "--output", fresh, "--dry-run")
	scope, _ = r.details()["scope"].(map[string]any)
	missing, _ = scope["missing"].(map[string]any)
	example, _ = scope["grant_example"].(map[string]any)
	if r.code() != "E_FORBIDDEN" || !listHasString(missing["outputs"], fresh) || !listHasString(example["outputs"], exports) {
		t.Fatalf("a new file under a roots-only grant: %s", r)
	}
	if err := ValidateEnvelope([]byte(r.stdout)); err != nil {
		t.Fatal(err)
	}
}

// C1: WriteOutput checks again at write time; a file that appeared under an
// outputs-only grant is refused and left as it is.
func TestC1WriteOutputRefusesAFileThatAppearedAtWriteTime(t *testing.T) {
	h := newHarness(t)
	dir := filepath.Join(h.dir, "out")
	path := filepath.Join(dir, "notes.tsv")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	h.grant(artifactScope([]string{dir}, nil))
	tok := h.mustToken("note", "export", "--output", path)
	h.up.hook = func(kind, _ string) error {
		if kind == "export" {
			mustWrite(t, path, []byte("appeared\n"))
		}
		return nil
	}
	r := h.run("note", "export", "--output", path, "--confirm", tok)
	missing, _ := r.details()["scope"].(map[string]any)["missing"].(map[string]any)
	if r.code() != "E_FORBIDDEN" || r.message() != "The output file already exists and the scope does not allow overwriting it; nothing was written." ||
		r.details()["next_action"] != "Ask the user to add the directory to the scope's roots, or choose a new file name." ||
		r.details()["dispatch_state"] != "not_started" || !listHasString(missing["roots"], path) {
		t.Fatalf("write-time refusal: %s", r)
	}
	if b, _ := os.ReadFile(path); string(b) != "appeared\n" {
		t.Fatalf("the file that appeared was overwritten: %q", b)
	}
}

// C1: the validators type missing.roots like missing.outputs.
func TestC1ValidatorsTypeMissingRoots(t *testing.T) {
	for roots, ok := range map[string]bool{`["/data/report.tsv"]`: true, `[""]`: false, `"/data"`: false, `[1]`: false} {
		var scope any
		raw := `{"status":"insufficient","path":"/s/scope.json","scope_id":"s","missing":{"roots":` + roots + `}}`
		if err := json.Unmarshal([]byte(raw), &scope); err != nil {
			t.Fatal(err)
		}
		v := &validation{}
		validateScopeRefusal(v, "scope", scope)
		if v.failed() == ok {
			t.Errorf("missing.roots %s: problems %v", roots, v.err())
		}
	}
}

// C2: a junction (or other reparse point) in an output path, or in a granted
// directory, is refused: Go 1.23+ filepath.EvalSymlinks no longer follows it.
func TestC2JunctionsAreRefused(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("junctions are a Windows feature")
	}
	h := newHarness(t)
	allowed, other := filepath.Join(h.dir, "allowed"), filepath.Join(h.dir, "other")
	_ = os.MkdirAll(allowed, 0o700)
	_ = os.MkdirAll(other, 0o700)
	link := filepath.Join(allowed, "j")
	if out, err := exec.Command("cmd", "/c", "mklink", "/J", link, other).CombinedOutput(); err != nil {
		t.Skipf("mklink /J: %v %s", err, out)
	}
	target := filepath.Join(link, "x.tsv")
	if _, reason := resolvePath(target); !strings.Contains(reason, "a junction or other reparse point is in the path") {
		t.Fatalf("resolvePath: %q", reason)
	}
	if pathWithin(target, []string{allowed}) {
		t.Fatal("a path through a junction lies inside nothing")
	}
	h.grant(artifactScope([]string{allowed}, []string{allowed}))
	r := h.run("note", "export", "--output", target, "--dry-run")
	if r.code() != "E_FORBIDDEN" || !strings.Contains(r.message(), "a junction or other reparse point is in the path "+target) {
		t.Fatalf("an output through a junction: %s", r)
	}
	if _, err := os.Stat(filepath.Join(other, "x.tsv")); err == nil {
		t.Fatal("a write landed outside the scope")
	}
	h.grant(artifactScope([]string{link}, nil))
	r = h.run("note", "export", "--output", filepath.Join(allowed, "y.tsv"), "--dry-run")
	if scope, _ := r.details()["scope"].(map[string]any); scope["status"] != "invalid" ||
		!strings.Contains(r.message(), "a junction or other reparse point is in the path "+link) {
		t.Fatalf("a granted junction makes the scope invalid: %s", r)
	}
}

// C3: keys of the scope file are matched exactly: encoding/json's case-insensitive
// matching must not turn "Dangerous": true into a grant.
func TestC3ScopeKeysAreExact(t *testing.T) {
	base := `"scope_id":"s","tool":"notes","granted_by":"me","granted_at":"2026-10-12T08:00:00Z","expires_at":"2026-10-19T08:00:00Z","effects":["change"],"dangerous":false`
	for raw, want := range map[string]string{
		`{` + base + `,"Dangerous":true}`:                                                     "unknown key `Dangerous` (did you mean `dangerous`?)",
		`{` + base + `,"targets":[{"key":"ops/n-1","KEY":"ops"}]}`:                            "unknown key `KEY` (did you mean `key`?)",
		`{` + base + `,"limits":{"max_operations":1,"Max_Operations":1000}}`:                  "unknown key `Max_Operations` (did you mean `max_operations`?)",
		`{"Scope_id":"x",` + strings.TrimPrefix(base, `"scope_id":"s",`) + `,"scope_id":"s"}`: "unknown key `Scope_id` (did you mean `scope_id`?)",
	} {
		if f, reason := parseScope([]byte(raw)); f != nil || reason != want {
			t.Errorf("%s:\n got %q\nwant %q", raw, reason, want)
		}
	}
}

// C4: the scope and settings files are named by absolute paths and, on POSIX,
// writable by their owner only.
func TestC4ScopeAndSettingsFileChecks(t *testing.T) {
	h := settingsHarness(t)
	scopeFile := h.scope
	h.scope = "scope.json"
	r := h.run("note", "update", "1", "--title", "X", "--dry-run")
	if scope, _ := r.details()["scope"].(map[string]any); scope["status"] != "invalid" ||
		r.message() != "The scope file is invalid (NOTES_SCOPE_FILE must be an absolute path), so nothing was sent." {
		t.Fatalf("a relative scope path: %s", r)
	}
	h.scope = scopeFile
	h.env["NOTES_SETTINGS_FILE"] = "settings.json"
	if r := h.run("probe"); r.code() != "E_CONFIG" ||
		r.message() != "The settings file settings.json is invalid: NOTES_SETTINGS_FILE must be an absolute path." {
		t.Fatalf("a relative settings path: %s", r)
	}
	delete(h.env, "NOTES_SETTINGS_FILE")
	if runtime.GOOS == "windows" {
		return // the profile ACL protects these files on Windows
	}
	if err := os.Chmod(scopeFile, 0o666); err != nil {
		t.Fatal(err)
	}
	r = h.run("note", "update", "1", "--title", "X", "--dry-run")
	if !strings.Contains(r.message(), scopeFile+" is writable by other users; only its owner may write it") {
		t.Fatalf("a scope file others can write: %s", r)
	}
	_ = os.Chmod(scopeFile, 0o600)
	settings := writeSettings(t, h, `{"profiles": {"work": {}}}`)
	if err := os.Chmod(settings, 0o620); err != nil {
		t.Fatal(err)
	}
	if r := h.run("probe"); r.code() != "E_CONFIG" || !strings.Contains(r.message(), settings+" is writable by other users") {
		t.Fatalf("a settings file its group can write: %s", r)
	}
}

// ---------------------------------------------------------------- D. settings, debug, redaction

// pingApp adds a read command that fails with text holding its secrets.
func pingApp(h *tharness, fail func(a *App, cmd *cobra.Command) error) {
	h.build = func(u *upstream) *App {
		a := testApp(u)
		a.Settings = []Setting{{Name: "hook_secret", Secret: true}}
		cmd := &cobra.Command{Use: "ping", RunE: func(cmd *cobra.Command, _ []string) error { return fail(a, cmd) }}
		cmd.Flags().StringP("token", "k", "", "Access token")
		cmd.Flags().String("host", "", "Host")
		a.Root.AddCommand(cmd)
		a.Schema("ping_result", SchemaDef{Shape: "object"})
		a.Register(cmd, Meta{Summary: "Ping", Effects: []Effect{Observe}, Output: "ping_result", Secrets: []string{"token"},
			Examples: []string{"notes ping"}})
		return a
	}
}

// D1, D2: no emitted error shows a secret: secret flags, Secret settings and values
// registered with AddSecret, raw or URL-escaped, in the cause, the upstream message
// or a panic text.
func TestD1ErrorsAreRedacted(t *testing.T) {
	const keyring = "key ring/SENTINEL+3"
	mode := ""
	h := newHarness(t)
	pingApp(h, func(a *App, cmd *cobra.Command) error {
		tok, _ := cmd.Flags().GetString("token")
		sec, _, e := a.Setting(cmd, "hook_secret")
		if e != nil {
			return e
		}
		a.AddSecret(keyring)
		a.AddSecret("abc") // too short to redact
		switch mode {
		case "panic":
			panic("token " + tok + " was refused")
		case "upstream":
			return FromHTTP(401, "The upstream refused the credentials.", "token "+tok+" and "+url.PathEscape(keyring)+" are invalid", 0)
		}
		return fmt.Errorf("GET https://h.example/hook/%s?private_token=%s&k=%s failed (abc)", sec, tok, url.QueryEscape(keyring))
	})
	h.env["NOTES_HOOK_SECRET"] = "HOOK-SENTINEL-2"
	for _, mode = range []string{"cause", "panic", "upstream"} {
		r := h.run("ping", "--token", "glpat-SENTINEL-1")
		for _, s := range []string{"glpat-SENTINEL-1", "HOOK-SENTINEL-2", keyring, url.QueryEscape(keyring), url.PathEscape(keyring)} {
			if strings.Contains(r.stdout, s) {
				t.Fatalf("%s: %q appears: %s", mode, s, r.stdout)
			}
		}
		if r.exit == 0 || !strings.Contains(r.stdout, "***") {
			t.Fatalf("%s: %s", mode, r)
		}
		if mode == "cause" && !strings.Contains(r.stdout, "(abc)") {
			t.Fatalf("a value under 4 characters is not a secret to redact: %s", r)
		}
	}
}

// D2: AddSecret also redacts debug lines, in their escaped forms too.
func TestD2AddSecretRedactsDebugLines(t *testing.T) {
	h := newHarness(t)
	pingApp(h, func(a *App, cmd *cobra.Command) error {
		a.AddSecret("kr/SENTINEL 9")
		a.Debugf("token %s, query %s, path %s", "kr/SENTINEL 9", url.QueryEscape("kr/SENTINEL 9"), url.PathEscape("kr/SENTINEL 9"))
		return a.OK(cmd, map[string]any{})
	})
	h.env["NOTES_DEBUG"] = "1"
	r := h.run("ping")
	if strings.Contains(r.stderr, "SENTINEL") || !strings.Contains(r.stderr, "debug: token ***, query ***, path ***") {
		t.Fatalf("debug: %q", r.stderr)
	}
}

// D3: App.Profile and App.SettingsPath tell a tool the active profile and the file.
func TestD3ProfileAccessors(t *testing.T) {
	h := settingsHarness(t)
	h.build = func(build func(*upstream) *App) func(*upstream) *App {
		return func(u *upstream) *App {
			a := build(u)
			who := &cobra.Command{Use: "whoami", RunE: func(cmd *cobra.Command, _ []string) error {
				name, source := a.Profile()
				return a.OK(cmd, map[string]any{"name": name, "source": source, "file": a.SettingsPath()})
			}}
			a.Root.AddCommand(who)
			a.Register(who, Meta{Summary: "Who", Effects: []Effect{Observe}, Output: "probe_result", Examples: []string{"notes whoami"}})
			return a
		}
	}(h.build)
	file := filepath.Join(h.state, "settings.json")
	if d := h.run("whoami").data(); d["name"] != "" || d["source"] != "none" || d["file"] != file {
		t.Fatalf("no settings file: %v", d)
	}
	writeSettings(t, h, `{"default_profile": "work", "profiles": {"work": {}, "home": {}}}`)
	for _, c := range []struct {
		args         []string
		env          string
		name, source string
	}{{nil, "", "work", "config"}, {[]string{"--profile", "home"}, "", "home", "flag"}, {nil, "home", "home", "env"}} {
		h.env["NOTES_PROFILE"] = c.env
		if d := h.run(append([]string{"whoami"}, c.args...)...).data(); d["name"] != c.name || d["source"] != c.source {
			t.Fatalf("%v %q: %v", c.args, c.env, d)
		}
	}
}

// D4, D5: a requested profile that is not in the settings file stops every tool
// command before its own code, without echoing the name; the built-ins still run.
func TestD4UnknownProfileStopsEveryToolCommand(t *testing.T) {
	h := settingsHarness(t)
	writeSettings(t, h, `{"profiles": {"work": {}}}`)
	for _, args := range [][]string{{"note", "get", "1"}, {"note", "update", "1", "--title", "X", "--dry-run"}, {"note", "list"}} {
		r := h.run(append(args, "--profile", "nope-SENTINEL")...)
		if r.code() != "E_CONFIG" || r.message() != "The requested profile is not in the settings file." ||
			r.details()["profile"] != "nope-SENTINEL" || r.details()["source"] != "flag" ||
			strings.Contains(r.message()+fmt.Sprint(r.details()["next_action"]), "SENTINEL") {
			t.Fatalf("%v: %s", args, r)
		}
	}
	if _, err := os.Stat(filepath.Join(h.state, "operations")); err == nil {
		if entries, _ := os.ReadDir(filepath.Join(h.state, "operations")); len(entries) != 0 {
			t.Fatal("a write under an unknown profile recorded an operation")
		}
	}
	for _, args := range [][]string{{"context"}, {"doctor"}, {"reference", "--index"}, {"changelog"}, {"operation", "list"},
		{"help"}, {"--version"}, {"note", "list", "--help"}} {
		if r := h.run(append(args, "--profile", "nope")...); r.exit != 0 {
			t.Fatalf("the built-in %v must still run: %s", args, r)
		}
	}
	// No settings file at all (D5).
	h2 := settingsHarness(t)
	h2.env["NOTES_PROFILE"] = "ghost-SENTINEL"
	r := h2.run("probe")
	path := filepath.Join(h2.state, "settings.json")
	if r.code() != "E_CONFIG" || r.message() != "The requested profile does not exist: there is no settings file." ||
		r.details()["next_action"] != "Create "+path+" with this profile, or run without --profile." ||
		r.details()["profile"] != "ghost-SENTINEL" || r.details()["source"] != "env" {
		t.Fatalf("no settings file: %s", r)
	}
}

// D6: a suggested command line never shows a secret flag's value, however given.
func TestD6SuggestionsNeverEchoSecrets(t *testing.T) {
	h := newHarness(t)
	pingApp(h, func(a *App, cmd *cobra.Command) error { return a.OK(cmd, map[string]any{}) })
	for _, args := range [][]string{
		{"ping", "-k", "SHORT-SENTINEL-1", "--hostt", "gitlab.example.com"},
		{"ping", "-k=SHORT-SENTINEL-2", "--hostt", "gitlab.example.com"},
		{"ping", "-kATTACHED-SENTINEL-3", "--hostt", "gitlab.example.com"},
		{"ping", "--token", "-DASH-SENTINEL-4", "--hostt", "gitlab.example.com"},
		{"ping", "--token=LONG-SENTINEL-5", "--hostt", "gitlab.example.com"},
		{"ping", "--token", "LONG-SENTINEL-6", "--hostt", "gitlab.example.com"},
	} {
		r := h.run(args...)
		next, _ := r.details()["next_action"].(string)
		if r.details()["problem"] != "unknown_flag" || strings.Contains(r.stdout, "SENTINEL") || !strings.Contains(next, "***") ||
			!strings.Contains(next, "--host gitlab.example.com") {
			t.Fatalf("%v: %s", args, r)
		}
	}
}

// D7: Debugf in a PersistentPreRunE knows the running command's secrets.
func TestD7DebugInPersistentPreRunIsRedacted(t *testing.T) {
	h := newHarness(t)
	pingApp(h, func(a *App, cmd *cobra.Command) error { return a.OK(cmd, map[string]any{}) })
	build := h.build
	h.build = func(u *upstream) *App {
		a := build(u)
		a.Root.PersistentPreRunE = func(cmd *cobra.Command, _ []string) error {
			if f := cmd.Flags().Lookup("token"); f != nil {
				a.Debugf("client setup: token=%s", f.Value.String())
			}
			return nil
		}
		return a
	}
	h.env["NOTES_DEBUG"] = "1"
	r := h.run("ping", "--token", "PPR-SENTINEL")
	if r.exit != 0 || strings.Contains(r.stderr, "PPR-SENTINEL") || !strings.Contains(r.stderr, "debug: client setup: token=***") {
		t.Fatalf("stderr: %q", r.stderr)
	}
}

// D10: the parity details of settings, profiles and debug.
func TestD10ParityDetails(t *testing.T) {
	h := settingsHarness(t)
	writeSettings(t, h, `{"default_profile": "work", "profiles": {"work": {}, "home": {}}}`)
	h.env["NOTES_PROFILE"] = "home"
	if p := contextProfile(t, h, "--profile", ""); p["name"] != "home" || p["source"] != "env" {
		t.Fatalf(`--profile "" counts as not given: %v`, p)
	}
	delete(h.env, "NOTES_PROFILE")
	path := writeSettings(t, h, `{"default_profile": "", "profiles": {"work": {}}}`)
	if c := checkNamed(h.run("doctor"), "settings"); c["status"] != "fail" ||
		c["message"] != "The settings file "+path+" is invalid: default_profile `` is not one of its profiles." {
		t.Fatalf(`default_profile "": %v`, c)
	}
	_ = os.Remove(path)
	for value, on := range map[string]bool{" ": false, " 0 ": false, "0": false, " 1": true} {
		h.env["NOTES_DEBUG"] = value
		if r := h.run("context"); strings.Contains(r.stderr, "debug:") != on {
			t.Fatalf("NOTES_DEBUG=%q: %q", value, r.stderr)
		}
	}
	delete(h.env, "NOTES_DEBUG")
	dir := filepath.Join(h.dir, "settings-dir")
	_ = os.MkdirAll(dir, 0o700)
	h.env["NOTES_SETTINGS_FILE"] = dir
	if c := checkNamed(h.run("doctor"), "settings"); c["message"] != "The settings file "+dir+" is invalid: it is not a regular file." {
		t.Fatalf("a directory: %v", c)
	}
	real := filepath.Join(h.dir, "real.json")
	mustWrite(t, real, []byte(`{"profiles": {"work": {}}}`))
	link := filepath.Join(h.dir, "link.json")
	if err := os.Symlink(real, link); err != nil {
		t.Logf("no symlink here (%v); the link case is not exercised", err)
		return
	}
	h.env["NOTES_SETTINGS_FILE"] = link
	if c := checkNamed(h.run("doctor"), "settings"); c["message"] != "The settings file "+link+" is invalid: it must be a regular file, not a link." {
		t.Fatalf("a link: %v", c)
	}
}

// D11: a line break in a debug message stays on its debug: line; redactHost drops
// user info with or without a scheme.
func TestD11DebugLinesAndHostRedaction(t *testing.T) {
	h := newHarness(t)
	pingApp(h, func(a *App, cmd *cobra.Command) error {
		a.Debugf("first\nsecond\rthird")
		return a.OK(cmd, map[string]any{})
	})
	h.env["NOTES_DEBUG"] = "1"
	r := h.run("ping")
	for _, line := range strings.Split(strings.TrimSpace(r.stderr), "\n") {
		if !strings.HasPrefix(line, "debug: ") {
			t.Fatalf("a line that does not start with debug: %q", line)
		}
	}
	if !strings.Contains(r.stderr, `debug: first\nsecond\rthird`) {
		t.Fatalf("stderr: %q", r.stderr)
	}
	for in, want := range map[string]string{
		"user:tok@gitlab.example.com/x":         "gitlab.example.com/x",
		"https://user:tok@gitlab.example.com/x": "https://gitlab.example.com/x",
		"tok@host:8443":                         "host:8443",
		"https://gitlab.example.com/?t=1#f":     "https://gitlab.example.com/",
		"gitlab.example.com":                    "gitlab.example.com",
	} {
		if got := redactHost(in); got != want {
			t.Errorf("redactHost(%q) = %q, want %q", in, got, want)
		}
	}
}

// ---------------------------------------------------------------- F. vocabulary and doctor

// F4: operation list filters with --state (CLI-028); --status is gone.
func TestF4OperationListTakesState(t *testing.T) {
	h := newHarness(t)
	if r := h.run("note", "update", "1", "--title", "X", "--confirm", h.mustToken("note", "update", "1", "--title", "X")); r.exit != 0 {
		t.Fatalf("write: %s", r)
	}
	if r := h.run("operation", "list", "--state", "applied"); r.exit != 0 || len(r.data()["items"].([]any)) != 1 ||
		r.data()["scope"].(map[string]any)["state"] != "applied" {
		t.Fatalf("--state applied: %s", r)
	}
	if r := h.run("operation", "list", "--status", "applied"); r.details()["problem"] != "unknown_flag" {
		t.Fatalf("--status: %s", r)
	}
	ref := h.run("reference", "--command", "operation list")
	if !strings.Contains(ref.stdout, `"name":"state"`) || strings.Contains(ref.stdout, `"name":"status"`) ||
		!strings.Contains(ref.stdout, "notes operation list --state unknown") {
		t.Fatalf("reference: %s", ref)
	}
}
