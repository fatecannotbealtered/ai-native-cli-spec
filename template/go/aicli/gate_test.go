// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/spf13/cobra"
)

func TestBareWriteIsRefusedWithPreviewAndToken(t *testing.T) {
	h := newHarness(t)
	r := h.run("note", "update", "1", "--title", "New")
	if r.exit != 5 || r.code() != "E_CONFIRMATION_REQUIRED" {
		t.Fatalf("want exit 5 E_CONFIRMATION_REQUIRED, got %s", r)
	}
	d := r.details()
	if d["problem"] != "confirmation_required" || d["preview"] == nil || d["operation_id"] == nil || !isToken(d["confirm_token"]) {
		t.Fatalf("refusal lacks preview/token/operation_id: %s", r)
	}
	if !strings.Contains(d["next_action"].(string), r.token()) {
		t.Fatalf("next_action should carry the token: %v", d["next_action"])
	}
	if h.up.Writes() != 0 {
		t.Fatal("a bare call wrote upstream")
	}
	// The refusal's token confirms directly (no extra dry-run round trip).
	c := h.run("note", "update", "1", "--title", "New", "--confirm", r.token())
	if c.exit != 0 || h.up.Writes() != 1 {
		t.Fatalf("confirm from the refusal failed: %s", c)
	}
	if n, _ := h.up.get("1"); n.Title != "New" {
		t.Fatalf("title not changed: %+v", n)
	}
}

func TestDryRunReturnsPreviewAndChangesNothing(t *testing.T) {
	h := newHarness(t)
	r := h.run("note", "update", "1", "--title", "New", "--dry-run")
	if r.exit != 0 || !isToken(r.token()) {
		t.Fatalf("dry-run failed: %s", r)
	}
	p := r.data()["preview"].(map[string]any)
	if p["command"] != "note update" || p["can_apply"] != true || p["base"] == nil {
		t.Fatalf("unexpected preview: %v", p)
	}
	inputs := p["inputs"].(map[string]any)
	if inputs["title"] != "New" || inputs["id"] != "1" {
		t.Fatalf("preview inputs: %v", inputs)
	}
	if h.up.Writes() != 0 {
		t.Fatal("dry-run wrote upstream")
	}
	untrusted := r.env["meta"].(map[string]any)["untrusted"]
	if !listHasString(untrusted, "data.preview.changes[].before") {
		t.Fatalf("preview untrusted paths not declared: %v", untrusted)
	}
}

func TestConfirmRunsOnceThenTokenUsed(t *testing.T) {
	h := newHarness(t)
	tok := h.mustToken("note", "create", "--title", "Hello")
	r := h.run("note", "create", "--title", "Hello", "--confirm", tok)
	if r.exit != 0 || h.up.Writes() != 1 {
		t.Fatalf("confirm failed: %s", r)
	}
	rec := r.data()["receipt"].(map[string]any)
	out := rec["outcome"].(map[string]any)
	if out["execution"] != "completed" || out["change"] != "applied" || out["verification"] != "passed" || rec["replayed"] != false {
		t.Fatalf("receipt: %v", rec)
	}
	again := h.run("note", "create", "--title", "Hello", "--confirm", tok)
	expectConflict(t, again, "token_used")
	if h.up.Writes() != 1 {
		t.Fatal("a used token wrote again")
	}
	// operation show reports the applied record.
	id := r.data()["operation_id"].(string)
	show := h.run("operation", "show", id)
	if show.exit != 0 || show.data()["status"] != "applied" || show.data()["receipt"] == nil {
		t.Fatalf("operation show: %s", show)
	}
}

func TestConcurrentConfirmsFromGoroutinesWriteOnce(t *testing.T) {
	h := newHarness(t)
	tok := h.mustToken("note", "update", "2", "--title", "Raced")
	const n = 16
	var wg sync.WaitGroup
	results := make([]tout, n)
	start := make(chan struct{})
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			results[i] = h.run("note", "update", "2", "--title", "Raced", "--confirm", tok)
		}(i)
	}
	close(start)
	wg.Wait()
	ok := 0
	for _, r := range results {
		switch {
		case r.exit == 0:
			ok++
		case r.code() == "E_CONFLICT" && r.details()["conflict"] == "token_used":
		default:
			t.Errorf("unexpected result: %s", r)
		}
	}
	if ok != 1 || h.up.Writes() != 1 {
		t.Fatalf("want exactly one success and one write, got %d successes, %d writes", ok, h.up.Writes())
	}
}

func TestTokenExpires(t *testing.T) {
	h := newHarness(t)
	now := time.Now().UTC()
	h.now = func() time.Time { return now }
	tok := h.mustToken("note", "update", "1", "--title", "Late")
	now = now.Add(tokenLifetime + time.Second)
	h.grant(map[string]any{"expires_at": timeString(now.Add(time.Hour))})
	// keep the scope digest equal to the dry-run's: restore the original file content
	// is not needed here because expiry is checked first.
	r := h.run("note", "update", "1", "--title", "Late", "--confirm", tok)
	expectConflict(t, r, "token_expired")
	if h.up.Writes() != 0 {
		t.Fatal("an expired token wrote")
	}
	// The conflict carries a fresh plan, so one more call applies it.
	fresh, _ := r.details()["confirm_token"].(string)
	if fresh == "" || fresh == tok || r.details()["preview"] == nil || r.details()["operation_id"] == nil {
		t.Fatalf("token_expired should carry a new preview and token: %s", r)
	}
	if na, _ := r.details()["next_action"].(string); !strings.Contains(na, fresh) {
		t.Fatalf("next_action should name the new token: %s", r)
	}
	r = h.run("note", "update", "1", "--title", "Late", "--confirm", fresh)
	if r.exit != 0 || h.up.Writes() != 1 {
		t.Fatalf("the fresh token should apply: %s", r)
	}
}

func TestTextModeCannotBypassTheGate(t *testing.T) {
	h := newHarness(t)
	r := h.run("note", "update", "1", "--title", "X", "--format", "text")
	if r.exit != 5 || h.up.Writes() != 0 {
		t.Fatalf("text mode bypassed the gate: %s", r)
	}
	if !strings.Contains(r.stderr, "E_CONFIRMATION_REQUIRED") || !strings.Contains(r.stderr, "ct2_") {
		t.Fatalf("text refusal should show the code and token: %q", r.stderr)
	}
	for _, mode := range [][]string{{"--pretty"}, {"--quiet"}, {"--compact"}, {"--json"}, {"--format", "json"}} {
		r = h.run(append([]string{"note", "update", "1", "--title", "X"}, mode...)...)
		if r.exit != 5 || h.up.Writes() != 0 {
			t.Fatalf("%v bypassed the gate: %s", mode, r)
		}
	}
	r = h.run("note", "update", "1", "--title", "X", "--format", "raw")
	if r.exit != 2 || r.details()["problem"] != "unsupported_format" || h.up.Writes() != 0 {
		t.Fatalf("raw on a non-raw write: %s", r)
	}
	// a valid token still works in text mode
	tok := h.mustToken("note", "update", "1", "--title", "X")
	r = h.run("note", "update", "1", "--title", "X", "--confirm", tok, "--format", "text")
	if r.exit != 0 || h.up.Writes() != 1 {
		t.Fatalf("text-mode confirm failed: %s", r)
	}
}

func TestDangerousNeedsTheFlagInBothSteps(t *testing.T) {
	h := newHarness(t)
	r := h.run("note", "delete", "1", "--dry-run")
	if r.exit != 5 || r.details()["problem"] != "dangerous_flag_required" || r.token() != "" {
		t.Fatalf("dry-run without --dangerous: %s", r)
	}
	// The refusal shows the plan (read-only, no token) so it can go to the user at once.
	if p, ok := r.details()["preview"].(map[string]any); !ok || p["command"] != "note delete" || hasKey(r.details(), "confirm_token") {
		t.Fatalf("the refusal should carry the preview and no token: %s", r)
	}
	if na, _ := r.details()["next_action"].(string); !strings.Contains(na, "--dangerous") {
		t.Fatalf("next_action should name --dangerous: %s", r)
	}
	r = h.run("note", "delete", "1")
	if r.exit != 5 || r.details()["problem"] != "dangerous_flag_required" {
		t.Fatalf("bare call without --dangerous: %s", r)
	}
	tok := h.mustToken("note", "delete", "1", "--dangerous")
	r = h.run("note", "delete", "1", "--confirm", tok)
	if r.exit != 5 || r.details()["problem"] != "dangerous_flag_required" || h.up.Writes() != 0 {
		t.Fatalf("confirm without --dangerous: %s", r)
	}
	r = h.run("note", "delete", "1", "--confirm", tok, "--dangerous")
	if r.exit != 0 || h.up.Writes() != 1 {
		t.Fatalf("confirm with --dangerous: %s", r)
	}
	// deliver is dangerous by default
	r = h.run("note", "share", "2", "--to", "bob@example.com", "--dry-run")
	if r.exit != 5 || r.details()["problem"] != "dangerous_flag_required" {
		t.Fatalf("deliver must be dangerous: %s", r)
	}
}

func TestStateDriftRefusesTheConfirm(t *testing.T) {
	h := newHarness(t)
	tok := h.mustToken("note", "update", "1", "--title", "Mine")
	h.up.bump("1")
	r := h.run("note", "update", "1", "--title", "Mine", "--confirm", tok)
	expectConflict(t, r, "state_drift")
	if h.up.Writes() != 0 {
		t.Fatal("a drifted target was written")
	}
	fresh, _ := r.details()["confirm_token"].(string)
	if fresh == "" || fresh == tok || r.details()["preview"] == nil {
		t.Fatalf("state_drift should carry a new preview and token: %s", r)
	}
	if r = h.run("note", "update", "1", "--title", "Mine", "--confirm", tok); r.code() != "E_CONFLICT" || h.up.Writes() != 0 {
		t.Fatalf("the drifted token must stay refused: %s", r)
	}
	r = h.run("note", "update", "1", "--title", "Mine", "--confirm", fresh)
	if r.exit != 0 || h.up.Writes() != 1 {
		t.Fatalf("the fresh token should apply: %s", r)
	}
}

func TestScopeChangedRefusesTheConfirm(t *testing.T) {
	h := newHarness(t)
	tok := h.mustToken("note", "update", "1", "--title", "Mine")
	h.grant(map[string]any{"notes": "edited"})
	r := h.run("note", "update", "1", "--title", "Mine", "--confirm", tok)
	expectConflict(t, r, "scope_changed")
	if h.up.Writes() != 0 {
		t.Fatal("a changed scope still wrote")
	}
}

func TestDryRunAndConfirmTogetherIsUsage(t *testing.T) {
	h := newHarness(t)
	tok := h.mustToken("note", "update", "1", "--title", "X")
	r := h.run("note", "update", "1", "--title", "X", "--dry-run", "--confirm", tok)
	if r.exit != 2 || r.details()["problem"] != "conflicting_flags" || h.up.Writes() != 0 {
		t.Fatalf("got %s", r)
	}
	if na, _ := r.details()["next_action"].(string); !strings.HasPrefix(na, "Drop --dry-run") {
		t.Fatalf("next_action should say to drop --dry-run: %s", r)
	}
}

func TestBlockedPreviewIssuesNoToken(t *testing.T) {
	h := newHarness(t)
	r := h.run("note", "update", "1", "--dry-run")
	if r.exit != 0 || r.data()["confirm_token"] != nil || r.data()["expires_at"] != nil {
		t.Fatalf("a blocked preview must return a null token: %s", r)
	}
	p := r.data()["preview"].(map[string]any)
	if p["can_apply"] != false || len(p["blocked_by"].([]any)) == 0 {
		t.Fatalf("preview: %v", p)
	}
	r = h.run("note", "update", "1")
	if r.exit != 5 || r.details()["confirm_token"] != nil {
		t.Fatalf("a blocked bare call: %s", r)
	}
}

func TestPreviewErrorsKeepTheirCodeAndSendNothing(t *testing.T) {
	h := newHarness(t)
	r := h.run("note", "update", "99", "--title", "X", "--dry-run")
	if r.exit != 3 || r.code() != "E_NOT_FOUND" || r.details()["dispatch_state"] != "not_started" {
		t.Fatalf("got %s", r)
	}
}

func TestApplyErrorClassification(t *testing.T) {
	cases := []struct {
		name     string
		hook     func(kind, id string) error
		code     string
		exit     int
		dispatch string
		status   string
		writes   int
	}{
		{"not dispatched", func(string, string) error {
			return NotDispatched(FromHTTP(0, "The upstream cannot be reached.", "", 0))
		}, "E_NETWORK", 7, "not_started", "not_applied", 0},
		{"rejected", func(string, string) error {
			return Rejected(FromHTTP(422, "The title is too long.", "title too long", 0))
		}, "E_VALIDATION", 2, "dispatched", "not_applied", 0},
		{"rejected without a code", func(string, string) error { return Rejected(errors.New("refused")) },
			"E_SERVER", 7, "dispatched", "not_applied", 0},
		{"connection reset", func(string, string) error { return errors.New("connection reset by peer") },
			"E_OUTCOME_UNKNOWN", 6, "unknown", "unknown", 0},
		{"plain HTTP 500", func(string, string) error { return FromHTTP(500, "Server error.", "", 0) },
			"E_OUTCOME_UNKNOWN", 6, "unknown", "unknown", 0},
		{"panic", func(string, string) error { panic("boom") }, "E_OUTCOME_UNKNOWN", 6, "unknown", "unknown", 0},
		{"deadline", func(string, string) error { return context.DeadlineExceeded }, "E_OUTCOME_UNKNOWN", 6, "unknown", "unknown", 0},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			h := newHarness(t)
			tok := h.mustToken("note", "update", "1", "--title", "X")
			h.up.hook = c.hook
			r := h.run("note", "update", "1", "--title", "X", "--confirm", tok)
			if r.exit != c.exit || r.code() != c.code || r.details()["dispatch_state"] != c.dispatch {
				t.Fatalf("want %s exit %d dispatch %s, got %s", c.code, c.exit, c.dispatch, r)
			}
			if h.up.Writes() != c.writes {
				t.Fatalf("writes %d", h.up.Writes())
			}
			id, _ := r.details()["operation_id"].(string)
			if id == "" {
				t.Fatalf("no operation_id in %s", r)
			}
			if c.code == "E_OUTCOME_UNKNOWN" {
				if !strings.Contains(r.details()["lookup"].(string), "operation show --id "+id) {
					t.Fatalf("lookup: %v", r.details()["lookup"])
				}
				// next_action names the read that shows whether the change landed.
				na, _ := r.details()["next_action"].(string)
				if !strings.Contains(na, "`notes note get 1`") || !strings.Contains(na, "operation resolve --id "+id) {
					t.Fatalf("next_action: %q", na)
				}
			}
			h.up.hook = nil
			show := h.run("operation", "show", "--id", id)
			if show.data()["status"] != c.status {
				t.Fatalf("record status %v, want %s", show.data()["status"], c.status)
			}
			again := h.run("note", "update", "1", "--title", "X", "--confirm", tok)
			expectConflict(t, again, "token_used")
		})
	}
}

func TestUnknownOutcomeBlocksTheTargetUntilResolved(t *testing.T) {
	h := newHarness(t)
	tok := h.mustToken("note", "update", "1", "--title", "X")
	h.up.hook = func(string, string) error { return errors.New("timeout after send") }
	r := h.run("note", "update", "1", "--title", "X", "--confirm", tok)
	if r.code() != "E_OUTCOME_UNKNOWN" {
		t.Fatalf("got %s", r)
	}
	h.up.hook = nil
	id := r.details()["operation_id"].(string)
	blocked := h.run("note", "update", "1", "--title", "Y", "--dry-run")
	expectConflict(t, blocked, "unresolved_operation")
	if blocked.details()["active_operation"] != id {
		t.Fatalf("active_operation: %v", blocked.details()["active_operation"])
	}
	// other targets are not blocked
	if other := h.run("note", "update", "2", "--title", "Y", "--dry-run"); other.exit != 0 {
		t.Fatalf("another target was blocked: %s", other)
	}
	list := h.run("operation", "list", "--state", "unknown")
	if list.exit != 0 || list.data()["count"].(interface{ String() string }).String() != "1" {
		t.Fatalf("operation list --state unknown: %s", list)
	}
	// resolving needs a scope covering the operation's effects
	h.scopeOn = false
	res := h.run("operation", "resolve", id, "--outcome", "not_applied", "--evidence", "checked the note")
	if res.exit != 4 || res.code() != "E_FORBIDDEN" {
		t.Fatalf("resolve without scope: %s", res)
	}
	h.scopeOn = true
	res = h.run("operation", "resolve", id, "--outcome", "not_applied", "--evidence", "the title is unchanged upstream")
	if res.exit != 0 || res.data()["status"] != "resolved" {
		t.Fatalf("resolve: %s", res)
	}
	res2 := h.run("operation", "resolve", id, "--outcome", "applied", "--evidence", "again")
	if res2.exit == 0 {
		t.Fatalf("resolving twice must fail: %s", res2)
	}
	if again := h.run("note", "update", "1", "--title", "Y", "--dry-run"); again.exit != 0 {
		t.Fatalf("target still blocked after resolve: %s", again)
	}
}

func TestResolveRefusesWhileTheLeaseIsLive(t *testing.T) {
	h := newHarness(t)
	tok := h.mustToken("note", "update", "1", "--title", "X")
	var id string
	h.up.hook = func(string, string) error {
		// While Apply runs, another process tries to resolve the operation.
		list := h.run("operation", "list", "--state", "dispatching")
		items := list.data()["items"].([]any)
		if len(items) != 1 {
			t.Errorf("want one dispatching operation, got %s", list)
			return nil
		}
		id = items[0].(map[string]any)["operation_id"].(string)
		r := h.run("operation", "resolve", id, "--outcome", "applied", "--evidence", "guess")
		if r.code() != "E_CONFLICT" || r.details()["conflict"] != "lock_held" || r.details()["active_operation"] != id {
			t.Errorf("resolve during the lease: %s", r)
		}
		blocked := h.run("note", "update", "1", "--title", "Z", "--dry-run")
		if blocked.details()["conflict"] != "unresolved_operation" {
			t.Errorf("a write during the lease must be blocked: %s", blocked)
		}
		return nil
	}
	r := h.run("note", "update", "1", "--title", "X", "--confirm", tok)
	if r.exit != 0 {
		t.Fatalf("confirm: %s", r)
	}
}

func TestExpiredLeaseReadsAsUnknown(t *testing.T) {
	h := newHarness(t)
	now := time.Now().UTC()
	h.now = func() time.Time { return now }
	tok := h.mustToken("note", "update", "1", "--title", "X")
	h.up.hook = func(string, string) error {
		now = now.Add(10 * time.Minute) // the process "hangs" past its lease
		list := h.run("operation", "list", "--state", "unknown")
		if list.data()["count"].(interface{ String() string }).String() != "1" {
			t.Errorf("an expired lease must read as unknown: %s", list)
		}
		return nil
	}
	if r := h.run("note", "update", "1", "--title", "X", "--confirm", tok); r.exit != 0 {
		t.Fatalf("confirm: %s", r)
	}
}

func TestVerificationFromReadBack(t *testing.T) {
	h := newHarness(t)
	tok := h.mustToken("note", "update", "1", "--title", "Exact")
	r := h.run("note", "update", "1", "--title", "Exact", "--confirm", tok)
	out := r.data()["receipt"].(map[string]any)["outcome"].(map[string]any)
	if out["verification"] != "passed" {
		t.Fatalf("verification: %v", out)
	}
	if !listHasString(r.env["meta"].(map[string]any)["untrusted"], "data.receipt.observed") {
		t.Fatalf("observed values must be declared untrusted: %v", r.env["meta"])
	}
}

func TestVerificationMismatchIsVerifyFailed(t *testing.T) {
	h := newHarness(t)
	tok := h.mustToken("note", "update", "1", "--title", "A very long title")
	h.up.mangle = func(s string) string { return s[:6] } // the upstream silently truncates
	r := h.run("note", "update", "1", "--title", "A very long title", "--confirm", tok)
	if r.exit != 1 || r.code() != "E_VERIFY_FAILED" || h.up.Writes() != 1 {
		t.Fatalf("want E_VERIFY_FAILED after one write, got %s", r)
	}
	rec := r.details()["receipt"].(map[string]any)
	if rec["outcome"].(map[string]any)["change"] != "applied" || !listHasString(rec["mismatched"], "title") {
		t.Fatalf("receipt: %v", rec)
	}
	// the change took effect, so the record is applied; its receipt says the
	// read-back failed
	show := h.run("operation", "show", r.details()["operation_id"].(string))
	if show.data()["status"] != "applied" {
		t.Fatalf("record status: %v", show.data()["status"])
	}
}

func TestIdempotencyKeyReplaysTheOriginalReceipt(t *testing.T) {
	h := newHarness(t)
	args := []string{"note", "create", "--title", "Once", "--idempotency-key", "k-1"}
	tok := h.mustToken(args...)
	// A token issued before the first confirm meets the applied operation at its own
	// confirm (a dry-run after it is answered with the replay at once, P4).
	tok2 := h.mustToken(args...)
	first := h.run(append(args, "--confirm", tok)...)
	if first.exit != 0 || h.up.Writes() != 1 {
		t.Fatalf("first: %s", first)
	}
	second := h.run(append(args, "--confirm", tok2)...)
	if second.exit != 0 || h.up.Writes() != 1 {
		t.Fatalf("replay wrote again: %s", second)
	}
	rec := second.data()["receipt"].(map[string]any)
	if rec["replayed"] != true || second.data()["operation_id"] != first.data()["operation_id"] {
		t.Fatalf("replay receipt: %v", second.data())
	}
	// The replay carries the original result, so the new note's ref survives a lost reply.
	res, _ := second.data()["result"].(map[string]any)
	if res["ref"] == nil || res["ref"] != first.data()["result"].(map[string]any)["ref"] {
		t.Fatalf("replay result: %v", second.data()["result"])
	}
	other := h.run("note", "create", "--title", "Different", "--idempotency-key", "k-1", "--dry-run")
	expectConflict(t, other, "idempotency_mismatch")
}

// A replay needs the same command, key, inputs and target (decision 32): the same
// key and inputs resolving to another target are idempotency_mismatch, never the
// other target's receipt.
func TestReplayRequiresTheSameTarget(t *testing.T) {
	h := newHarness(t)
	h.build = func(u *upstream) *App {
		a := testApp(u)
		c := a.commandByPath("note create")
		c.RunE = func(cmd *cobra.Command, _ []string) error {
			title, _ := cmd.Flags().GetString("title")
			// The collection comes from the environment and is not a bound input.
			key := a.Env("NOTES_COLLECTION")
			return a.Write(cmd, WriteSpec{
				Target:  Target{Key: key, Kind: "collection"},
				Preview: func(context.Context) (Preview, error) { return Preview{Action: "Create", CanApply: true}, nil },
				Apply: func(context.Context, Op) (Result, error) {
					u.write(func() {})
					return Result{Data: map[string]any{"title": title}}, nil
				},
			})
		}
		return a
	}
	args := []string{"note", "create", "--title", "Once", "--idempotency-key", "k-9"}
	h.env["NOTES_COLLECTION"] = "notes"
	if r := h.run(append(args, "--confirm", h.mustToken(args...))...); r.exit != 0 {
		t.Fatalf("first: %s", r)
	}
	h.env["NOTES_COLLECTION"] = "archive"
	r := h.run(append(args, "--dry-run")...)
	expectConflict(t, r, "idempotency_mismatch")
	if h.up.Writes() != 1 {
		t.Fatalf("writes: %d", h.up.Writes())
	}
}

func TestWriteGateRequiresRegisteredWriteCommand(t *testing.T) {
	h := newHarness(t)
	h.build = func(u *upstream) *App {
		a := testApp(u)
		get := a.commandByPath("note get")
		get.RunE = func(cmd *cobraCommand, _ []string) error {
			return a.Write(cmd, WriteSpec{Target: Target{Key: "notes/1"},
				Preview: func(context.Context) (Preview, error) { return Preview{CanApply: true}, nil },
				Apply: func(context.Context, Op) (Result, error) {
					u.write(func() {})
					return Result{}, nil
				}})
		}
		return a
	}
	r := h.run("note", "get", "1", "--confirm", "ct2_aaaaaaaaaaaaaaaaaaaaaaaaaa_00000000000000000000000000000000")
	if r.code() != "E_UNKNOWN" || h.up.Writes() != 0 {
		t.Fatalf("a read command used the gate: %s", r)
	}
}

func TestOKOnWriteCommandIsRefused(t *testing.T) {
	h := newHarness(t)
	h.build = func(u *upstream) *App {
		a := testApp(u)
		c := a.commandByPath("note create")
		c.RunE = func(cmd *cobraCommand, _ []string) error { return a.OK(cmd, map[string]any{"done": true}) }
		return a
	}
	r := h.run("note", "create", "--title", "X")
	if r.code() != "E_UNKNOWN" {
		t.Fatalf("a write that skipped the gate reported success: %s", r)
	}
}

func TestNoUnkeyedFallbackWhenTheKeyIsBroken(t *testing.T) {
	h := newHarness(t)
	if err := os.MkdirAll(h.state, 0o700); err != nil {
		t.Fatal(err)
	}
	for name, content := range map[string][]byte{"empty": {}, "short": []byte("short"), "long": make([]byte, 33)} {
		if err := os.WriteFile(filepath.Join(h.state, "confirm.key"), content, 0o600); err != nil {
			t.Fatal(err)
		}
		for _, args := range [][]string{{"--dry-run"}, {}} {
			r := h.run(append([]string{"note", "update", "1", "--title", "X"}, args...)...)
			if r.exit != 1 || r.code() != "E_IO" || strings.Contains(r.stdout, "ct2_") {
				t.Fatalf("%s key, %v: want E_IO and no token, got %s", name, args, r)
			}
		}
	}
	if h.up.Writes() != 0 {
		t.Fatal("wrote with a broken key")
	}
}

func TestKeyRemovedAfterDryRunRefusesTheConfirm(t *testing.T) {
	h := newHarness(t)
	tok := h.mustToken("note", "update", "1", "--title", "X")
	if err := os.Remove(filepath.Join(h.state, "confirm.key")); err != nil {
		t.Fatal(err)
	}
	r := h.run("note", "update", "1", "--title", "X", "--confirm", tok)
	expectMismatch(t, r, "--confirm")
	if _, err := os.Stat(filepath.Join(h.state, "confirm.key")); err == nil {
		t.Fatal("a confirm must not create a key")
	}
	// a fresh key cannot validate the old token either
	h.mustToken("note", "update", "2", "--title", "Y")
	r = h.run("note", "update", "1", "--title", "X", "--confirm", tok)
	expectMismatch(t, r, "--confirm")
	if h.up.Writes() != 0 {
		t.Fatal("wrote with a replaced key")
	}
}

func TestForgedTokenForAnotherRecordIsRejected(t *testing.T) {
	h := newHarness(t)
	t1 := h.mustToken("note", "update", "1", "--title", "A")
	t2 := h.mustToken("note", "update", "2", "--title", "B")
	// splice t2's id with t1's MAC, and t1's id with t2's MAC
	for _, forged := range []string{t2[:31] + t1[31:], t1[:31] + t2[31:]} {
		for _, args := range [][]string{{"1", "--title", "A"}, {"2", "--title", "B"}} {
			r := h.run(append(append([]string{"note", "update"}, args...), "--confirm", forged)...)
			if r.exit == 0 {
				t.Fatalf("a spliced token was accepted: %s", r)
			}
		}
	}
	if h.up.Writes() != 0 {
		t.Fatal("a forged token wrote")
	}
}

func TestTamperedRecordFailsTheMAC(t *testing.T) {
	h := newHarness(t)
	r := h.run("note", "update", "1", "--title", "A", "--dry-run")
	tok := r.token()
	id := r.data()["operation_id"].(string)
	path := filepath.Join(h.state, "operations", id+".json")
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	// An attacker without the key rewrites the bound claims to allow another title.
	tampered := strings.Replace(string(b), `"tool_version":"1.2.3"`, `"tool_version":"9.9.9"`, 1)
	if tampered == string(b) {
		t.Fatal("record layout changed; update the test")
	}
	if err := os.WriteFile(path, []byte(tampered), 0o600); err != nil {
		t.Fatal(err)
	}
	out := h.run("note", "update", "1", "--title", "A", "--confirm", tok)
	expectMismatch(t, out, "--confirm")
}

func TestMalformedTokenSaysWhatToDrop(t *testing.T) {
	h := newHarness(t)
	tok := h.mustToken("note", "update", "1", "--title", "X")
	for _, bad := range []string{"<" + tok + ">", strings.ToUpper(tok), " " + tok + " ", `"` + tok + `"`} {
		r := h.run("note", "update", "1", "--title", "X", "--confirm", bad)
		expectConflict(t, r, "token_mismatch")
		if na, _ := r.details()["next_action"].(string); !strings.Contains(na, "exactly as the preview issued it") {
			t.Fatalf("%q: next_action %q", bad, na)
		}
	}
	if h.up.Writes() != 0 {
		t.Fatal("a wrapped token wrote")
	}
	// Only the exact form is accepted, and it still applies once.
	if r := h.run("note", "update", "1", "--title", "X", "--confirm", tok); r.exit != 0 || h.up.Writes() != 1 {
		t.Fatalf("the exact token: %s", r)
	}
}

func TestFileAndLiteralMismatchExplainsTheBytes(t *testing.T) {
	h := newHarness(t)
	f := filepath.Join(h.dir, "body.txt")
	if err := os.WriteFile(f, []byte("Hello\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	tok := h.mustToken("note", "update", "1", "--body-file", f)
	r := h.run("note", "update", "1", "--body", "Hello", "--confirm", tok)
	expectConflict(t, r, "token_mismatch")
	if na, _ := r.details()["next_action"].(string); !strings.Contains(na, "6 bytes") || !strings.Contains(na, "--body-file") {
		t.Fatalf("next_action should explain the file's bytes: %s", r)
	}
	tok = h.mustToken("note", "update", "1", "--body", "Hello")
	r = h.run("note", "update", "1", "--body-file", f, "--confirm", tok)
	expectConflict(t, r, "token_mismatch")
	if na, _ := r.details()["next_action"].(string); !strings.Contains(na, "literal --body") {
		t.Fatalf("next_action should explain the literal: %s", r)
	}
}
