// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func TestNoScopeRefusesEveryWriteBeforeSending(t *testing.T) {
	h := newHarness(t)
	h.scopeOn = false
	for _, args := range [][]string{
		{"note", "update", "1", "--title", "X", "--dry-run"},
		{"note", "update", "1", "--title", "X"},
		{"note", "share", "1", "--to", "bob@example.com", "--dangerous", "--dry-run"},
		{"note", "bulk-tag", "--ids", "1,2", "--tag", "x", "--dry-run"},
	} {
		r := h.run(args...)
		scope, _ := r.details()["scope"].(map[string]any)
		if r.exit != 4 || r.code() != "E_FORBIDDEN" || scope["status"] != "missing" || r.token() != "" {
			t.Fatalf("%v: want E_FORBIDDEN missing, got %s", args, r)
		}
		if !strings.Contains(r.details()["next_action"].(string), "do not create or edit it yourself") {
			t.Fatalf("next_action must tell the agent not to write the scope: %v", r.details()["next_action"])
		}
		ex := scope["grant_example"].(map[string]any)
		if ex["tool"] != "notes" || ex["granted_by"] != "<your name>" {
			t.Fatalf("grant_example: %v", ex)
		}
	}
	// single-target refusals carry the preview so the person sees what was asked for
	r := h.run("note", "update", "1", "--title", "X")
	if p, _ := r.details()["preview"].(map[string]any); p == nil || p["command"] != "note update" {
		t.Fatalf("refusal should carry details.preview: %s", r)
	}
	if h.up.Writes() != 0 {
		t.Fatal("wrote without a scope")
	}
	// reads still work
	if r := h.run("note", "get", "1"); r.exit != 0 {
		t.Fatalf("a read was refused without a scope: %s", r)
	}
}

func TestScopeStatuses(t *testing.T) {
	now := time.Now().UTC()
	cases := []struct {
		name   string
		change map[string]any
		raw    string
		status string
		args   []string
	}{
		{"expired", map[string]any{"expires_at": timeString(now.Add(-time.Minute))}, "", "expired", nil},
		{"revoked", map[string]any{"revoked": true}, "", "revoked", nil},
		{"other tool", map[string]any{"tool": "other-cli"}, "", "invalid", nil},
		{"unknown key", map[string]any{"approved": true}, "", "invalid", nil},
		{"missing key", map[string]any{"granted_by": nil}, "", "invalid", nil},
		{"empty targets", map[string]any{"targets": []any{}}, "", "insufficient", nil},
		{"null targets", nil, `{"scope_id":"a","tool":"notes","granted_by":"x","granted_at":"2026-01-01T00:00:00Z","expires_at":null,"effects":["change"],"dangerous":false,"targets":null}`, "invalid", nil},
		{"bad effect", map[string]any{"effects": []string{"change", "launch"}}, "", "invalid", nil},
		{"bad time", map[string]any{"expires_at": "tomorrow"}, "", "invalid", nil},
		{"relative output", map[string]any{"outputs": []string{"out"}}, "", "invalid", nil},
		{"duplicate key", nil, `{"scope_id":"a","scope_id":"b","tool":"notes","granted_by":"x","granted_at":"2026-01-01T00:00:00Z","expires_at":null,"effects":["change"],"dangerous":false}`, "invalid", nil},
		{"nested duplicate", nil, `{"scope_id":"a","tool":"notes","granted_by":"x","granted_at":"2026-01-01T00:00:00Z","expires_at":null,"effects":["change"],"dangerous":false,"targets":[{"key":"notes/","key":"x"}]}`, "invalid", nil},
		{"bom", nil, "\xef\xbb\xbf{}", "invalid", nil},
		{"two documents", nil, `{"scope_id":"a","tool":"notes","granted_by":"x","granted_at":"2026-01-01T00:00:00Z","expires_at":null,"effects":["change"],"dangerous":false} {}`, "invalid", nil},
		{"not an object", nil, `["change"]`, "invalid", nil},
		{"missing effect", map[string]any{"effects": []string{"deliver"}}, "", "insufficient", nil},
		{"not dangerous", map[string]any{"dangerous": false}, "", "insufficient", []string{"note", "delete", "1", "--dangerous", "--dry-run"}},
		{"target outside", map[string]any{"targets": []any{map[string]any{"key": "notes/2"}}}, "", "insufficient", nil},
		{"exact target", map[string]any{"targets": []any{map[string]any{"key": "notes", "match": "exact"}}}, "", "insufficient", nil},
		{"destination not granted", map[string]any{"destinations": []string{"alice@example.com"}}, "", "insufficient",
			[]string{"note", "share", "1", "--to", "bob@example.com", "--dangerous", "--dry-run"}},
		{"zero budget", map[string]any{"limits": map[string]any{"max_operations": 0}}, "", "invalid", nil},
		{"expires before granted", map[string]any{"expires_at": timeString(now.Add(-2 * time.Hour))}, "", "invalid", nil},
		{"local time", map[string]any{"granted_at": "2026-01-01T08:00:00+08:00"}, "", "invalid", nil},
		{"budget spent", map[string]any{"limits": map[string]any{"max_operations": 1}}, "", "budget_spent",
			[]string{"note", "bulk-tag", "--ids", "1,2", "--tag", "x", "--dry-run"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			h := newHarness(t)
			if c.raw != "" {
				mustWrite(t, h.scope, []byte(c.raw))
			} else {
				h.grant(c.change)
			}
			args := c.args
			if args == nil {
				args = []string{"note", "update", "1", "--title", "X", "--dry-run"}
			}
			r := h.run(args...)
			scope, _ := r.details()["scope"].(map[string]any)
			if r.exit != 4 || r.code() != "E_FORBIDDEN" || scope["status"] != c.status {
				t.Fatalf("want E_FORBIDDEN %s, got %s", c.status, r)
			}
			if h.up.Writes() != 0 {
				t.Fatal("wrote outside the scope")
			}
		})
	}
}

// The example grant of a readable scope is the whole file after the change: what was
// granted stays, only what is missing is added, and the scope keeps its id.
func TestGrantExampleExtendsTheCurrentScope(t *testing.T) {
	h := newHarness(t)
	h.grant(map[string]any{"targets": []any{map[string]any{"key": "notes/2"}}, "notes": "kept"})
	r := h.run("note", "share", "1", "--to", "eve@example.com", "--dangerous", "--dry-run")
	scope, _ := r.details()["scope"].(map[string]any)
	if scope["status"] != "insufficient" {
		t.Fatalf("want insufficient: %s", r)
	}
	ex := scope["grant_example"].(map[string]any)
	if ex["scope_id"] != "scp_test" || ex["granted_by"] != "tester" || ex["notes"] != "kept" || ex["dangerous"] != true {
		t.Fatalf("grant_example should keep the file's keys: %v", ex)
	}
	targets, _ := ex["targets"].([]any)
	if len(targets) != 2 || targets[0].(map[string]any)["key"] != "notes/2" || targets[1].(map[string]any)["key"] != "notes/1" {
		t.Fatalf("targets should be the union: %v", ex["targets"])
	}
	dests, _ := ex["destinations"].([]any)
	if len(dests) != 3 || dests[0] != "bob@example.com" || dests[2] != "eve@example.com" {
		t.Fatalf("destinations should be the union: %v", ex["destinations"])
	}
	if na, _ := r.details()["next_action"].(string); !strings.Contains(na, "details.scope.missing") || !strings.Contains(na, "do not edit it yourself") {
		t.Fatalf("next_action: %v", r.details()["next_action"])
	}

	// expired: the same file with a new expiry
	h.grant(map[string]any{"expires_at": timeString(time.Now().UTC().Add(-time.Minute))})
	r = h.run("note", "update", "1", "--title", "X", "--dry-run")
	ex = r.details()["scope"].(map[string]any)["grant_example"].(map[string]any)
	exp, _ := parseTime(ex["expires_at"].(string))
	if ex["scope_id"] != "scp_test" || !exp.After(time.Now()) {
		t.Fatalf("an expired scope's example should renew it: %v", ex)
	}

	// budget spent: the same file with room for this write
	h.grant(map[string]any{"limits": map[string]any{"max_operations": 1}})
	r = h.run("note", "bulk-tag", "--ids", "1,2", "--tag", "x", "--dry-run")
	ex = r.details()["scope"].(map[string]any)["grant_example"].(map[string]any)
	if n, _ := intOf(ex["limits"].(map[string]any)["max_operations"]); n != 2 {
		t.Fatalf("a spent budget's example should raise max_operations to 2: %v", ex)
	}
	if err := ValidateEnvelope([]byte(r.stdout)); err != nil {
		t.Fatalf("refusal envelope: %v", err)
	}
}

func TestInvalidScopeSaysWhichKey(t *testing.T) {
	h := newHarness(t)
	h.grant(map[string]any{"target": []any{map[string]any{"key": "notes/1"}}})
	r := h.run("note", "update", "1", "--title", "X", "--dry-run")
	if !strings.Contains(r.message(), "unknown key `target` (did you mean `targets`?)") || strings.Contains(r.message(), "json:") {
		t.Fatalf("message: %q", r.message())
	}
	h.grant(map[string]any{"dangerous": "yes"})
	r = h.run("note", "update", "1", "--title", "X", "--dry-run")
	if !strings.Contains(r.message(), "dangerous must be true or false, not string") {
		t.Fatalf("message: %q", r.message())
	}
}

func TestNonCanonicalTargetKeysAreRefused(t *testing.T) {
	h := newHarness(t)
	h.grant(map[string]any{"targets": []any{map[string]any{"key": "notes/"}}})
	for _, id := range []string{"../admin", "1/../../admin", "..", "1/./2", "1\\..\\x", "1\x00"} {
		r := h.run("note", "update", id, "--title", "X", "--dry-run")
		if r.exit != 2 || r.code() != "E_VALIDATION" || r.details()["parameter"] != "target" {
			t.Fatalf("target key from %q: %s", id, r)
		}
	}
	if h.up.Writes() != 0 {
		t.Fatal("a non-canonical target was written")
	}
}

func TestScopeCheckedAgainAtConfirm(t *testing.T) {
	h := newHarness(t)
	tok := h.mustToken("note", "update", "1", "--title", "X")
	h.grant(map[string]any{"revoked": true})
	r := h.run("note", "update", "1", "--title", "X", "--confirm", tok)
	if r.exit != 4 || r.details()["scope"].(map[string]any)["status"] != "revoked" || h.up.Writes() != 0 {
		t.Fatalf("a revoked scope at confirm: %s", r)
	}
}

func TestScopeBudgetCountsDispatchedOperations(t *testing.T) {
	h := newHarness(t)
	h.grant(map[string]any{"limits": map[string]any{"max_operations": 2}})
	for i, title := range []string{"A", "B"} {
		tok := h.mustToken("note", "update", "1", "--title", title)
		if r := h.run("note", "update", "1", "--title", title, "--confirm", tok); r.exit != 0 {
			t.Fatalf("write %d: %s", i, r)
		}
	}
	r := h.run("note", "update", "1", "--title", "C", "--dry-run")
	if r.exit != 4 || r.details()["scope"].(map[string]any)["status"] != "budget_spent" {
		t.Fatalf("third write: %s", r)
	}
	// a batch needs a unit per item
	h2 := newHarness(t)
	h2.grant(map[string]any{"limits": map[string]any{"max_operations": 1}})
	r = h2.run("note", "bulk-tag", "--ids", "1,2", "--tag", "x", "--dry-run")
	if r.exit != 4 || r.details()["scope"].(map[string]any)["status"] != "budget_spent" {
		t.Fatalf("batch over budget: %s", r)
	}
}

func TestSubtreeMatch(t *testing.T) {
	cases := []struct {
		sel   targetSelector
		key   string
		match bool
	}{
		{targetSelector{Key: "acme"}, "acme", true},
		{targetSelector{Key: "acme"}, "acme/web", true},
		{targetSelector{Key: "acme"}, "acme/web!5", true},
		{targetSelector{Key: "acme"}, "acme!5", true},
		{targetSelector{Key: "acme"}, "acme#3", true},
		{targetSelector{Key: "acme"}, "acme:x", true},
		{targetSelector{Key: "acme"}, "acme@x", true},
		{targetSelector{Key: "acme"}, "acmecorp", false},
		{targetSelector{Key: "acme"}, "acme-evil", false},
		{targetSelector{Key: "acme"}, "acm", false},
		{targetSelector{Key: "acme/"}, "acme/web", true},
		{targetSelector{Key: "acme/"}, "acme", false},
		{targetSelector{Key: "acme/web"}, "acme/web!5", true},
		{targetSelector{Key: "acme/web"}, "acme/webhooks", false},
		{targetSelector{Key: "legacy/payments", Match: "exact"}, "legacy/payments", true},
		{targetSelector{Key: "legacy/payments", Match: "exact"}, "legacy/payments!1", false},
		{targetSelector{Key: ""}, "anything", false},
	}
	for _, c := range cases {
		if got := subtreeMatch(c.sel, c.key); got != c.match {
			t.Errorf("%+v vs %q: got %v", c.sel, c.key, got)
		}
	}
}

func TestDestinationMatching(t *testing.T) {
	granted := []string{"bob@example.com", "https://hooks.example.com/notes", "#general", "mailto:alice@acme.com", "team/"}
	cases := map[string]bool{
		"bob@example.com":   true,
		"alice@example.com": false,
		// '@', ':', '#', '!' and '/' are ordinary characters of a non-URL destination,
		// never append points (decision 29, GO-GATE-01).
		"bob@example.com@evil.com":       false,
		"bob@example.com/evil":           false,
		"bob@example.com:evil":           false,
		"bob@example.com#evil":           false,
		"bob@example.com!evil":           false,
		"bob@example.com:x@evil.org":     false,
		"bob@example.com/x@evil.org":     false,
		"bob@example.com.evil.com":       false,
		"mailto:alice@acme.com":          true,
		"mailto:alice@acme.com@evil.com": false,
		"#general:secret":                false,
		// a granted entry ending in "/" is a prefix at "/" boundaries only
		"team/":                                true,
		"team/alice":                           true,
		"team/a/b":                             true,
		"team":                                 false,
		"teams/alice":                          false,
		"team/../admin":                        false,
		"team/./x":                             false,
		"team//x":                              false,
		"team/x/":                              false,
		"team/x\\..\\y":                        false,
		"team/x\x00":                           false,
		"https://hooks.example.com/notes":      true,
		"https://hooks.example.com/notes/team": true,
		"https://HOOKS.example.com/notes/team": true,
		"https://hooks.example.com/notesx":     false,
		"https://hooks.example.com/other":      false,
		"https://hooks.example.com.evil.org/notes":  false,
		"https://hooks.example.com@evil.org/notes":  false,
		"https://evil.org/hooks.example.com/notes":  false,
		"https://hooks.example.com/notes/../admin":  false,
		"https://hooks.example.com/notes/%2e%2e/x":  false,
		"https://hooks.example.com/notes?x=1":       false,
		"https://hooks.example.com/notes#frag":      false,
		"http://hooks.example.com/notes":            false,
		"https://hooks.example.com/notes%2fadmin":   false,
		"https://hooks.example.com\\notes\\x":       false,
		"#general":                                  true,
		"#general-secret":                           false,
		"https://hooks.example.com:8443/notes/team": false,
	}
	for dest, want := range cases {
		if got := destinationAllowed(dest, granted); got != want {
			t.Errorf("%q: got %v, want %v", dest, got, want)
		}
	}
}

// TestDestinationEscapeIsRefusedEndToEnd: a scope granting bob@example.com never
// lets the gate issue a token for bob@example.com@evil.com (GO-GATE-01).
func TestDestinationEscapeIsRefusedEndToEnd(t *testing.T) {
	h := newHarness(t)
	for _, to := range []string{"bob@example.com@evil.com", "bob@example.com:x@evil.org", "bob@example.com/x@evil.org"} {
		r := h.run("note", "share", "1", "--to", to, "--dangerous", "--dry-run")
		scope, _ := r.details()["scope"].(map[string]any)
		if r.exit != 4 || r.code() != "E_FORBIDDEN" || scope["status"] != "insufficient" || r.token() != "" {
			t.Fatalf("--to %s: want E_FORBIDDEN insufficient and no token, got %s", to, r)
		}
	}
	if h.up.Writes() != 0 {
		t.Fatal("a destination outside the scope was written")
	}
}

func TestOutputPathsResolveLinks(t *testing.T) {
	dir := t.TempDir()
	allowed := filepath.Join(dir, "allowed")
	other := filepath.Join(dir, "other")
	_ = os.MkdirAll(allowed, 0o700)
	_ = os.MkdirAll(other, 0o700)
	if !pathWithin(filepath.Join(allowed, "a.txt"), []string{allowed}) {
		t.Fatal("a file in the root was refused")
	}
	if !pathWithin(filepath.Join(allowed, "new", "deep", "a.txt"), []string{allowed}) {
		t.Fatal("a new nested file in the root was refused")
	}
	if pathWithin(filepath.Join(allowed, "..", "other", "a.txt"), []string{allowed}) {
		t.Fatal("a dot-dot path escaped the root")
	}
	if pathWithin(other+"x", []string{other}) {
		t.Fatal("a sibling with a common prefix matched")
	}
	link := filepath.Join(allowed, "escape")
	if err := os.Symlink(other, link); err == nil {
		if pathWithin(filepath.Join(link, "a.txt"), []string{allowed}) {
			t.Fatal("a symlink inside the root escaped it")
		}
	} else {
		t.Logf("symlinks unavailable: %v", err)
	}
	if runtime.GOOS == "windows" && !pathWithin(strings.ToUpper(filepath.Join(allowed, "a.txt")), []string{allowed}) {
		t.Fatal("Windows paths compare case-insensitively")
	}
}

func TestContextReportsTheScope(t *testing.T) {
	h := newHarness(t)
	r := h.run("context")
	auth := r.data()["authorization"].(map[string]any)
	if auth["status"] != "granted" || auth["scope_id"] != "scp_test" {
		t.Fatalf("authorization: %v", auth)
	}
	h.scopeOn = false
	r = h.run("context")
	if r.data()["authorization"].(map[string]any)["status"] != "missing" {
		t.Fatalf("authorization without scope: %v", r.data())
	}
}
