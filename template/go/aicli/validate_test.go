// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"strings"
	"testing"
)

func TestValidateEnvelopeAcceptsLibraryOutput(t *testing.T) {
	good := []string{
		`{"ok":true,"schema_version":"2.0","data":{},"meta":{"duration_ms":0}}`,
		`{"ok":true,"schema_version":"2.0","data":{"items":[],"count":0,"has_more":false},"meta":{"duration_ms":3,"untrusted":["data.items[].title"]}}`,
		`{"ok":false,"schema_version":"2.0","error":{"code":"E_NOT_FOUND","message":"Gone.","retryable":false,"details":{"dispatch_state":"not_started","next_action":"List them."}},"meta":{"duration_ms":1}}`,
		`{"ok":false,"schema_version":"2.0","error":{"code":"E_X_TOOL","message":"Tool code.","retryable":false,"details":{"dispatch_state":"not_started","next_action":"Do x."}},"meta":{"duration_ms":1}}`,
		`{"ok":true,"schema_version":"2.0","data":{"id":"5","note_ids":["1"],"updated_at":"2026-10-12T08:00:00.123Z","closed_at":null,"error_codes":{"E_USAGE":{"exit":2}}},"meta":{"duration_ms":0}}`,
	}
	for _, g := range good {
		if err := ValidateEnvelope([]byte(g + "\n")); err != nil {
			t.Errorf("%s: %v", g, err)
		}
	}
}

func TestValidateEnvelopeRejectsContractViolations(t *testing.T) {
	ok := `{"ok":true,"schema_version":"2.0","data":{},"meta":{"duration_ms":0}}`
	errEnv := func(code, retry, details string) string {
		return `{"ok":false,"schema_version":"2.0","error":{"code":"` + code + `","message":"m","retryable":` + retry + `,"details":` + details + `},"meta":{"duration_ms":0}}`
	}
	base := `"dispatch_state":"not_started","next_action":"n"`
	receipt := func(outcome string) string {
		return `{"operation_id":"op_aaaaaaaaaaaaaaaaaaaaaaaaaa","command":"x","target":{"key":"k"},"effects":["change"],"outcome":` + outcome + `,"observed":{},"replayed":false}`
	}
	applied := `{"execution":"completed","change":"applied","delivery":"not_applicable","persistence":"not_applicable","verification":"passed"}`
	unknown := `{"execution":"unknown","change":"unknown","delivery":"not_applicable","persistence":"not_applicable","verification":"not_run"}`
	cases := map[string]string{
		"no newline":            ok,
		"bom":                   "\xef\xbb\xbf" + ok + "\n",
		"two documents":         ok + "\n" + ok + "\n",
		"crlf":                  ok + "\r\n",
		"extra key":             `{"ok":true,"schema_version":"2.0","data":{},"meta":{"duration_ms":0},"extra":1}` + "\n",
		"missing meta":          `{"ok":true,"schema_version":"2.0","data":{}}` + "\n",
		"old schema":            `{"ok":true,"schema_version":"1.0","data":{},"meta":{"duration_ms":0}}` + "\n",
		"meta extra":            `{"ok":true,"schema_version":"2.0","data":{},"meta":{"duration_ms":0,"x":1}}` + "\n",
		"negative duration":     `{"ok":true,"schema_version":"2.0","data":{},"meta":{"duration_ms":-1}}` + "\n",
		"bad untrusted path":    `{"ok":true,"schema_version":"2.0","data":{},"meta":{"duration_ms":0,"untrusted":["items[].title"]}}` + "\n",
		"duplicate key":         `{"ok":true,"ok":true,"schema_version":"2.0","data":{},"meta":{"duration_ms":0}}` + "\n",
		"error with data":       `{"ok":false,"schema_version":"2.0","data":{},"meta":{"duration_ms":0}}` + "\n",
		"retryable mismatch":    errEnv("E_NETWORK", "false", `{`+base+`}`) + "\n",
		"no dispatch_state":     errEnv("E_NOT_FOUND", "false", `{"next_action":"n"}`) + "\n",
		"bad dispatch_state":    errEnv("E_NOT_FOUND", "false", `{"dispatch_state":"maybe","next_action":"n"}`) + "\n",
		"no next_action":        errEnv("E_NOT_FOUND", "false", `{"dispatch_state":"not_started"}`) + "\n",
		"usage without problem": errEnv("E_USAGE", "false", `{`+base+`}`) + "\n",
		"unknown problem":       errEnv("E_USAGE", "false", `{`+base+`,"problem":"oops"}`) + "\n",
		"mismatch no param":     errEnv("E_CONFLICT", "false", `{`+base+`,"conflict":"token_mismatch"}`) + "\n",
		"unresolved no active":  errEnv("E_CONFLICT", "false", `{`+base+`,"conflict":"unresolved_operation"}`) + "\n",
		"unknown no lookup":     errEnv("E_OUTCOME_UNKNOWN", "false", `{`+base+`,"operation_id":"op_aaaaaaaaaaaaaaaaaaaaaaaaaa"}`) + "\n",
		"confirm no token":      errEnv("E_CONFIRMATION_REQUIRED", "false", `{`+base+`,"problem":"confirmation_required"}`) + "\n",
		"partial no receipt":    errEnv("E_PARTIAL", "false", `{`+base+`}`) + "\n",
		"verify wrong receipt":  errEnv("E_VERIFY_FAILED", "false", `{`+base+`,"receipt":`+receipt(applied)+`}`) + "\n",
		"human bad action":      errEnv("E_HUMAN_REQUIRED", "false", `{`+base+`,"action":"dance","resume":"x"}`) + "\n",
		"bad code":              errEnv("NOT_FOUND", "false", `{`+base+`}`) + "\n",
		"ok but unknown receipt": `{"ok":true,"schema_version":"2.0","data":{"operation_id":"op_aaaaaaaaaaaaaaaaaaaaaaaaaa","receipt":` +
			receipt(unknown) + `,"result":{}},"meta":{"duration_ms":0}}` + "\n",
		"page without continuation": `{"ok":true,"schema_version":"2.0","data":{"items":[],"count":0,"has_more":true},"meta":{"duration_ms":0}}` + "\n",
		"truncated without reason":  `{"ok":true,"schema_version":"2.0","data":{"items":[],"count":0,"has_more":false,"truncated":true},"meta":{"duration_ms":0}}` + "\n",
		"non-canonical token":       `{"ok":true,"schema_version":"2.0","data":{"preview":{},"confirm_token":"CT2_X","expires_at":"2026-01-01T00:00:00Z","operation_id":"op_aaaaaaaaaaaaaaaaaaaaaaaaaa"},"meta":{"duration_ms":0}}` + "\n",
		"over budget":               errEnv("E_NOT_FOUND", "false", `{`+base+`,"x":"`+strings.Repeat("y", 2100)+`"}`) + "\n",
		"notice bad severity":       `{"ok":true,"schema_version":"2.0","data":{},"meta":{"duration_ms":0,"notices":[{"type":"x","severity":"loud","message":"m"}]}}` + "\n",
		"camel case key":            `{"ok":true,"schema_version":"2.0","data":{"createdBy":"x"},"meta":{"duration_ms":0}}` + "\n",
		"numeric id":                `{"ok":true,"schema_version":"2.0","data":{"id":5},"meta":{"duration_ms":0}}` + "\n",
		"numeric ids":               `{"ok":true,"schema_version":"2.0","data":{"note_ids":["1",2]},"meta":{"duration_ms":0}}` + "\n",
		"local time":                `{"ok":true,"schema_version":"2.0","data":{"updated_at":"2026-10-12T08:00:00+08:00"},"meta":{"duration_ms":0}}` + "\n",
		"one candidate":             errEnv("E_AMBIGUOUS", "false", `{`+base+`,"candidates":[{"ref":"a"}]}`) + "\n",
		"negative retry":            errEnv("E_RATE_LIMITED", "true", `{`+base+`,"retry_after_ms":-5}`) + "\n",
		"lookup other operation":    errEnv("E_OUTCOME_UNKNOWN", "false", `{`+base+`,"operation_id":"op_aaaaaaaaaaaaaaaaaaaaaaaaaa","lookup":"x operation show --id op_bbbbbbbbbbbbbbbbbbbbbbbbbb"}`) + "\n",
	}
	for name, raw := range cases {
		if err := ValidateEnvelope([]byte(raw)); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}
