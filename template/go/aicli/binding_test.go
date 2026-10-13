// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeFile(t *testing.T, dir, name, content string) string {
	t.Helper()
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestPositionalAndFlagBindIdentically(t *testing.T) {
	h := newHarness(t)
	tok := h.mustToken("note", "update", "1", "--title", "Same")
	r := h.run("note", "update", "--id", "1", "--title", "Same", "--confirm", tok)
	if r.exit != 0 || h.up.Writes() != 1 {
		t.Fatalf("flag form did not match the positional form: %s", r)
	}
	tok = h.mustToken("note", "update", "--id", "2", "--title", "Same")
	r = h.run("note", "update", "2", "--title", "Same", "--confirm", tok)
	if r.exit != 0 || h.up.Writes() != 2 {
		t.Fatalf("positional form did not match the flag form: %s", r)
	}
}

func TestBodyAndBodyFileBindIdentically(t *testing.T) {
	h := newHarness(t)
	f := writeFile(t, h.dir, "body.txt", "Hello from a file")
	tok := h.mustToken("note", "update", "1", "--body", "Hello from a file")
	r := h.run("note", "update", "1", "--body-file", f, "--confirm", tok)
	if r.exit != 0 {
		t.Fatalf("--body-file with the same content must match --body: %s", r)
	}
	tok = h.mustToken("note", "update", "2", "--body-file", f)
	r = h.run("note", "update", "2", "--body", "Hello from a file", "--confirm", tok)
	if r.exit != 0 {
		t.Fatalf("--body with the same content must match --body-file: %s", r)
	}
	// a trailing newline is different content
	f2 := writeFile(t, h.dir, "body2.txt", "Hello from a file\n")
	tok = h.mustToken("note", "update", "3", "--body", "Hello from a file")
	r = h.run("note", "update", "3", "--body-file", f2, "--confirm", tok)
	expectMismatch(t, r, "--body")
	// stdin binds by content too
	h.stdin = "piped body"
	tok = h.mustToken("note", "update", "3", "--body-file", "-")
	h.stdin = ""
	r = h.run("note", "update", "3", "--body", "piped body", "--confirm", tok)
	if r.exit != 0 {
		t.Fatalf("stdin content must bind like the literal: %s", r)
	}
	if n, _ := h.up.get("3"); n.Body != "piped body" {
		t.Fatalf("body: %q", n.Body)
	}
}

func TestFileContentChangedAfterDryRunIsRefused(t *testing.T) {
	h := newHarness(t)
	f := writeFile(t, h.dir, "body.txt", "approved text")
	tok := h.mustToken("note", "update", "1", "--body-file", f)
	writeFile(t, h.dir, "body.txt", "swapped text")
	r := h.run("note", "update", "1", "--body-file", f, "--confirm", tok)
	expectMismatch(t, r, "--body")
	if h.up.Writes() != 0 {
		t.Fatal("swapped file content was sent")
	}
	p := r.details()
	if strings.Contains(r.stdout, "swapped text") {
		t.Fatalf("the error echoed input content: %v", p)
	}
}

func TestBodyAndBodyFileTogetherIsUsage(t *testing.T) {
	h := newHarness(t)
	f := writeFile(t, h.dir, "b.txt", "x")
	r := h.run("note", "update", "1", "--body", "x", "--body-file", f, "--dry-run")
	if r.exit != 2 || r.details()["problem"] != "conflicting_flags" {
		t.Fatalf("got %s", r)
	}
}

func TestMissingFileInputIsUsage(t *testing.T) {
	h := newHarness(t)
	r := h.run("note", "update", "1", "--body-file", filepath.Join(h.dir, "nope.txt"), "--dry-run")
	if r.exit != 2 || r.details()["problem"] != "invalid_value" || r.details()["parameter"] != "--body-file" {
		t.Fatalf("got %s", r)
	}
}

func TestMismatchNamesTheParameter(t *testing.T) {
	cases := []struct {
		name    string
		dry     []string
		confirm []string
		param   string
	}{
		{"changed flag", []string{"note", "update", "1", "--title", "A"}, []string{"note", "update", "1", "--title", "B"}, "--title"},
		{"changed positional", []string{"note", "update", "1", "--title", "A"}, []string{"note", "update", "2", "--title", "A"}, "--id"},
		{"added flag", []string{"note", "update", "1", "--title", "A"}, []string{"note", "update", "1", "--title", "A", "--body", "x"}, "--body"},
		{"dropped flag", []string{"note", "update", "1", "--title", "A", "--body", "x"}, []string{"note", "update", "1", "--title", "A"}, "--body"},
		{"slice order", []string{"note", "create", "--title", "A", "--tag", "x,y"}, []string{"note", "create", "--title", "A", "--tag", "y,x"}, "--tag"},
		{"slice element", []string{"note", "create", "--title", "A", "--tag", "x", "--tag", "y"}, []string{"note", "create", "--title", "A", "--tag", "x"}, "--tag"},
		{"explicit default", []string{"note", "create", "--title", "A"}, []string{"note", "create", "--title", "A", "--body", ""}, "--body"},
		{"other command", []string{"note", "update", "1", "--title", "A"}, []string{"note", "comment", "add", "1", "--text", "A"}, "command"},
		{"secret", []string{"note", "share", "1", "--to", "bob@example.com", "--token", "s3cret-one", "--dangerous"},
			[]string{"note", "share", "1", "--to", "bob@example.com", "--token", "s3cret-two", "--dangerous"}, "--token"},
		{"idempotency key", []string{"note", "create", "--title", "A", "--idempotency-key", "k1"},
			[]string{"note", "create", "--title", "A", "--idempotency-key", "k2"}, "--idempotency-key"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			h := newHarness(t)
			tok := h.mustToken(c.dry...)
			r := h.run(append(c.confirm, "--confirm", tok)...)
			expectMismatch(t, r, c.param)
			if h.up.Writes() != 0 {
				t.Fatal("a mismatched confirm wrote")
			}
		})
	}
}

func TestEquivalentSpellingsBindIdentically(t *testing.T) {
	h := newHarness(t)
	cases := [][2][]string{
		{{"note", "update", "1", "--title", "A"}, {"note", "update", "1", "--title=A"}},
		{{"note", "update", "1", "--title", "A"}, {"note", "update", "1", "-t", "A"}},
		{{"note", "update", "1", "--title", "A"}, {"note", "update", "1", "--subject", "A"}},
		{{"note", "update", "1", "--title", "A"}, {"notes", "update", "1", "--title", "A"}},
		{{"note", "delete", "1", "--dangerous"}, {"note", "rm", "1", "--dangerous"}},
		{{"note", "comment", "add", "1", "--text", "hi"}, {"comment", "add", "1", "--text", "hi"}},
		{{"note", "comment", "add", "1", "--text", "hi"}, {"comments", "create", "1", "--text", "hi"}},
	}
	for _, c := range cases {
		h.up.reset()
		tok := h.mustToken(c[0]...)
		r := h.run(append(c[1], "--confirm", tok)...)
		if r.exit != 0 {
			t.Fatalf("%v should confirm a token from %v: %s", c[1], c[0], r)
		}
	}
}

func TestPresentationFlagsAreNotBound(t *testing.T) {
	h := newHarness(t)
	tok := h.mustToken("note", "update", "1", "--title", "A", "--pretty", "--fields", "title")
	r := h.run("note", "update", "1", "--title", "A", "--quiet", "--format", "text", "--confirm", tok)
	if r.exit != 0 {
		t.Fatalf("presentation flags changed the binding: %s", r)
	}
}

func TestSecretsNeverLeave(t *testing.T) {
	h := newHarness(t)
	const secret = "tok-SUPER-SECRET-123"
	args := []string{"note", "share", "1", "--to", "bob@example.com", "--token", secret, "--dangerous"}
	dry := h.run(append(args, "--dry-run")...)
	if dry.exit != 0 {
		t.Fatalf("dry-run: %s", dry)
	}
	inputs := dry.data()["preview"].(map[string]any)["inputs"].(map[string]any)
	if inputs["token"] != "***" {
		t.Fatalf("secret shown in preview inputs: %v", inputs)
	}
	bare := h.run(args...)
	conf := h.run(append(args, "--confirm", dry.token())...)
	if conf.exit != 0 {
		t.Fatalf("confirm: %s", conf)
	}
	ref := h.run("reference", "--command", "note share")
	for _, out := range []tout{dry, bare, conf, ref} {
		if strings.Contains(out.stdout+out.stderr, secret) {
			t.Fatalf("secret leaked in output: %s", out)
		}
	}
	// nothing in the state dir holds the secret or its unkeyed hash
	unkeyed := sha256Hex([]byte(secret))
	_ = filepath.Walk(h.state, func(p string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return nil
		}
		b, _ := os.ReadFile(p)
		if strings.Contains(string(b), secret) || strings.Contains(string(b), unkeyed) {
			t.Errorf("%s stores the secret or its unkeyed hash", p)
		}
		return nil
	})
	// a secret file input is shown as *** too
	params := ref.data()["command"].(map[string]any)["params"].([]any)
	found := false
	for _, p := range params {
		if p.(map[string]any)["name"] == "token" {
			found = p.(map[string]any)["secret"] == true
		}
	}
	if !found {
		t.Fatal("reference must mark --token secret")
	}
}

func TestUsageErrorsDoNotEchoSecrets(t *testing.T) {
	h := newHarness(t)
	const secret = "s3cr3t-in-a-typo"
	r := h.run("note", "share", "1", "--to", "bob@example.com", "--tokn", secret, "--dangerous")
	if r.exit != 2 || strings.Contains(r.stdout+r.stderr, secret) {
		t.Fatalf("got %s", r)
	}
}

func TestBatchTargetsAreBound(t *testing.T) {
	h := newHarness(t)
	tok := h.mustToken("note", "bulk-tag", "--ids", "1,2", "--tag", "work")
	for _, ids := range []string{"1,3", "2,1", "1", "1,2,3"} {
		r := h.run("note", "bulk-tag", "--ids", ids, "--tag", "work", "--confirm", tok)
		expectMismatch(t, r, "--ids")
	}
	r := h.run("note", "bulk-tag", "--ids", "1,2", "--tag", "home", "--confirm", tok)
	expectMismatch(t, r, "--tag")
	if h.up.Writes() != 0 {
		t.Fatal("a mismatched batch wrote")
	}
	r = h.run("note", "bulk-tag", "--ids", "1", "--ids", "2", "--tag", "work", "--confirm", tok)
	if r.exit == 0 {
		// repeated --ids and comma form give the same slice, so this confirms
		if h.up.Writes() != 2 {
			t.Fatalf("writes: %d", h.up.Writes())
		}
	} else {
		t.Fatalf("repeated --ids must bind like the comma form: %s", r)
	}
}

func TestSetResolvedBindsTheResolvedValue(t *testing.T) {
	h := newHarness(t)
	h.build = func(u *upstream) *App {
		a := testApp(u)
		for _, path := range []string{"note update", "note bulk-tag"} {
			c := a.commandByPath(path)
			orig := c.RunE
			c.RunE = func(cmd *cobraCommand, args []string) error {
				if f := cmd.Flags().Lookup("id"); f != nil && f.Changed {
					if err := SetResolved(cmd, "id", strings.TrimPrefix(f.Value.String(), "notes/")); err != nil {
						return err
					}
				}
				if ids, err := cmd.Flags().GetStringSlice("ids"); err == nil && len(ids) > 0 {
					var out []string
					for _, id := range ids {
						out = append(out, strings.TrimPrefix(id, "notes/"))
					}
					if err := SetResolved(cmd, "ids", out...); err != nil {
						return err
					}
				}
				return orig(cmd, args)
			}
		}
		return a
	}
	tok := h.mustToken("note", "update", "notes/1", "--title", "A")
	if r := h.run("note", "update", "1", "--title", "A", "--confirm", tok); r.exit != 0 {
		t.Fatalf("a ref and an id must bind identically after SetResolved: %s", r)
	}
	tok = h.mustToken("note", "bulk-tag", "--ids", "notes/1,2", "--tag", "x")
	if r := h.run("note", "bulk-tag", "--ids", "1,notes/2", "--tag", "x", "--confirm", tok); r.exit != 0 {
		t.Fatalf("resolved slices must bind identically: %s", r)
	}
}
