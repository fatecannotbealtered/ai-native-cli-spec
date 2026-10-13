// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"strings"
	"testing"
)

// When an error must drop its preview to fit the budget, a token for that preview
// goes too: the agent never gets a token for a plan it cannot see.
func TestErrorBudgetNeverLeavesATokenWithoutItsPreview(t *testing.T) {
	preview := map[string]any{
		"operation_id": "op_aaaaaaaaaaaaaaaaaaaaaaaaaa", "command": "note update", "action": "Update note 1",
		"effects": []any{"change"}, "dangerous": false, "inputs": map[string]any{"title": strings.Repeat("t", 500)},
		"can_apply": true, "blocked_by": []any{}, "target": map[string]any{"key": "notes/1", "kind": "note"},
		"changes": []any{map[string]any{"field": "title", "before": strings.Repeat("b", 500), "after": strings.Repeat("a", 500)}},
	}
	obj := errorObject{Code: "E_CONFLICT", Message: "The target changed since the preview; nothing was sent.", Details: map[string]any{
		"conflict": "state_drift", "dispatch_state": "not_started",
		"next_action":   "details.preview is a new plan: if it still matches what the user asked for, run the same command again with --confirm ct2_aaaaaaaaaaaaaaaaaaaaaaaaaa_00000000000000000000000000000000.",
		"confirm_token": "ct2_aaaaaaaaaaaaaaaaaaaaaaaaaa_00000000000000000000000000000000", "expires_at": "2026-10-12T00:15:00Z",
		"operation_id": "op_aaaaaaaaaaaaaaaaaaaaaaaaaa", "preview": preview,
		// A long field that is never shortened forces the last stage.
		"lookup": strings.Repeat("l", 1500),
	}}
	out := fitErrorBudget(obj, metaObject{DurationMS: 1}, "notes")
	if size := errorSize(out, metaObject{DurationMS: 1}); size > errorBudgetChars {
		t.Fatalf("over budget: %d", size)
	}
	if hasKey(out.Details, "confirm_token") && !hasKey(out.Details, "preview") {
		t.Fatalf("a token without its preview: %v", out.Details)
	}
	if hasKey(out.Details, "preview") {
		t.Fatalf("the test should reach the last stage: %v", out.Details)
	}
	if na, _ := out.Details["next_action"].(string); !strings.Contains(na, "--dry-run") {
		t.Fatalf("next_action should ask for a new dry-run: %q", na)
	}
	// The same refusal that fits keeps both.
	small := errorObject{Code: obj.Code, Message: obj.Message, Details: cloneMap(obj.Details)}
	delete(small.Details, "lookup")
	out = fitErrorBudget(small, metaObject{DurationMS: 1}, "notes")
	if !hasKey(out.Details, "confirm_token") || !hasKey(out.Details, "preview") {
		t.Fatalf("a fitting conflict keeps its plan: %v", out.Details)
	}
}

// The shortened note says where the full details are, and only where they exist: the
// items of a batch from operation list --batch, a single operation from operation show.
func TestShortenedNoteNamesWhereTheFullDetailsAre(t *testing.T) {
	batch := "opb_aaaaaaaaaaaaaaaaaaaaaaaaaa"
	cases := []struct {
		obj  errorObject
		tool string
		want string
	}{
		{errorObject{Code: "E_CONFIRMATION_REQUIRED", Details: map[string]any{"preview": map[string]any{}}}, "notes", "--dry-run returns the full preview"},
		{errorObject{Code: "E_PARTIAL", Details: map[string]any{"items": []any{}, "operation_id": batch}}, "notes",
			"(Details were shortened to fit; `notes operation list --batch " + batch + "` lists every item.)"},
		{errorObject{Code: "E_PARTIAL", Details: map[string]any{"items": []any{}, "operation_id": batch}}, "",
			"(Details were shortened to fit; `operation list --batch " + batch + "` lists every item.)"},
		{errorObject{Code: "E_PARTIAL", Details: map[string]any{"items": []any{}}}, "notes", "`operation show --id` returns them in full"},
		{errorObject{Code: "E_VERIFY_FAILED", Details: map[string]any{"receipt": map[string]any{}, "operation_id": "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"}},
			"notes", "`operation show --id` returns them in full"},
		{errorObject{Code: "E_FORBIDDEN", Details: map[string]any{"scope": map[string]any{}}}, "notes", "(Details were shortened to fit.)"},
	}
	for _, c := range cases {
		if got := shortenedNote(c.obj, c.tool); !strings.Contains(got, c.want) {
			t.Errorf("%s: %q", c.obj.Code, got)
		}
	}
}
