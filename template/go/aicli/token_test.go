// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"regexp"
	"strings"
	"testing"
)

func testClaims() claims {
	return claims{V: 2, Tool: "notes", ToolVersion: "1.2.3", Op: "op_aaaaaaaaaaaaaaaaaaaaaaaaaa", Cmd: "note update",
		Inv: "sha256:00", Inputs: map[string]string{"id": "sha256:01", "title": "sha256:02"}, Account: "tester",
		Host: "https://notes.example", Target: "notes/1", Scope: "sha256:03", Base: "4", Idem: "", Exp: "2026-10-12T08:10:00Z"}
}

func testKey() []byte {
	k := make([]byte, keySize)
	for i := range k {
		k[i] = byte(i)
	}
	return k
}

func TestOperationIDsMatchTheContract(t *testing.T) {
	single := regexp.MustCompile(operationIDPattern)
	batch := regexp.MustCompile(batchIDPattern)
	seen := map[string]bool{}
	for i := 0; i < 200; i++ {
		id, err := newOperationID(false)
		if err != nil || !single.MatchString(id) || !validOperationID(id) {
			t.Fatalf("bad op id %q (%v)", id, err)
		}
		bid, err := newOperationID(true)
		if err != nil || !batch.MatchString(bid) || !validOperationID(bid) {
			t.Fatalf("bad batch id %q (%v)", bid, err)
		}
		if seen[id] || seen[bid] {
			t.Fatal("operation ids repeat")
		}
		seen[id], seen[bid] = true, true
	}
	for _, bad := range []string{"", "op_", "op_AAAAAAAAAAAAAAAAAAAAAAAAAA", "op_aaaaaaaaaaaaaaaaaaaaaaaaa", "op_aaaaaaaaaaaaaaaaaaaaaaaaaaa",
		"op_aaaaaaaaaaaaaaaaaaaaaaaaa1", "op_../../../../etc/passwd/aa", "opx_aaaaaaaaaaaaaaaaaaaaaaaaaa", "op_aaaaaaaaaaaaaaaaaaaaaaaaa="} {
		if validOperationID(bad) {
			t.Errorf("%q accepted as an operation id", bad)
		}
	}
}

func TestTokenRoundTripAndFormat(t *testing.T) {
	c := testClaims()
	tok, err := mintToken(testKey(), c)
	if err != nil {
		t.Fatal(err)
	}
	if !regexp.MustCompile(tokenPattern).MatchString(tok) {
		t.Fatalf("token %q does not match the contract format", tok)
	}
	part, mac, ok := parseToken(tok)
	if !ok || part != idPart(c.Op) || !verifyMAC(testKey(), c, mac) {
		t.Fatalf("round trip failed: %v %q %q", ok, part, mac)
	}
}

// TestTokenKnownAnswer pins the token encoding so other implementations (the Python
// library) can check they produce the same bytes.
func TestTokenKnownAnswer(t *testing.T) {
	c := testClaims()
	body, err := c.canonical()
	if err != nil {
		t.Fatal(err)
	}
	wantBody := `{"account":"tester","base":"4","cmd":"note update","exp":"2026-10-12T08:10:00Z","host":"https://notes.example",` +
		`"idem":"","inputs":{"id":"sha256:01","title":"sha256:02"},"inv":"sha256:00","op":"op_aaaaaaaaaaaaaaaaaaaaaaaaaa",` +
		`"scope":"sha256:03","target":"notes/1","tool":"notes","tool_version":"1.2.3","v":2}`
	if string(body) != wantBody {
		t.Fatalf("canonical claims changed:\n got %s\nwant %s", body, wantBody)
	}
	tok, _ := mintToken(testKey(), c)
	const want = "ct2_aaaaaaaaaaaaaaaaaaaaaaaaaa_"
	if !strings.HasPrefix(tok, want) || len(tok) != tokenLen {
		t.Fatalf("token %q", tok)
	}
	t.Logf("known-answer token: %s", tok)
}

func TestParseTokenAcceptsOnlyTheCanonicalForm(t *testing.T) {
	tok, _ := mintToken(testKey(), testClaims())
	if _, _, ok := parseToken(tok); !ok {
		t.Fatal("canonical token rejected")
	}
	bad := map[string]string{
		"upper case":         strings.ToUpper(tok),
		"upper-case id":      tok[:4] + strings.ToUpper(tok[4:30]) + tok[30:],
		"upper-case mac":     tok[:31] + strings.ToUpper(tok[31:]),
		"padding":            tok + "=",
		"double padding":     tok + "==",
		"truncated":          tok[:len(tok)-1],
		"extra hex":          tok + "a",
		"leading space":      " " + tok,
		"trailing space":     tok + " ",
		"trailing newline":   tok + "\n",
		"no prefix":          tok[4:],
		"old prefix":         "ct_" + tok[4:],
		"v1 prefix":          "ct1_" + tok[4:],
		"dash separator":     tok[:30] + "-" + tok[31:],
		"base32 digit 0":     tok[:4] + "0" + tok[5:],
		"base32 digit 1":     tok[:4] + "1" + tok[5:],
		"base32 digit 8":     tok[:4] + "8" + tok[5:],
		"non-hex mac":        tok[:31] + "g" + tok[32:],
		"empty":              "",
		"base64 style":       "ct2_" + strings.Repeat("A", 26) + "_" + strings.Repeat("0", 32),
		"op id in token":     "ct2_op_" + tok[4:],
		"unicode lookalike":  strings.Replace(tok, "a", "а", 1),
		"url-encoded":        strings.Replace(tok, "_", "%5F", 1),
		"full-width digit":   tok[:31] + "０" + tok[32:],
		"null byte appended": tok + "\x00",
	}
	for name, s := range bad {
		if _, _, ok := parseToken(s); ok {
			t.Errorf("%s: %q accepted", name, s)
		}
	}
}

func TestTokenMACBindsEveryClaim(t *testing.T) {
	key := testKey()
	c := testClaims()
	tok, _ := mintToken(key, c)
	_, mac, _ := parseToken(tok)
	mutations := map[string]func(*claims){
		"v":            func(c *claims) { c.V = 3 },
		"tool":         func(c *claims) { c.Tool = "other" },
		"tool_version": func(c *claims) { c.ToolVersion = "1.2.4" },
		"op":           func(c *claims) { c.Op = "op_bbbbbbbbbbbbbbbbbbbbbbbbbb" },
		"cmd":          func(c *claims) { c.Cmd = "note delete" },
		"inv":          func(c *claims) { c.Inv = "sha256:99" },
		"inputs value": func(c *claims) { c.Inputs = map[string]string{"id": "sha256:01", "title": "sha256:09"} },
		"inputs key":   func(c *claims) { c.Inputs = map[string]string{"id": "sha256:01", "body": "sha256:02"} },
		"inputs extra": func(c *claims) {
			c.Inputs = map[string]string{"id": "sha256:01", "title": "sha256:02", "tag": "sha256:0a"}
		},
		"account": func(c *claims) { c.Account = "mallory" },
		"host":    func(c *claims) { c.Host = "https://evil.example" },
		"target":  func(c *claims) { c.Target = "notes/2" },
		"scope":   func(c *claims) { c.Scope = "" },
		"base":    func(c *claims) { c.Base = "5" },
		"idem":    func(c *claims) { c.Idem = "k" },
		"exp":     func(c *claims) { c.Exp = "2026-10-12T09:10:00Z" },
	}
	for name, mutate := range mutations {
		m := testClaims()
		m.Inputs = map[string]string{"id": "sha256:01", "title": "sha256:02"}
		mutate(&m)
		if verifyMAC(key, m, mac) {
			t.Errorf("changing %s keeps the MAC valid", name)
		}
	}
	other := testKey()
	other[0] ^= 1
	if verifyMAC(other, c, mac) {
		t.Error("a different key verifies the MAC")
	}
	if verifyMAC(key, c, mac[:30]) || verifyMAC(key, c, mac+"00") || verifyMAC(key, c, "zz"+mac[2:]) {
		t.Error("a malformed MAC verified")
	}
}

func TestCanonicalJSON(t *testing.T) {
	got, err := canonicalJSON(map[string]any{"b": "x\"y\\z\n\x01é<>&", "a": []string{"2", "1"}, "c": map[string]string{"z": "1", "y": "2"}, "d": 2})
	if err != nil {
		t.Fatal(err)
	}
	want := `{"a":["2","1"],"b":"x\"y\\z\u000a\u0001é<>&","c":{"y":"2","z":"1"},"d":2}`
	if string(got) != want {
		t.Fatalf("got  %s\nwant %s", got, want)
	}
	if _, err := canonicalJSON("\xff"); err == nil {
		t.Fatal("invalid UTF-8 was encoded")
	}
	if _, err := canonicalJSON(1.5); err == nil {
		t.Fatal("floats must not be encoded canonically")
	}
	if claimString("\xff") == claimString("\xfe") || !strings.HasPrefix(claimString("\xff"), "bytes-sha256:") {
		t.Fatal("invalid UTF-8 inputs must bind by raw-byte digest")
	}
}
