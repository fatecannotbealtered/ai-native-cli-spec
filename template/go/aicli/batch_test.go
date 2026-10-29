// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/spf13/cobra"
)

func summaryOf(t *testing.T, m map[string]any) map[string]int {
	t.Helper()
	s, _ := m["summary"].(map[string]any)
	out := map[string]int{}
	for k, v := range s {
		n, _ := v.(interface{ String() string })
		if n == nil {
			t.Fatalf("summary.%s is not a number: %v", k, v)
		}
		var i int
		for _, c := range n.String() {
			i = i*10 + int(c-'0')
		}
		out[k] = i
	}
	return out
}

func TestBatchAllSucceed(t *testing.T) {
	h := newHarness(t)
	dry := h.run("note", "bulk-tag", "--ids", "1,2,2,3", "--tag", "work", "--dry-run")
	if dry.exit != 0 {
		t.Fatalf("dry-run: %s", dry)
	}
	p := dry.data()["preview"].(map[string]any)
	if p["total"].(interface{ String() string }).String() != "3" || len(p["items"].([]any)) != 3 {
		t.Fatalf("batch preview must de-duplicate and list every target: %v", p)
	}
	if !strings.HasPrefix(dry.data()["operation_id"].(string), "opb_") {
		t.Fatalf("batch id: %v", dry.data()["operation_id"])
	}
	for _, it := range p["items"].([]any) {
		if !validOperationID(it.(map[string]any)["operation_id"].(string)) {
			t.Fatalf("item without an operation id: %v", it)
		}
	}
	r := h.run("note", "bulk-tag", "--ids", "1,2,2,3", "--tag", "work", "--confirm", dry.token())
	if r.exit != 0 || h.up.Writes() != 3 {
		t.Fatalf("confirm: %s", r)
	}
	s := summaryOf(t, r.data())
	if s["total"] != 3 || s["succeeded"] != 3 || s["failed"] != 0 || s["unknown"] != 0 || s["skipped"] != 0 {
		t.Fatalf("summary: %v", s)
	}
	for _, it := range r.data()["items"].([]any) {
		if it.(map[string]any)["ok"] != true {
			t.Fatalf("item: %v", it)
		}
	}
	again := h.run("note", "bulk-tag", "--ids", "1,2,2,3", "--tag", "work", "--confirm", dry.token())
	expectConflict(t, again, "token_used")
	show := h.run("operation", "show", dry.data()["operation_id"].(string))
	if show.data()["status"] != "applied" || len(show.data()["items"].([]any)) != 3 {
		t.Fatalf("batch record: %s", show)
	}
}

func TestBatchPartialIsNotOK(t *testing.T) {
	h := newHarness(t)
	tok := h.mustToken("note", "bulk-tag", "--ids", "1,2,3", "--tag", "x")
	h.up.hook = func(kind, id string) error {
		if id == "2" {
			return Rejected(FromHTTP(403, "Note 2 is locked.", "locked", 0))
		}
		return nil
	}
	r := h.run("note", "bulk-tag", "--ids", "1,2,3", "--tag", "x", "--confirm", tok)
	if r.exit != 1 || r.code() != "E_PARTIAL" {
		t.Fatalf("want E_PARTIAL, got %s", r)
	}
	s := summaryOf(t, r.details())
	if s["succeeded"] != 2 || s["failed"] != 1 || s["skipped"] != 0 {
		t.Fatalf("summary: %v", s)
	}
	items := r.details()["items"].([]any)
	if items[1].(map[string]any)["ok"] != false || items[1].(map[string]any)["error"].(map[string]any)["code"] != "E_FORBIDDEN" {
		t.Fatalf("item 2: %v", items[1])
	}
}

func TestBatchAllFailedUsesTheFirstItemsCode(t *testing.T) {
	h := newHarness(t)
	tok := h.mustToken("note", "bulk-tag", "--ids", "1,2", "--tag", "x")
	h.up.hook = func(string, string) error { return Rejected(FromHTTP(404, "Gone.", "", 0)) }
	r := h.run("note", "bulk-tag", "--ids", "1,2", "--tag", "x", "--confirm", tok)
	if r.exit != 3 || r.code() != "E_NOT_FOUND" || r.details()["dispatch_state"] != "dispatched" {
		t.Fatalf("got %s", r)
	}
}

func TestBatchUnknownItemMakesTheBatchUnknown(t *testing.T) {
	h := newHarness(t)
	tok := h.mustToken("note", "bulk-tag", "--ids", "1,2,3", "--tag", "x")
	h.up.hook = func(kind, id string) error {
		switch id {
		case "2":
			return errors.New("connection reset after send")
		case "3":
			return Rejected(FromHTTP(422, "Bad tag.", "", 0))
		}
		return nil
	}
	r := h.run("note", "bulk-tag", "--ids", "1,2,3", "--tag", "x", "--confirm", tok)
	if r.exit != 6 || r.code() != "E_OUTCOME_UNKNOWN" || r.details()["lookup"] == nil {
		t.Fatalf("got %s", r)
	}
	s := summaryOf(t, r.details())
	if s["succeeded"] != 1 || s["unknown"] != 1 || s["failed"] != 1 {
		t.Fatalf("summary: %v", s)
	}
	// the unknown item blocks its own target only
	h.up.hook = nil
	blocked := h.run("note", "update", "2", "--title", "Y", "--dry-run")
	expectConflict(t, blocked, "unresolved_operation")
	if ok := h.run("note", "update", "3", "--title", "Y", "--dry-run"); ok.exit != 0 {
		t.Fatalf("target 3 must not be blocked: %s", ok)
	}
}

func TestBatchStopsWhenContinueOnErrorIsFalse(t *testing.T) {
	h := newHarness(t)
	args := []string{"note", "bulk-tag", "--ids", "1,2,3", "--tag", "x", "--continue-on-error=false"}
	tok := h.mustToken(args...)
	h.up.hook = func(kind, id string) error {
		if id == "1" {
			return Rejected(FromHTTP(409, "Conflict.", "", 0))
		}
		return nil
	}
	r := h.run(append(args, "--confirm", tok)...)
	if r.code() != "E_CONFLICT" {
		t.Fatalf("got %s", r)
	}
	s := summaryOf(t, r.details())
	if s["failed"] != 1 || s["skipped"] != 2 || h.up.Writes() != 0 {
		t.Fatalf("summary %v writes %d", s, h.up.Writes())
	}
}

func TestBatchWithAMissingTargetCannotApply(t *testing.T) {
	h := newHarness(t)
	r := h.run("note", "bulk-tag", "--ids", "1,99", "--tag", "x", "--dry-run")
	if r.exit != 0 || r.data()["confirm_token"] != nil {
		t.Fatalf("a batch with a missing target must not get a token: %s", r)
	}
	p := r.data()["preview"].(map[string]any)
	blocked := p["blocked_by"].([]any)
	if p["can_apply"] != false || len(blocked) != 1 || !strings.Contains(blocked[0].(map[string]any)["message"].(string), "notes/99") {
		t.Fatalf("preview: %v", p)
	}
}

func TestEmptyBatchIsUsage(t *testing.T) {
	h := newHarness(t)
	r := h.run("note", "bulk-tag", "--tag", "x", "--dry-run")
	if r.exit != 2 || r.details()["problem"] != "missing_parameter" || r.details()["parameter"] != "--ids" {
		t.Fatalf("got %s", r)
	}
}

func TestBatchDriftRefusesTheWholeBatch(t *testing.T) {
	h := newHarness(t)
	tok := h.mustToken("note", "bulk-tag", "--ids", "1,2", "--tag", "x")
	h.up.bump("2")
	r := h.run("note", "bulk-tag", "--ids", "1,2", "--tag", "x", "--confirm", tok)
	expectConflict(t, r, "state_drift")
	if h.up.Writes() != 0 {
		t.Fatal("a drifted batch wrote")
	}
}

func TestLargeBatchErrorStaysWithinBudget(t *testing.T) {
	h := newHarness(t)
	h.up.mu.Lock()
	var ids []string
	for i := 10; i < 70; i++ {
		id := strings.Repeat("9", 1) + string(rune('0'+i/10)) + string(rune('0'+i%10))
		h.up.notes[id] = &tnote{ID: id, Ref: "notes/" + id, Title: "t", Version: 1}
		ids = append(ids, id)
	}
	h.up.mu.Unlock()
	args := []string{"note", "bulk-tag", "--ids", strings.Join(ids, ","), "--tag", "x"}
	bare := h.run(args...)
	if bare.exit != 5 || len([]rune(strings.TrimSpace(bare.stdout))) > errorBudgetChars {
		t.Fatalf("a large refusal must fit the error budget (%d chars): exit %d", len(bare.stdout), bare.exit)
	}
	if !isToken(bare.details()["confirm_token"]) {
		t.Fatalf("the shortened refusal must keep its token: %s", bare.stdout)
	}
	h.up.hook = func(string, string) error { return Rejected(FromHTTP(422, "Bad tag.", "", 0)) }
	r := h.run(append(args, "--confirm", bare.token())...)
	if r.code() != "E_VALIDATION" || len([]rune(strings.TrimSpace(r.stdout))) > errorBudgetChars {
		t.Fatalf("a large batch error must fit the budget: %d chars, %s", len(r.stdout), r.code())
	}
}

// manyNotes adds n notes (ids 100..) and returns their ids.
func manyNotes(h *tharness, n int) []string {
	h.up.mu.Lock()
	defer h.up.mu.Unlock()
	var ids []string
	for i := 0; i < n; i++ {
		id := itoa(100 + i)
		h.up.notes[id] = &tnote{ID: id, Ref: "notes/" + id, Title: "t", Version: 1}
		ids = append(ids, id)
	}
	return ids
}

// A large batch keeps to the success budget by the contract's shortening rules: the
// preview lists its first targets and items one to one with omitted, and the result
// drops the receipts of items that succeeded, then lists fewer items with omitted.
func TestLargeBatchSuccessStaysWithinBudget(t *testing.T) {
	h := newHarness(t)
	ids := manyNotes(h, 120)
	args := []string{"note", "bulk-tag", "--ids", strings.Join(ids, ","), "--tag", "x"}
	dry := h.run(append(args, "--dry-run")...)
	if dry.exit != 0 || len([]rune(strings.TrimSpace(dry.stdout))) > successBudgetChars {
		t.Fatalf("dry-run: exit %d, %d chars", dry.exit, len(dry.stdout))
	}
	p := dry.data()["preview"].(map[string]any)
	targets, items := p["targets"].([]any), p["items"].([]any)
	omitted, _ := intOf(p["omitted"])
	total, _ := intOf(p["total"])
	if total != 120 || len(targets) != len(items) || int64(len(targets))+omitted != total || omitted == 0 {
		t.Fatalf("shortened preview: total %d, %d targets, %d items, omitted %d", total, len(targets), len(items), omitted)
	}
	r := h.run(append(args, "--confirm", dry.token())...)
	if r.exit != 0 || h.up.Writes() != 120 || len([]rune(strings.TrimSpace(r.stdout))) > successBudgetChars {
		t.Fatalf("confirm: exit %d, writes %d, %d chars", r.exit, h.up.Writes(), len(r.stdout))
	}
	if s := summaryOf(t, r.data()); s["succeeded"] != 120 {
		t.Fatalf("summary counts every item: %v", s)
	}
	for _, it := range r.data()["items"].([]any) {
		if m := it.(map[string]any); m["receipt"] != nil || m["result"] != nil {
			t.Fatalf("a succeeded item keeps its receipt in operation show only: %v", m)
		}
	}
}

// A large failed batch lists the failed items first, all of them before any that
// succeeded, and counts the rest in omitted.
func TestLargeBatchErrorListsFailuresFirst(t *testing.T) {
	h := newHarness(t)
	ids := manyNotes(h, 60)
	args := []string{"note", "bulk-tag", "--ids", strings.Join(ids, ","), "--tag", "x"}
	tok := h.mustToken(args...)
	failing := map[string]bool{ids[40]: true, ids[55]: true}
	h.up.hook = func(_, id string) error {
		if failing[id] {
			return Rejected(FromHTTP(422, "Bad tag.", "", 0))
		}
		return nil
	}
	r := h.run(append(args, "--confirm", tok)...)
	if r.code() != "E_PARTIAL" || len([]rune(strings.TrimSpace(r.stdout))) > errorBudgetChars {
		t.Fatalf("want E_PARTIAL within the error budget: %s (%d chars)", r.code(), len(r.stdout))
	}
	items := r.details()["items"].([]any)
	omitted, _ := intOf(r.details()["omitted"])
	if len(items) < 2 || int64(len(items))+omitted != 60 {
		t.Fatalf("items %d + omitted %d must count every item", len(items), omitted)
	}
	for i, it := range items[:2] {
		if it.(map[string]any)["ok"] != false {
			t.Fatalf("item %d: the failed items come first: %v", i, it)
		}
	}
}

// Each batch item gets its own upstream idempotency key, <user key>/<first 16 hex of
// SHA-256(target key)>, while the user's key stays bound into the batch token
// (decision 31, A5): distinct per target and the same on every dry-run.
func TestBatchItemsGetTheirOwnIdempotencyKey(t *testing.T) {
	var mu sync.Mutex
	var keys, opIDs []string
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
						mu.Lock()
						keys, opIDs = append(keys, op.IdempotencyKey), append(opIDs, op.ID)
						mu.Unlock()
						return Result{}, nil
					},
				})
			}
			return a.WriteBatch(cmd, BatchSpec{Items: items})
		}
		return a
	}
	args := []string{"note", "bulk-tag", "--ids", "1,2,3", "--tag", "x", "--idempotency-key", "K1"}
	tok := h.mustToken(args...)
	if r := h.run("note", "bulk-tag", "--ids", "1,2,3", "--tag", "x", "--idempotency-key", "K2", "--confirm", tok); r.code() != "E_CONFLICT" {
		t.Fatalf("the user's key is bound into the batch token: %s", r)
	}
	if r := h.run(append(args, "--confirm", tok)...); r.exit != 0 {
		t.Fatalf("confirm: %s", r)
	}
	if len(keys) != 3 {
		t.Fatalf("keys: %v", keys)
	}
	seen := map[string]bool{}
	for i, k := range keys {
		want := "K1/" + sha256Hex([]byte("notes/" + []string{"1", "2", "3"}[i]))[:16]
		if k != want || seen[k] {
			t.Fatalf("item %d: key %q, want %s and distinct keys", i, k, want)
		}
		seen[k] = true
	}
	// Without a user key the items get none.
	keys, opIDs = nil, nil
	plain := []string{"note", "bulk-tag", "--ids", "1,2", "--tag", "x"}
	if r := h.run(append(plain, "--confirm", h.mustToken(plain...))...); r.exit != 0 || len(keys) != 2 || keys[0] != "" || keys[1] != "" {
		t.Fatalf("no key: %v %s", keys, r)
	}
}

// operation show of a large batch names every item and shortens its preview by the
// preview rule (first targets and items, one to one, with omitted) to fit the budget.
func TestLargeBatchOperationShowStaysWithinBudget(t *testing.T) {
	h := newHarness(t)
	ids := manyNotes(h, 40)
	args := []string{"note", "bulk-tag", "--ids", strings.Join(ids, ","), "--tag", "x"}
	dry := h.run(append(args, "--dry-run")...)
	h.run(append(args, "--confirm", dry.token())...)
	r := h.run("operation", "show", "--id", dry.data()["operation_id"].(string))
	if r.exit != 0 || len([]rune(strings.TrimSpace(r.stdout))) > successBudgetChars {
		t.Fatalf("operation show: exit %d, %d chars", r.exit, len(r.stdout))
	}
	if items := r.data()["items"].([]any); len(items) != 40 || hasKey(r.data(), "items_omitted") {
		t.Fatalf("operation show names every item: %d", len(items))
	}
	p := r.data()["preview"].(map[string]any)
	omitted, _ := intOf(p["omitted"])
	if omitted == 0 || int64(len(p["targets"].([]any)))+omitted != 40 || len(p["targets"].([]any)) != len(p["items"].([]any)) {
		t.Fatalf("shortened preview: %v targets, %v items, omitted %d", len(p["targets"].([]any)), len(p["items"].([]any)), omitted)
	}
}

// operation show of a batch still too large once its preview is shortened lists the
// items that need attention first, then the done ones, each group in input order, cut
// to fit; items_omitted counts the rest and a notice names operation list --batch,
// which pages through every item in input order (operation.shortening). An item whose
// record cannot be read is unknown, with a null target, so it needs attention too.
func TestLargeBatchOperationShowListsAttentionFirst(t *testing.T) {
	h := newHarness(t)
	ids := manyNotes(h, 300)
	args := []string{"note", "bulk-tag", "--ids", strings.Join(ids, ","), "--tag", "x"}
	tok := h.mustToken(args...)
	attention := []string{ids[7], ids[20], ids[150], ids[299]}
	h.up.hook = func(_, id string) error {
		switch id {
		case ids[7], ids[299]:
			return errors.New("connection reset after send")
		case ids[150]:
			return Rejected(FromHTTP(422, "Bad tag.", "", 0))
		}
		return nil
	}
	r := h.run(append(args, "--confirm", tok)...)
	batch, _ := r.details()["operation_id"].(string)
	if r.code() != "E_OUTCOME_UNKNOWN" || !strings.HasPrefix(batch, "opb_") {
		t.Fatalf("confirm: %s %v", r.code(), r.details()["operation_id"])
	}
	// The shortened batch error says where every item is listed.
	if na, _ := r.details()["next_action"].(string); !strings.Contains(na, "(Details were shortened to fit; `notes operation list --batch "+batch+"` lists every item.)") {
		t.Fatalf("next_action: %q", na)
	}
	// The record of the applied item for ids[20] goes missing.
	rec, _, err := (&journal{dir: filepath.Join(h.state, "operations")}).read(batch)
	if err != nil {
		t.Fatal(err)
	}
	lost := rec.Items[20]
	if err := os.Remove(filepath.Join(h.state, "operations", lost+".json")); err != nil {
		t.Fatal(err)
	}
	// keyOf is a listed item's target key, nil when its record cannot be read.
	keyOf := func(m map[string]any) any {
		if tg, ok := m["target"].(map[string]any); ok {
			return tg["key"]
		}
		return nil
	}
	wantKey := func(id string) any {
		if id == ids[20] {
			return nil
		}
		return "notes/" + id
	}
	// A cached update notice shares meta with the items_omitted notice; both fit.
	if err := h.app().RecordUpdateCheck("1.3.0", "info"); err != nil {
		t.Fatal(err)
	}
	show := h.run("operation", "show", "--id", batch)
	size := len([]rune(strings.TrimSpace(show.stdout)))
	if show.exit != 0 || size > successBudgetChars {
		t.Fatalf("operation show: exit %d, %d chars", show.exit, size)
	}
	if err := ValidateEnvelope([]byte(show.stdout)); err != nil {
		t.Fatalf("the shortened view does not validate: %v", err)
	}
	d := show.data()
	items := d["items"].([]any)
	omitted, _ := intOf(d["items_omitted"])
	if d["status"] != "unknown" || len(items) <= len(attention) || omitted != int64(300-len(items)) {
		t.Fatalf("status %v, %d items listed, items_omitted %d", d["status"], len(items), omitted)
	}
	want := append([]string{}, attention...)
	for _, id := range ids {
		if !containsStr(attention, id) {
			want = append(want, id)
		}
	}
	for i, it := range items {
		m := it.(map[string]any)
		if keyOf(m) != wantKey(want[i]) || (i < len(attention)) == (m["status"] == "applied") {
			t.Fatalf("item %d: %v, want notes/%s with the attention items first", i, m, want[i])
		}
		if want[i] == ids[20] && (m["operation_id"] != lost || m["status"] != "unknown") {
			t.Fatalf("the item whose record is missing: %v", m)
		}
	}
	command := "notes operation list --batch " + batch
	message := fmt.Sprintf("operation show lists %d of the batch's 300 items, those that need attention first; run `%s` to page through all of them.",
		len(items), command)
	types := map[string]map[string]any{}
	for _, n := range show.env["meta"].(map[string]any)["notices"].([]any) {
		types[n.(map[string]any)["type"].(string)] = n.(map[string]any)
	}
	if n := types["items_omitted"]; n == nil || n["severity"] != "info" || n["message"] != message || n["command"] != command ||
		types["update_available"] == nil {
		t.Fatalf("notices: %v", show.env["meta"])
	}
	t.Logf("operation show of 300 items: %d characters, %d items listed, items_omitted %d", size, len(items), omitted)

	// operation list --batch pages through every item in input order. A page of 200
	// rows would break the budget, so each page holds the rows that fit, says it was
	// truncated, and next_offset continues after them.
	var rows []any
	var perPage []int
	for offset := int64(0); ; {
		page := h.run("operation", "list", "--batch", strings.ToUpper(batch), "--limit", "200", "--offset", fmt.Sprint(offset))
		got := page.data()["items"].([]any)
		perPage = append(perPage, len(got))
		scope := page.data()["scope"].(map[string]any)
		total, _ := intOf(page.data()["total"])
		if page.exit != 0 || len(got) == 0 || len([]rune(strings.TrimSpace(page.stdout))) > successBudgetChars || page.data()["sort"] != "input_order" ||
			scope["batch"] != batch || scope["state"] != "every item of the batch" || scope["journal"] == nil || total != 300 {
			t.Fatalf("page at %d: exit %d, %d rows, sort %v, scope %v, total %v", offset, page.exit, len(got), page.data()["sort"], scope, page.data()["total"])
		}
		rows = append(rows, got...)
		if page.data()["has_more"] != true {
			if hasKey(page.data(), "truncated") || len(rows) != 300 {
				t.Fatalf("the last page at %d: %d rows in all, truncated %v", offset, len(rows), page.data()["truncated"])
			}
			break
		}
		next, _ := intOf(page.data()["next_offset"])
		if page.data()["truncated"] != true || page.data()["truncated_reason"] == nil || next != offset+int64(len(got)) {
			t.Fatalf("page at %d: %d rows, next_offset %v, truncated %v", offset, len(got), page.data()["next_offset"], page.data()["truncated"])
		}
		offset = next
	}
	t.Logf("operation list --batch --limit 200: rows per page %v", perPage)
	seen := map[any]bool{}
	for i, row := range rows {
		m := row.(map[string]any)
		if keyOf(m) != wantKey(ids[i]) || m["batch_id"] != batch || m["command"] != "note bulk-tag" || seen[m["operation_id"]] {
			t.Fatalf("row %d: %v", i, m)
		}
		seen[m["operation_id"]] = true
	}
	if m := rows[20].(map[string]any); m["operation_id"] != lost || m["status"] != "unknown" {
		t.Fatalf("the row of the item whose record is missing: %v", m)
	}
	// --state filters the rows, and total counts the filtered list.
	unknown := h.run("operation", "list", "--batch", batch, "--state", "unknown")
	got := unknown.data()["items"].([]any)
	total, _ := intOf(unknown.data()["total"])
	if len(got) != 3 || keyOf(got[0].(map[string]any)) != "notes/"+ids[7] || got[1].(map[string]any)["operation_id"] != lost ||
		keyOf(got[2].(map[string]any)) != "notes/"+ids[299] || total != 3 || unknown.data()["scope"].(map[string]any)["state"] != "unknown" {
		t.Fatalf("--state unknown: %s", unknown)
	}
	// A batch is resolved through its items: resolve points at the unknown ones.
	res := h.run("operation", "resolve", "--id", batch, "--outcome", "applied", "--evidence", "checked the notes")
	if res.code() != "E_VALIDATION" || res.details()["next_action"] != "Run `notes operation list --batch "+batch+" --state unknown` to see the items to resolve." {
		t.Fatalf("resolve of a batch: %s", res)
	}
}

// A batch whose view fits lists every item in input order, an item that needs
// attention in its place, without items_omitted or a notice.
func TestSmallBatchOperationShowListsEveryItemInInputOrder(t *testing.T) {
	h := newHarness(t)
	tok := h.mustToken("note", "bulk-tag", "--ids", "1,2,3", "--tag", "x")
	h.up.hook = func(_, id string) error {
		if id == "2" {
			return Rejected(FromHTTP(422, "Bad tag.", "", 0))
		}
		return nil
	}
	r := h.run("note", "bulk-tag", "--ids", "1,2,3", "--tag", "x", "--confirm", tok)
	show := h.run("operation", "show", "--id", r.details()["operation_id"].(string))
	items, _ := show.data()["items"].([]any)
	if show.exit != 0 || len(items) != 3 || hasKey(show.data(), "items_omitted") || hasKey(show.env["meta"].(map[string]any), "notices") {
		t.Fatalf("small batch: %s", show)
	}
	for i, id := range []string{"1", "2", "3"} {
		m := items[i].(map[string]any)
		if m["target"].(map[string]any)["key"] != "notes/"+id || (id == "2") == (m["status"] == "applied") {
			t.Fatalf("item %d: %v", i, m)
		}
	}
}

// operation list --batch takes a batch id only: an item id, a confirm token or a
// malformed id is E_USAGE, an unknown batch E_NOT_FOUND and an unreadable batch record
// E_IO. An item record that cannot be read is a row with status unknown and a null
// target; without --state a planned batch lists its planned items.
func TestOperationListBatchRefusalsAndUnreadableItems(t *testing.T) {
	h := newHarness(t)
	dry := h.run("note", "bulk-tag", "--ids", "1,2,3", "--tag", "x", "--dry-run")
	batch := dry.data()["operation_id"].(string)
	item := dry.data()["preview"].(map[string]any)["items"].([]any)[1].(map[string]any)["operation_id"].(string)
	for _, given := range []string{item, dry.token(), "../../etc/passwd", ""} {
		r := h.run("operation", "list", "--batch", given)
		if r.exit != 2 || r.code() != "E_USAGE" || r.details()["problem"] != "invalid_value" || r.details()["parameter"] != "--batch" ||
			r.details()["expected"] != "a batch id: opb_ followed by 26 characters a-z and 2-7" ||
			!strings.Contains(fmt.Sprint(r.details()["next_action"]), "`notes operation list --limit 5`") {
			t.Fatalf("--batch %q: %s", given, r)
		}
	}
	r := h.run("operation", "list", "--batch", "opb_zzzzzzzzzzzzzzzzzzzzzzzzzz")
	if r.exit != 3 || r.code() != "E_NOT_FOUND" || r.details()["next_action"] != "Run `notes operation list --limit 5` to see recorded operations." {
		t.Fatalf("unknown batch: %s", r)
	}
	if r := h.run("operation", "list", "--batch", batch); len(r.data()["items"].([]any)) != 3 || r.data()["has_more"] != false || r.data()["total"] != json.Number("3") {
		t.Fatalf("a planned batch lists its planned items: %s", r)
	}
	if err := os.Remove(filepath.Join(h.state, "operations", item+".json")); err != nil {
		t.Fatal(err)
	}
	r = h.run("operation", "list", "--batch", batch)
	rows := r.data()["items"].([]any)
	if len(rows) != 3 {
		t.Fatalf("every item has a row: %s", r)
	}
	row := rows[1].(map[string]any)
	if row["operation_id"] != item || row["status"] != "unknown" || row["target"] != nil || row["batch_id"] != batch || row["command"] != "note bulk-tag" {
		t.Fatalf("an unreadable item: %v", row)
	}
	if r := h.run("operation", "list", "--batch", batch, "--state", "planned"); len(r.data()["items"].([]any)) != 2 || r.data()["total"] != json.Number("2") {
		t.Fatalf("--state planned: %s", r)
	}
	mustWrite(t, filepath.Join(h.state, "operations", batch+".json"), []byte("{not json"))
	r = h.run("operation", "list", "--batch", batch)
	if r.code() != "E_IO" || !strings.Contains(fmt.Sprint(r.details()["next_action"]), "`notes operation show --id "+batch+"`") {
		t.Fatalf("an unreadable batch record: %s", r)
	}
}

// operation show of a batch with an item record that cannot be read lists that item
// with a null target and status unknown, and the batch status follows from the
// readable items; operation show of a batch record that cannot be read has null items
// and status unknown (CLI-058). Every such view passes ValidateEnvelope.
func TestOperationShowOfUnreadableBatchRecordsValidates(t *testing.T) {
	h := newHarness(t)
	operations := filepath.Join(h.state, "operations")
	show := func(id string) map[string]any {
		t.Helper()
		r := h.run("operation", "show", "--id", id)
		if r.exit != 0 {
			t.Fatalf("operation show: %s", r)
		}
		if err := ValidateEnvelope([]byte(r.stdout)); err != nil {
			t.Fatalf("operation show does not validate: %v\n%s", err, r.stdout)
		}
		return r.data()
	}
	// A planned batch whose second item record is missing.
	dry := h.run("note", "bulk-tag", "--ids", "1,2,3", "--tag", "x", "--dry-run")
	planned := dry.data()["operation_id"].(string)
	missing := dry.data()["preview"].(map[string]any)["items"].([]any)[1].(map[string]any)["operation_id"].(string)
	if err := os.Remove(filepath.Join(operations, missing+".json")); err != nil {
		t.Fatal(err)
	}
	d := show(planned)
	items := d["items"].([]any)
	if d["status"] != "planned" || len(items) != 3 || items[1].(map[string]any)["target"] != nil || items[1].(map[string]any)["status"] != "unknown" ||
		items[0].(map[string]any)["status"] != "planned" {
		t.Fatalf("a planned batch with a missing item record: %v", d)
	}
	// A dispatched batch whose first item record is corrupt.
	tok := h.mustToken("note", "bulk-tag", "--ids", "1,2", "--tag", "y")
	r := h.run("note", "bulk-tag", "--ids", "1,2", "--tag", "y", "--confirm", tok)
	batch := r.data()["operation_id"].(string)
	corrupt := r.data()["items"].([]any)[0].(map[string]any)["operation_id"].(string)
	mustWrite(t, filepath.Join(operations, corrupt+".json"), []byte("{not json"))
	d = show(batch)
	items = d["items"].([]any)
	if d["status"] != "applied" || items[0].(map[string]any)["target"] != nil || items[0].(map[string]any)["status"] != "unknown" ||
		items[1].(map[string]any)["status"] != "applied" {
		t.Fatalf("a dispatched batch with a corrupt item record: %v", d)
	}
	// The batch record itself cannot be read.
	mustWrite(t, filepath.Join(operations, batch+".json"), []byte("{not json"))
	d = show(batch)
	if d["status"] != "unknown" || d["items"] != nil || d["command"] != "unknown" || !hasKey(d, "items") {
		t.Fatalf("a corrupt batch record: %v", d)
	}
	// Resolving it needs a scope covering every write effect of the tool. The item list
	// stays lost: the resolve output and a later operation show have null items and
	// status resolved, and operation list --batch cannot list them (CLI-061).
	h.grant(map[string]any{"effects": []string{"change", "deliver", "artifact", "persist"}})
	res := h.run("operation", "resolve", "--id", batch, "--outcome", "applied", "--evidence", "checked notes 1 and 2")
	if res.exit != 0 {
		t.Fatalf("resolving a corrupt batch record: %s", res)
	}
	if err := ValidateEnvelope([]byte(res.stdout)); err != nil {
		t.Fatalf("operation resolve does not validate: %v\n%s", err, res.stdout)
	}
	for _, d := range []map[string]any{res.data(), show(batch)} {
		resolution, _ := d["resolution"].(map[string]any)
		if d["status"] != "resolved" || d["items"] != nil || !hasKey(d, "items") || resolution["outcome"] != "applied" {
			t.Fatalf("a resolved corrupt batch record: %v", d)
		}
	}
	if r := h.run("operation", "list", "--batch", batch); r.code() != "E_IO" ||
		!strings.Contains(fmt.Sprint(r.details()["next_action"]), "`notes operation show --id "+batch+"`") {
		t.Fatalf("operation list --batch of a batch whose items were lost: %s", r)
	}
	listed := false
	for _, row := range h.run("operation", "list").data()["items"].([]any) {
		if m := row.(map[string]any); m["operation_id"] == batch {
			listed = true
			if hasKey(m, "item_count") {
				t.Fatalf("a batch whose items were lost has no item count: %v", m)
			}
		}
	}
	if !listed {
		t.Fatal("operation list leaves out the resolved batch")
	}
}
