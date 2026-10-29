// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// TestReceiptRules covers every row of contract.json receipt_rules, in order (9 rows).
func TestReceiptRules(t *testing.T) {
	ok := Outcome{Execution: "completed", Change: "applied", Delivery: "not_applicable", Persistence: "not_applicable", Verification: "passed"}
	with := func(f func(*Outcome)) Outcome {
		o := ok
		f(&o)
		return o
	}
	cases := []struct {
		name  string
		o     Outcome
		cause string
		ok    bool
		code  string
	}{
		// rule 1: anything unknown, or running
		{"r1 execution unknown", with(func(o *Outcome) { o.Execution = "unknown" }), "", false, "E_OUTCOME_UNKNOWN"},
		{"r1 change unknown", with(func(o *Outcome) { o.Change = "unknown" }), "", false, "E_OUTCOME_UNKNOWN"},
		{"r1 delivery unknown", with(func(o *Outcome) { o.Delivery = "unknown" }), "", false, "E_OUTCOME_UNKNOWN"},
		{"r1 persistence unknown", with(func(o *Outcome) { o.Persistence = "unknown" }), "", false, "E_OUTCOME_UNKNOWN"},
		{"r1 running", with(func(o *Outcome) { o.Execution = "running" }), "", false, "E_OUTCOME_UNKNOWN"},
		{"r1 beats r2", with(func(o *Outcome) { o.Execution = "unknown"; o.Change = "partial" }), "", false, "E_OUTCOME_UNKNOWN"},
		// rule 2: anything partial
		{"r2 change partial", with(func(o *Outcome) { o.Change = "partial" }), "", false, "E_PARTIAL"},
		{"r2 delivery partial", with(func(o *Outcome) { o.Delivery = "partial" }), "", false, "E_PARTIAL"},
		{"r2 persistence partial", with(func(o *Outcome) { o.Persistence = "partial" }), "", false, "E_PARTIAL"},
		// rule 3: failed execution with a lasting effect
		{"r3 failed + applied", with(func(o *Outcome) { o.Execution = "failed" }), "E_SERVER", false, "E_PARTIAL"},
		{"r3 interrupted + delivered", with(func(o *Outcome) { o.Execution = "interrupted"; o.Change = "not_applied"; o.Delivery = "delivered" }), "", false, "E_PARTIAL"},
		{"r3 not_started + saved", with(func(o *Outcome) { o.Execution = "not_started"; o.Change = "not_applied"; o.Persistence = "saved" }), "", false, "E_PARTIAL"},
		// rule 4: failed execution without a lasting effect
		{"r4 failed with cause", with(func(o *Outcome) { o.Execution = "failed"; o.Change = "not_applied" }), "E_VALIDATION", false, "E_VALIDATION"},
		{"r4 failed default", with(func(o *Outcome) { o.Execution = "failed"; o.Change = "not_applied" }), "", false, "E_SERVER"},
		{"r4 not_started", with(func(o *Outcome) { o.Execution = "not_started"; o.Change = "not_applied" }), "E_NETWORK", false, "E_NETWORK"},
		{"r4 interrupted rolled back", with(func(o *Outcome) { o.Execution = "interrupted"; o.Change = "rolled_back" }), "", false, "E_SERVER"},
		// rule 5: the change was rolled back
		{"r5 rolled back default", with(func(o *Outcome) { o.Change = "rolled_back" }), "", false, "E_VERIFY_FAILED"},
		{"r5 rolled back with cause", with(func(o *Outcome) { o.Change = "rolled_back" }), "E_CONFLICT", false, "E_CONFLICT"},
		{"r5 beats r6", with(func(o *Outcome) { o.Change = "rolled_back"; o.Delivery = "rejected" }), "", false, "E_VERIFY_FAILED"},
		// rule 6: delivery rejected
		{"r6 rejected default", with(func(o *Outcome) { o.Change = "not_applicable"; o.Delivery = "rejected" }), "", false, "E_FORBIDDEN"},
		{"r6 rejected with cause", with(func(o *Outcome) { o.Change = "not_applicable"; o.Delivery = "rejected" }), "E_VALIDATION", false, "E_VALIDATION"},
		// rule 7, 8: verification
		{"r7 verify failed", with(func(o *Outcome) { o.Verification = "failed" }), "", false, "E_VERIFY_FAILED"},
		{"r8 verify incomplete", with(func(o *Outcome) { o.Verification = "incomplete" }), "", false, "E_VERIFY_INCOMPLETE"},
		// rule 9: otherwise ok (base profile: not_run with applied is ok)
		{"r9 passed", ok, "", true, ""},
		{"r9 not_run", with(func(o *Outcome) { o.Verification = "not_run" }), "", true, ""},
		{"r9 not_applicable", with(func(o *Outcome) {
			o.Change = "not_applicable"
			o.Delivery = "delivered"
			o.Verification = "not_applicable"
		}), "", true, ""},
	}
	for _, c := range cases {
		gotOK, gotCode := deriveReceipt(c.o, c.cause)
		if gotOK != c.ok || gotCode != c.code {
			t.Errorf("%s: got ok=%v code=%q, want ok=%v code=%q", c.name, gotOK, gotCode, c.ok, c.code)
		}
	}
}

func TestReceiptRuleCountMatchesTheContract(t *testing.T) {
	b, err := os.ReadFile(filepath.Join("..", "..", "common", "contract", "contract.json"))
	if err != nil {
		t.Skipf("contract.json not available here: %v", err)
	}
	var c struct {
		ReceiptRules struct {
			Rules []map[string]any `json:"rules"`
		} `json:"receipt_rules"`
	}
	if err := json.Unmarshal(b, &c); err != nil {
		t.Fatal(err)
	}
	if len(c.ReceiptRules.Rules) != receiptRuleCount || receiptRuleCount != 9 {
		t.Fatalf("contract has %d receipt rules; deriveReceipt implements 9", len(c.ReceiptRules.Rules))
	}
}

func TestOutcomeNormalisationFailsClosed(t *testing.T) {
	m := &Meta{Effects: []Effect{Change, Deliver}}
	o := normalizeOutcome(Outcome{Change: "done", Delivery: "sent", Verification: "ok"}, m)
	if o.Change != "unknown" || o.Delivery != "unknown" || o.Verification != "incomplete" {
		t.Fatalf("invalid values must fail closed: %+v", o)
	}
	if ok, code := deriveReceipt(o, ""); ok || code != "E_OUTCOME_UNKNOWN" {
		t.Fatalf("got %v %s", ok, code)
	}
	d := normalizeOutcome(Outcome{}, m)
	if d.Execution != "completed" || d.Change != "applied" || d.Delivery != "delivered" || d.Persistence != "not_applicable" {
		t.Fatalf("defaults: %+v", d)
	}
}

func TestAutoVerify(t *testing.T) {
	changes := []any{
		map[string]any{"field": "title", "after": "New"},
		map[string]any{"field": "tags", "add": []any{"x"}},
		map[string]any{"field": "labels", "remove": []any{"old"}},
	}
	v, mm := autoVerify(changes, map[string]any{"title": "New", "tags": []any{"a", "x"}, "labels": []any{"keep"}})
	if v != "passed" || len(mm) != 0 {
		t.Fatalf("got %s %v", v, mm)
	}
	v, mm = autoVerify(changes, map[string]any{"title": "Old", "tags": []any{"a"}, "labels": []any{"old"}})
	if v != "failed" || len(mm) != 3 {
		t.Fatalf("got %s %v", v, mm)
	}
	if v, _ := autoVerify(changes, map[string]any{}); v != "not_run" {
		t.Fatalf("nothing comparable must be not_run, got %s", v)
	}
}
