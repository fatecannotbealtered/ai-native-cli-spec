// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"testing"
)

// TestGeneratedContractMatchesContractJSON checks contract_gen.go against the
// contract it was generated from (spec repo only; gen-contract.js --check guards the
// bytes, this guards the meaning).
func TestGeneratedContractMatchesContractJSON(t *testing.T) {
	b, err := os.ReadFile(filepath.Join("..", "..", "common", "contract", "contract.json"))
	if err != nil {
		t.Skipf("contract.json not available here: %v", err)
	}
	var c struct {
		SpecVersion string `json:"spec_version"`
		Envelope    struct {
			SchemaVersion string `json:"schema_version_value"`
		} `json:"envelope"`
		ErrorCodes struct {
			Core map[string]struct {
				Exit      int  `json:"exit"`
				Retryable bool `json:"retryable"`
			} `json:"core"`
		} `json:"error_codes"`
		ErrorDetails struct {
			Problem  []string `json:"problem"`
			Conflict []string `json:"conflict"`
		} `json:"error_details"`
		ControlFlags struct {
			Excluded []string `json:"excluded_from_binding"`
		} `json:"control_flags"`
		Token struct {
			Format string `json:"format"`
			MaxTTL int    `json:"max_ttl_seconds"`
		} `json:"token"`
	}
	if err := json.Unmarshal(b, &c); err != nil {
		t.Fatal(err)
	}
	if c.SpecVersion != SpecVersion || c.Envelope.SchemaVersion != SchemaVersion {
		t.Fatalf("versions: %s %s", c.SpecVersion, c.Envelope.SchemaVersion)
	}
	if len(c.ErrorCodes.Core) != len(coreCodes) {
		t.Fatalf("core codes: contract %d, generated %d", len(c.ErrorCodes.Core), len(coreCodes))
	}
	for name, spec := range c.ErrorCodes.Core {
		got, ok := coreCodes[name]
		if !ok || got.exit != spec.Exit || got.retryable != spec.Retryable {
			t.Errorf("%s: contract %+v, generated %+v", name, spec, got)
		}
	}
	same := func(name string, a, b []string) {
		a, b = append([]string{}, a...), append([]string{}, b...)
		sort.Strings(a)
		sort.Strings(b)
		if len(a) != len(b) {
			t.Errorf("%s differs: %v vs %v", name, a, b)
			return
		}
		for i := range a {
			if a[i] != b[i] {
				t.Errorf("%s differs: %v vs %v", name, a, b)
				return
			}
		}
	}
	same("problem", c.ErrorDetails.Problem, problemValues)
	same("conflict", c.ErrorDetails.Conflict, conflictValues)
	same("excluded_from_binding", c.ControlFlags.Excluded, excludedFromBinding)
	if c.Token.Format != tokenPattern || int(tokenLifetime.Seconds()) > c.Token.MaxTTL {
		t.Fatalf("token: %s, lifetime %v over %d s", c.Token.Format, tokenLifetime, c.Token.MaxTTL)
	}
}
