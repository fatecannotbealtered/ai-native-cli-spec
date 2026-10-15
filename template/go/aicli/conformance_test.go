// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/spf13/cobra"
)

// recordTB captures the suite's reports instead of failing the test.
type recordTB struct {
	mu     sync.Mutex
	errors []string
	fatal  []string
	logs   []string
}

func (r *recordTB) Helper() {}
func (r *recordTB) Errorf(format string, args ...any) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.errors = append(r.errors, fmt.Sprintf(format, args...))
}
func (r *recordTB) Fatalf(format string, args ...any) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.fatal = append(r.fatal, fmt.Sprintf(format, args...))
}
func (r *recordTB) Logf(format string, args ...any) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.logs = append(r.logs, fmt.Sprintf(format, args...))
}

func (r *recordTB) has(prefix string) bool {
	for _, e := range append(append([]string{}, r.errors...), r.fatal...) {
		if strings.HasPrefix(e, prefix) {
			return true
		}
	}
	return false
}

var testSamples = map[string][]string{
	"note create":      {"--title", "Hello", "--body", "Body text", "--tag", "a,b"},
	"note update":      {"1", "--title", "New title"},
	"note delete":      {"2"},
	"note share":       {"1", "--to", "bob@example.com", "--token", "sample-secret-token"},
	"note bulk-tag":    {"--ids", "1,2", "--tag", "work"},
	"note comment add": {"1", "--text", "Looks good"},
}

// exportDir holds the conformance sample's --output file: inside the scope's outputs
// (a new file) and roots (once G04 wrote it, the later checks overwrite it, C1).
var exportDir = func() string {
	dir, err := os.MkdirTemp("", "aicli-conformance-export-")
	if err != nil {
		panic(err)
	}
	return dir
}()

// TestMain removes exportDir once every test of the package ran.
func TestMain(m *testing.M) {
	code := m.Run()
	_ = os.RemoveAll(exportDir)
	os.Exit(code)
}

func testHarness(build func(*upstream) *App) (Harness, *upstream) {
	u := newUpstream()
	samples := map[string][]string{"note export": {"--output", filepath.Join(exportDir, "notes.tsv")}}
	for k, v := range testSamples {
		samples[k] = v
	}
	return Harness{
		NewApp:  func(map[string]string) *App { return build(u) },
		Writes:  u.Writes,
		Reset:   u.reset,
		Samples: samples,
		Scope: map[string]any{"scope_id": "scp_conformance", "tool": "notes", "granted_by": "test",
			"granted_at": "2026-01-01T00:00:00Z", "expires_at": nil, "effects": []string{"change", "deliver", "artifact", "persist"},
			"dangerous": true, "destinations": []string{"bob@example.com"}, "outputs": []string{exportDir}, "roots": []string{exportDir}},
	}, u
}

func TestConformanceSuitePassesForTheTestTool(t *testing.T) {
	h, _ := testHarness(testApp)
	RunConformance(t, h)
}

func TestConformanceCatchesAWriteThatSkipsTheGate(t *testing.T) {
	h, u := testHarness(func(u *upstream) *App {
		a := testApp(u)
		c := a.commandByPath("note comment add")
		orig := c.RunE
		c.RunE = func(cmd *cobra.Command, args []string) error {
			u.write(func() {}) // writes before asking the gate
			return orig(cmd, args)
		}
		return a
	})
	_ = u
	rec := &recordTB{}
	RunConformance(rec, h)
	if !rec.has("G01") {
		t.Fatalf("the suite did not catch a write before the gate:\n%s", strings.Join(rec.errors, "\n"))
	}
}

func TestConformanceCatchesAMissingSample(t *testing.T) {
	h, _ := testHarness(testApp)
	samples := map[string][]string{}
	for k, v := range h.Samples {
		if k != "note share" {
			samples[k] = v
		}
	}
	samples["note nothing"] = []string{"x"}
	h.Samples = samples
	rec := &recordTB{}
	RunConformance(rec, h)
	if !rec.has(`G00: write command "note share" has no sample`) || !rec.has(`G00: Harness.Samples names "note nothing"`) {
		t.Fatalf("G00 not reported:\n%s", strings.Join(rec.errors, "\n"))
	}
}

func TestConformanceCatchesAnInputReadOutsideTheBinding(t *testing.T) {
	// The tool reads the new title from an environment variable instead of a flag:
	// the token cannot bind it, so a confirm can apply a different title. The suite
	// cannot see the variable, but it does notice the unbound positional through G03
	// when the tool ignores a declared input; here the tool ignores --title.
	h, _ := testHarness(func(u *upstream) *App {
		a := testApp(u)
		c := a.commandByPath("note update")
		c.RunE = func(cmd *cobra.Command, _ []string) error {
			id, _ := cmd.Flags().GetString("id")
			return a.Write(cmd, WriteSpec{
				Target: Target{Key: "notes/" + id},
				Preview: func(context.Context) (Preview, error) {
					return Preview{CanApply: true, Changes: []FieldChange{{Field: "title", After: "fixed"}}}, nil
				},
				Apply: func(context.Context, Op) (Result, error) {
					u.write(func() {})
					return Result{}, nil
				},
			})
		}
		return a
	})
	rec := &recordTB{}
	RunConformance(rec, h)
	// The binding is generic, so --title is still bound and G03 passes: the tool
	// cannot opt an input out of the token.
	for _, e := range rec.errors {
		if strings.HasPrefix(e, "G03") {
			t.Fatalf("generic binding should still cover the ignored flag: %s", e)
		}
	}
}

// A receipt the gate had to correct (verification passed with nothing observed) is
// still a tool bug: the suite reports it under G04.
func TestConformanceCatchesAnUnverifiedPass(t *testing.T) {
	h, _ := testHarness(func(u *upstream) *App {
		a := testApp(u)
		c := a.commandByPath("note comment add")
		c.RunE = func(cmd *cobra.Command, _ []string) error {
			id, _ := cmd.Flags().GetString("id")
			return a.Write(cmd, WriteSpec{
				Target: Target{Key: "notes/" + id + "#comments", Kind: "comments"},
				Preview: func(context.Context) (Preview, error) {
					return Preview{Action: "Comment", CanApply: true, Changes: []FieldChange{{Field: "comments", Add: []string{"x"}}}}, nil
				},
				Apply: func(context.Context, Op) (Result, error) {
					u.write(func() {})
					return Result{Outcome: Outcome{Verification: "passed"}}, nil
				},
			})
		}
		return a
	})
	rec := &recordTB{}
	RunConformance(rec, h)
	if !rec.has("G04: note comment add: Apply reported verification passed with an empty Result.Observed") {
		t.Fatalf("G04 should report the unverified pass:\n%s", strings.Join(rec.errors, "\n"))
	}
}

func TestConformanceCatchesBadSelfDescription(t *testing.T) {
	h, _ := testHarness(func(u *upstream) *App {
		a := testApp(u)
		a.RiskTier = "T0"
		bad := &cobra.Command{Use: "orphan", RunE: func(cmd *cobra.Command, _ []string) error { return a.OK(cmd, nil) }}
		a.Root.AddCommand(bad)
		return a
	})
	rec := &recordTB{}
	RunConformance(rec, h)
	if !rec.has("R02") {
		t.Fatalf("R02 not reported:\n%s", strings.Join(rec.errors, "\n"))
	}
}

func TestCoverageRecorderAndR06(t *testing.T) {
	dir := t.TempDir()
	cov := filepath.Join(dir, "coverage.jsonl")
	h := newHarness(t)
	h.env["NOTES_COVERAGE_FILE"] = cov
	h.run("note", "get", "1")
	h.run("note", "list")
	b, err := os.ReadFile(cov)
	if err != nil || !strings.Contains(string(b), `"command":"note get"`) {
		t.Fatalf("coverage file: %q %v", b, err)
	}
	hh, _ := testHarness(testApp)
	hh.CoverageFile = cov
	rec := &recordTB{}
	RunConformance(rec, hh)
	if !rec.has(`R06: command "note create" was never executed`) || rec.has(`R06: command "note get"`) {
		t.Fatalf("R06:\n%s", strings.Join(rec.errors, "\n"))
	}
}
