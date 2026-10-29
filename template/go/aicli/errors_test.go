// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"context"
	"testing"

	"github.com/spf13/cobra"
)

// dispatch_state says whether a state-changing request was sent: an upstream answer
// to a read, and a read that timed out or was interrupted, are not_started
// (decision 1). The gate reports dispatched for a write's Rejected apply error.
func TestReadErrorsAreNotStarted(t *testing.T) {
	for _, status := range []int{0, 400, 401, 403, 404, 409, 412, 413, 422, 429, 500, 503} {
		if e := FromHTTP(status, "The upstream refused.", "upstream text", 0); e.Details["dispatch_state"] != "not_started" {
			t.Errorf("FromHTTP(%d): dispatch_state %v", status, e.Details["dispatch_state"])
		}
	}
	for err, code := range map[error]string{context.DeadlineExceeded: "E_TIMEOUT", context.Canceled: "E_INTERRUPTED"} {
		if e := asError(err); e.Code != code || e.Details["dispatch_state"] != "not_started" {
			t.Errorf("%v: %s %v", err, e.Code, e.Details)
		}
	}
	h := newHarness(t)
	h.build = func(u *upstream) *App {
		a := testApp(u)
		a.commandByPath("note get").RunE = func(*cobra.Command, []string) error {
			return FromHTTP(404, "No note has that id.", `{"error":"not found"}`, 0)
		}
		return a
	}
	r := h.run("note", "get", "99")
	if r.code() != "E_NOT_FOUND" || r.details()["dispatch_state"] != "not_started" || r.details()["upstream_message"] == nil {
		t.Fatalf("a read's 404: %s", r)
	}
}

func TestAmbiguousCandidatesHaveRefAndLabel(t *testing.T) {
	e := Ambiguous("Two notes are called Groceries.", []Candidate{{Ref: "notes/1"}, {Ref: "notes/2", Label: "Groceries (old)"}, {Label: "no ref"}})
	list, _ := e.Details["candidates"].([]any)
	if e.Code != "E_AMBIGUOUS" || len(list) != 2 {
		t.Fatalf("candidates: %v", e.Details)
	}
	if first := list[0].(map[string]any); first["ref"] != "notes/1" || first["label"] != "notes/1" {
		t.Fatalf("a candidate without a label shows its ref: %v", first)
	}
	if len(e.Untrusted) != 1 || e.Untrusted[0] != "candidates[].label" {
		t.Fatalf("labels are external text: %v", e.Untrusted)
	}
	if one := Ambiguous("One note.", []Candidate{{Ref: "notes/1"}}); one.Code != "E_UNKNOWN" {
		t.Fatalf("one candidate is not ambiguous: %s", one.Code)
	}
}

// A page that breaks the completeness rules (CLI-041) is a bug in the tool: E_UNKNOWN,
// never a schema-invalid page.
func TestPageCompletenessRules(t *testing.T) {
	off, neg, one := 5, -1, 1
	items := []map[string]any{{"ref": "notes/1", "title": "a", "tags": []string{}}, {"ref": "notes/2", "title": "b", "tags": []string{}}}
	cases := []struct {
		name string
		p    Page
		ok   bool
	}{
		{"complete", Page{Items: items}, true},
		{"more by offset", Page{Items: items, HasMore: true, NextOffset: &off}, true},
		{"truncated with more", Page{Items: items, HasMore: true, ContinuationReason: "narrow the filter", Truncated: true, TruncatedReason: "size"}, true},
		{"total", Page{Items: items, Total: &off}, true},
		{"continuation without has_more", Page{Items: items, NextOffset: &off}, false},
		{"has_more without continuation", Page{Items: items, HasMore: true}, false},
		{"two continuations", Page{Items: items, HasMore: true, NextOffset: &off, NextCursor: "c"}, false},
		{"negative offset", Page{Items: items, HasMore: true, NextOffset: &neg}, false},
		{"count differs", Page{Items: items, Count: 3}, false},
		{"truncated without has_more", Page{Items: items, Truncated: true, TruncatedReason: "size"}, false},
		{"truncated without reason", Page{Items: items, HasMore: true, ContinuationReason: "x", Truncated: true}, false},
		{"reason without truncated", Page{Items: items, TruncatedReason: "size"}, false},
		{"total below count", Page{Items: items, Total: &one}, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			h := newHarness(t)
			h.build = func(u *upstream) *App {
				a := testApp(u)
				a.commandByPath("note list").RunE = func(cmd *cobra.Command, _ []string) error { return a.List(cmd, c.p) }
				return a
			}
			r := h.run("note", "list")
			if c.ok && r.exit != 0 {
				t.Fatalf("a valid page was refused: %s", r)
			}
			if !c.ok && r.code() != "E_UNKNOWN" {
				t.Fatalf("an invalid page was not refused: %s", r)
			}
		})
	}
}
