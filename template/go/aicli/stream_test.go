// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// streamLines parses NDJSON output into its lines.
func streamLines(t *testing.T, out string) []map[string]any {
	t.Helper()
	var lines []map[string]any
	for _, l := range strings.Split(strings.TrimRight(out, "\n"), "\n") {
		dec := json.NewDecoder(strings.NewReader(l))
		dec.UseNumber()
		var m map[string]any
		if err := dec.Decode(&m); err != nil {
			t.Fatalf("line %q is not JSON: %v", l, err)
		}
		lines = append(lines, m)
	}
	return lines
}

func TestStreamWritesItemsThenASummary(t *testing.T) {
	h := newHarness(t)
	r := h.run("note", "tail")
	if r.exit != 0 {
		t.Fatalf("exit %d: %s", r.exit, r.stdout)
	}
	if err := ValidateStream([]byte(r.stdout)); err != nil {
		t.Fatal(err)
	}
	lines := streamLines(t, r.stdout)
	if len(lines) < 3 {
		t.Fatalf("want an item per note and a summary, got %d lines", len(lines))
	}
	for i, l := range lines[:len(lines)-1] {
		if l["type"] != "item" || l["ok"] != true || l["data"] == nil {
			t.Fatalf("line %d is not an item: %v", i+1, l)
		}
	}
	last := lines[len(lines)-1]
	data, _ := last["data"].(map[string]any)
	if last["type"] != "summary" || last["ok"] != true || data["count"] != json.Number(itoa(len(lines)-1)) {
		t.Fatalf("summary: %v", last)
	}
	// --fields projects every item as an object (the ref is always kept).
	r = h.run("note", "tail", "--fields", "title")
	for _, l := range streamLines(t, r.stdout) {
		if l["type"] == "item" {
			if d := l["data"].(map[string]any); d["title"] == nil || d["body"] != nil || d["tags"] != nil {
				t.Fatalf("projected item: %v", d)
			}
		}
	}
}

func TestStreamEndsWithAnErrorSummary(t *testing.T) {
	h := newHarness(t)
	r := h.run("note", "tail", "--fail-after", "1")
	lines := streamLines(t, r.stdout)
	if len(lines) != 2 || lines[0]["type"] != "item" {
		t.Fatalf("want one item and the summary: %s", r.stdout)
	}
	last := lines[1]
	e, _ := last["error"].(map[string]any)
	if last["type"] != "summary" || last["ok"] != false || e == nil {
		t.Fatalf("error summary: %v", last)
	}
	code, _ := e["code"].(string)
	if want := coreCodes[code].exit; r.exit != want || r.exit == 0 {
		t.Fatalf("exit %d, want the summary's %s exit %d", r.exit, code, want)
	}
	if d, _ := e["details"].(map[string]any); d["dispatch_state"] != "not_started" {
		t.Fatalf("a read's error is not_started: %v", e)
	}
}

func TestValidateStreamRejectsMalformedStreams(t *testing.T) {
	item := `{"ok":true,"schema_version":"2.0","type":"item","data":{"id":"1"},"meta":{"duration_ms":1}}`
	summary := `{"ok":true,"schema_version":"2.0","type":"summary","data":{"count":1},"meta":{"duration_ms":1}}`
	if err := ValidateStream([]byte(item + "\n" + summary + "\n")); err != nil {
		t.Fatalf("a good stream: %v", err)
	}
	for name, raw := range map[string]string{
		"no summary":         item + "\n" + item + "\n",
		"summary not last":   summary + "\n" + item + "\n",
		"no type":            `{"ok":true,"schema_version":"2.0","data":{},"meta":{"duration_ms":1}}` + "\n" + summary + "\n",
		"crlf":               item + "\r\n" + summary + "\r\n",
		"no final newline":   item + "\n" + summary,
		"empty":              "",
		"not json":           "hello\n" + summary + "\n",
		"unknown type":       strings.Replace(item, `"item"`, `"row"`, 1) + "\n" + summary + "\n",
		"summary in between": item + "\n" + summary + "\n" + summary + "\n",
	} {
		if err := ValidateStream([]byte(raw)); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func outputScope(dir string) map[string]any {
	return map[string]any{"effects": []string{"change", "deliver", "artifact", "persist"}, "outputs": []string{dir}}
}

func TestWriteOutputGoesThroughTheGate(t *testing.T) {
	h := newHarness(t)
	dir := filepath.Join(h.dir, "out")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	h.grant(outputScope(dir))
	path := filepath.Join(dir, "notes.tsv")
	r := h.run("note", "export", "--output", path, "--dry-run")
	if r.exit != 0 || r.token() == "" {
		t.Fatalf("dry-run: %s", r)
	}
	p := r.data()["preview"].(map[string]any)
	if outs, _ := p["outputs"].([]any); len(outs) != 1 || outs[0] != path {
		t.Fatalf("preview.outputs: %v", p["outputs"])
	}
	if _, err := os.Stat(path); err == nil {
		t.Fatal("the dry-run wrote the file")
	}
	r = h.run("note", "export", "--output", path, "--confirm", r.token())
	if r.exit != 0 {
		t.Fatalf("confirm: %s", r)
	}
	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(content)
	res := r.data()["result"].(map[string]any)
	if res["path"] != path || res["sha256"] != hex.EncodeToString(sum[:]) || res["bytes"] != json.Number(itoa(len(content))) || res["format"] != "tsv" {
		t.Fatalf("data.result: %v", res)
	}
	if !strings.Contains(string(content), "\t") {
		t.Fatalf("content: %q", content)
	}
}

func TestWriteOutputOutsideTheScopeIsRefused(t *testing.T) {
	h := newHarness(t)
	h.grant(outputScope(filepath.Join(h.dir, "allowed")))
	path := filepath.Join(h.dir, "elsewhere", "notes.tsv")
	r := h.run("note", "export", "--output", path, "--dry-run")
	scope, _ := r.details()["scope"].(map[string]any)
	missing, _ := scope["missing"].(map[string]any)
	if r.code() != "E_FORBIDDEN" || scope["status"] != "insufficient" || missing["outputs"] == nil || r.token() != "" {
		t.Fatalf("want E_FORBIDDEN insufficient with missing.outputs: %s", r)
	}
	// The example grant adds the file's directory and keeps the granted one.
	ex := scope["grant_example"].(map[string]any)
	if outs, _ := ex["outputs"].([]any); len(outs) != 2 || outs[1] != filepath.Dir(path) {
		t.Fatalf("grant_example.outputs: %v", ex["outputs"])
	}
}

func TestWriteOutputRefusesAFileThatChanged(t *testing.T) {
	h := newHarness(t)
	h.grant(outputScope(h.dir))
	path := filepath.Join(h.dir, "notes.tsv")
	tok := h.mustToken("note", "export", "--output", path)
	if err := os.WriteFile(path, []byte("someone else's\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	r := h.run("note", "export", "--output", path, "--confirm", tok)
	expectConflict(t, r, "state_drift")
	if b, _ := os.ReadFile(path); string(b) != "someone else's\n" {
		t.Fatalf("the changed file was overwritten: %q", b)
	}
	// The conflict carries the new plan: overwrite the file that is there now.
	p, _ := r.details()["preview"].(map[string]any)
	if p == nil || !strings.HasPrefix(p["action"].(string), "Overwrite ") {
		t.Fatalf("fresh preview: %s", r)
	}
}
