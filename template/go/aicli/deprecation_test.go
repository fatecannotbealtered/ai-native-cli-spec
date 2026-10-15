// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

// Deprecation (CLI-081), diagnostics (CLI-102) and the default flag synonyms
// (CLI-028).

func deprecatedHarness(t *testing.T) *tharness {
	t.Helper()
	h := newHarness(t)
	h.build = func(u *upstream) *App {
		a := testApp(u)
		old := &cobra.Command{Use: "old-list", RunE: func(cmd *cobra.Command, _ []string) error {
			return a.OK(cmd, map[string]any{"listed": true})
		}}
		old.Flags().Int("max", 20, "Maximum notes to return")
		childNamed(a.Root, "note").AddCommand(old)
		a.Schema("old_result", SchemaDef{Shape: "object"})
		a.Register(old, Meta{Summary: "List notes the old way", Effects: []Effect{Observe}, Output: "old_result",
			Examples: []string{"notes note old-list"}, Deprecated: "use `note list`",
			DeprecatedFlags: map[string]string{"max": "use `note list --limit`."}})
		return a
	}
	return h
}

func TestDeprecatedCommandAddsNotices(t *testing.T) {
	h := deprecatedHarness(t)
	r := h.run("note", "old-list", "--max", "5")
	if r.exit != 0 {
		t.Fatalf("a deprecated command keeps working: %s", r)
	}
	mustJSON(t, "notices", r.env["meta"].(map[string]any)["notices"], `[
		{"type":"deprecated","severity":"warning","message":"`+"`note old-list`"+` is deprecated: use `+"`note list`"+`. It will be removed in notes 2.0.0."},
		{"type":"deprecated","severity":"warning","message":"`+"`--max`"+` is deprecated: use `+"`note list --limit`"+`."}]`)
	r = h.run("note", "old-list")
	if n := r.env["meta"].(map[string]any)["notices"].([]any); len(n) != 1 {
		t.Fatalf("without the deprecated flag one notice: %s", r)
	}
	if r := h.run("note", "list"); r.env["meta"].(map[string]any)["notices"] != nil {
		t.Fatalf("a current command has no notice: %s", r)
	}
}

func TestDeprecatedHelpReferenceAndCommandMap(t *testing.T) {
	h := deprecatedHarness(t)
	help := h.run("note", "old-list", "--help").stdout
	if !strings.HasPrefix(help, "Deprecated: use `note list`. List notes the old way\n") ||
		!strings.Contains(help, "--max (integer, default 20)  Deprecated: use `note list --limit`. Maximum notes to return") {
		t.Fatalf("help:\n%s", help)
	}
	if group := h.run("note", "--help").stdout; !strings.Contains(group, "Deprecated: use `note list`. List notes the old way") {
		t.Fatalf("group help:\n%s", group)
	}
	r := h.run("reference", "--command", "note old-list")
	entry := r.data()["command"].(map[string]any)
	if entry["summary"] != "Deprecated: use `note list`. List notes the old way" {
		t.Fatalf("reference summary: %v", entry["summary"])
	}
	for _, p := range entry["params"].([]any) {
		if pm := p.(map[string]any); pm["name"] == "max" && pm["description"] != "Deprecated: use `note list --limit`. Maximum notes to return" {
			t.Fatalf("reference flag description: %v", pm)
		}
	}
	if err := validateReferenceCommand(r.data()); err != nil {
		t.Fatal(err)
	}
	want := "- `note`: list, get, create, update, delete (dangerous), share (dangerous), bulk-tag, tail, export; `note comment` add\n"
	if got := h.app().CommandMap(); got != want {
		t.Fatalf("the Skill's command map leaves deprecated commands out:\n%q", got)
	}
	a := h.app()
	a.reg.meta[childNamed(childNamed(a.Root, "note"), "old-list")].DeprecatedFlags["nope"] = "x"
	a.InstallBuiltins()
	a.prepareTree()
	if !contains(a.checkTree(), `"note old-list": Meta.DeprecatedFlags names --nope, which the command does not define`) {
		t.Fatalf("tree check: %v", a.checkTree())
	}
}

func TestDebugLinesGoToStderrWithSecretsRedacted(t *testing.T) {
	h := newHarness(t)
	h.build = func(u *upstream) *App {
		a := testApp(u)
		a.Settings = []Setting{{Name: "api_key", Secret: true}}
		ping := &cobra.Command{Use: "ping", RunE: func(cmd *cobra.Command, _ []string) error {
			token, _ := cmd.Flags().GetString("token")
			key, _, err := a.Setting(cmd, "api_key")
			if err != nil {
				return err
			}
			a.Debugf("calling the upstream with token %s and key %s", token, key)
			return a.OK(cmd, map[string]any{"pong": true})
		}}
		ping.Flags().String("token", "", "Access token")
		a.Root.AddCommand(ping)
		a.Schema("pong", SchemaDef{Shape: "object"})
		a.Register(ping, Meta{Summary: "Ping the upstream", Effects: []Effect{Observe}, Output: "pong", Secrets: []string{"token"},
			Examples: []string{"notes ping"}})
		return a
	}
	h.env["NOTES_API_KEY"] = "k3y-from-the-env"
	quiet := h.run("ping", "--token", "s3cr3t-flag-value")
	if quiet.exit != 0 || quiet.stderr != "" {
		t.Fatalf("without NOTES_DEBUG nothing goes to stderr: %s", quiet)
	}
	h.env["NOTES_DEBUG"] = "1"
	r := h.run("ping", "--token", "s3cr3t-flag-value")
	if r.exit != quiet.exit || !strings.Contains(r.stderr, "debug: calling the upstream with token *** and key ***\n") {
		t.Fatalf("debug line: %s", r)
	}
	if strings.Contains(r.stdout+r.stderr, "s3cr3t-flag-value") || strings.Contains(r.stdout+r.stderr, "k3y-from-the-env") {
		t.Fatalf("a secret leaked: %s", r)
	}
	if strings.Contains(r.stdout, "debug:") {
		t.Fatalf("debug lines never go to stdout: %s", r)
	}
	for _, want := range []string{"debug: state directory: " + h.state + "\n", "debug: settings file: ", "debug: active profile: none\n"} {
		if !strings.Contains(r.stderr, want) {
			t.Errorf("missing library line %q in:\n%s", want, r.stderr)
		}
	}
	if r := h.run("context"); !strings.Contains(r.stderr, "debug: scope file: "+h.scope+"\n") {
		t.Errorf("context logs the scope file:\n%s", r.stderr)
	}
	h.env["NOTES_DEBUG"] = "0"
	if r := h.run("ping", "--token", "x"); r.stderr != "" {
		t.Fatalf("NOTES_DEBUG=0 is off: %s", r)
	}
}

func TestDefaultFlagSynonymsSuggest(t *testing.T) {
	h := newHarness(t)
	r := h.run("note", "list", "--per-page", "5")
	if r.exit != 2 || r.details()["problem"] != "unknown_flag" || r.details()["parameter"] != "--per-page" {
		t.Fatalf("--per-page: %s", r)
	}
	if dym := r.details()["did_you_mean"].([]any); dym[0] != "--limit" || r.details()["next_action"] != "Run `notes note list --limit 5`." {
		t.Fatalf("--per-page should suggest --limit: %v %v", dym, r.details()["next_action"])
	}
	// only when the command has the target flag: note list has no --query
	if r := h.run("note", "list", "--search", "x"); listHasString(r.details()["did_you_mean"], "--query") {
		t.Fatalf("a synonym of a flag the command lacks: %s", r)
	}
	// the tool's own entry replaces the default
	h.build = func(u *upstream) *App {
		a := testApp(u)
		a.FlagSynonyms["per-page"] = []string{"tag"}
		return a
	}
	if r := h.run("note", "list", "--per-page", "5"); r.details()["did_you_mean"].([]any)[0] != "--tag" {
		t.Fatalf("the tool's entry wins: %s", r)
	}
}
