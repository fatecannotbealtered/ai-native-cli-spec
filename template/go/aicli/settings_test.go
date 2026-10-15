// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

// settingsHarness is the test tool with three settings and a probe command that
// reports two of them with their sources.
func settingsHarness(t *testing.T) *tharness {
	t.Helper()
	h := newHarness(t)
	h.build = func(u *upstream) *App {
		a := testApp(u)
		a.Settings = []Setting{
			{Name: "host", Flag: "host", Default: "https://notes.example.com", Description: "Notes service URL"},
			{Name: "api_token", Env: "TOKEN", Secret: true, Description: "API token"},
			{Name: "page_size", Default: "20"},
		}
		probe := &cobra.Command{Use: "probe", RunE: func(cmd *cobra.Command, _ []string) error {
			out := map[string]any{}
			for _, name := range []string{"host", "page_size"} {
				v, src, err := a.Setting(cmd, name)
				if err != nil {
					return err
				}
				out[name], out[name+"_source"] = v, src
			}
			return a.OK(cmd, out)
		}}
		probe.Flags().String("host", "", "Notes service URL")
		a.Root.AddCommand(probe)
		a.Schema("probe_result", SchemaDef{Shape: "object"})
		a.Register(probe, Meta{Summary: "Show settings", Effects: []Effect{Observe}, Output: "probe_result", Examples: []string{"notes probe"}})
		return a
	}
	return h
}

// writeSettings writes <state>/settings.json and returns its path.
func writeSettings(t *testing.T, h *tharness, content string) string {
	t.Helper()
	path := filepath.Join(h.state, "settings.json")
	if err := os.MkdirAll(h.state, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func contextProfile(t *testing.T, h *tharness, args ...string) map[string]any {
	t.Helper()
	r := h.run(append([]string{"context"}, args...)...)
	if r.exit != 0 {
		t.Fatalf("context must not fail on the settings file: %s", r)
	}
	if err := validateContext(r.data()); err != nil {
		t.Fatalf("context does not validate: %v\n%s", err, r.stdout)
	}
	p, _ := r.data()["profile"].(map[string]any)
	return p
}

func TestSettingPrecedence(t *testing.T) {
	h := settingsHarness(t)
	probe := func(args ...string) map[string]any {
		t.Helper()
		r := h.run(append([]string{"probe"}, args...)...)
		if r.exit != 0 {
			t.Fatalf("probe: %s", r)
		}
		return r.data()
	}
	mustJSON(t, "defaults", probe(), `{"host":"https://notes.example.com","host_source":"default","page_size":"20","page_size_source":"default"}`)
	writeSettings(t, h, `{"profiles": {"work": {"host": "https://work.example", "page_size": 50}}}`)
	mustJSON(t, "profile", probe(), `{"host":"https://work.example","host_source":"profile","page_size":"50","page_size_source":"profile"}`)
	h.env["NOTES_HOST"] = "https://env.example"
	if d := probe(); d["host"] != "https://env.example" || d["host_source"] != "env" || d["page_size_source"] != "profile" {
		t.Fatalf("env: %v", d)
	}
	if d := probe("--host", "https://flag.example"); d["host"] != "https://flag.example" || d["host_source"] != "flag" {
		t.Fatalf("flag: %v", d)
	}
	// <PREFIX>_CONFIG names another file
	other := filepath.Join(h.dir, "other.json")
	if err := os.WriteFile(other, []byte(`{"profiles": {"x": {"page_size": "5"}}}`), 0o600); err != nil {
		t.Fatal(err)
	}
	h.env["NOTES_SETTINGS_FILE"] = other
	if d := probe(); d["page_size"] != "5" {
		t.Fatalf("NOTES_SETTINGS_FILE: %v", d)
	}
}

func TestProfileSelectionOrder(t *testing.T) {
	h := settingsHarness(t)
	if p := contextProfile(t, h); p["name"] != nil || p["source"] != "none" || p["file"] != nil || len(p["profiles"].([]any)) != 0 {
		t.Fatalf("no settings file: %v", p)
	}
	path := writeSettings(t, h, `{"default_profile": "work", "profiles": {"work": {"host": "https://w.example"}, "home": {"host": "https://h.example"}}}`)
	mustJSON(t, "default_profile", contextProfile(t, h), `{"name":"work","source":"config","profiles":["home","work"],"file":`+jsonString(path)+`}`)
	h.env["NOTES_PROFILE"] = "home"
	if p := contextProfile(t, h); p["name"] != "home" || p["source"] != "env" {
		t.Fatalf("env: %v", p)
	}
	if p := contextProfile(t, h, "--profile", "work"); p["name"] != "work" || p["source"] != "flag" {
		t.Fatalf("flag over env: %v", p)
	}
	if r := h.run("probe", "--profile", "work"); r.data()["host"] != "https://w.example" {
		t.Fatalf("the active profile's value: %s", r)
	}
	delete(h.env, "NOTES_PROFILE")
	writeSettings(t, h, `{"profiles": {"only": {}}}`)
	if p := contextProfile(t, h); p["name"] != "only" || p["source"] != "single" {
		t.Fatalf("single: %v", p)
	}
	writeSettings(t, h, `{"default_profile": null, "profiles": {"a": {}, "b": {}}}`)
	if p := contextProfile(t, h); p["name"] != nil || p["source"] != "none" {
		t.Fatalf("two profiles and no default: %v", p)
	}
	r := h.run("doctor")
	if c := checkNamed(r, "settings"); c["status"] != "pass" || c["message"] != filepath.Join(h.state, "settings.json")+" is valid; no profile is active." {
		t.Fatalf("doctor settings, no profile: %v", c)
	}
}

func TestInvalidConfigIsConfigError(t *testing.T) {
	h := settingsHarness(t)
	r := h.run("doctor")
	if c := checkNamed(r, "settings"); c["status"] != "pass" || c["message"] != "No settings file; defaults and environment variables apply." || c["fix"] != nil {
		t.Fatalf("doctor without a settings file: %v", c)
	}
	cases := map[string]string{
		`{"profiles": {"work": {"host": "a", "host": "b"}}}`:    `the key "host" appears twice`,
		`{"profiles": {"work": {"api_token": "s3cret"}}}`:       "`api_token` is a secret; secrets go in the OS keyring, not the settings file (SEC-040)",
		`{"profiles": {"Work": {}}}`:                            "the profile name `Work` is not allowed (use a-z, 0-9, _ and -, up to 64 characters)",
		`{"profiles": {"work": {"colour": "blue"}}}`:            "profile `work` sets `colour`, which is not a setting of notes",
		`{"profiles": {"work": {"host": ["a"]}}}`:               "`host` in profile `work` must be a string, number or true/false",
		`{"profile": {}}`:                                       "unknown key `profile` (did you mean `profiles`?)",
		`{"default_profile": "home", "profiles": {"work": {}}}`: "default_profile `home` is not one of its profiles",
		"\xef\xbb\xbf{}":                                        "it starts with a byte order mark",
		`{} {}`:                                                 "it holds more than one JSON value",
	}
	for content, reason := range cases {
		path := writeSettings(t, h, content)
		r := h.run("probe")
		want := "The settings file " + path + " is invalid: " + reason + "."
		if r.exit != 4 || r.code() != "E_CONFIG" || r.message() != want {
			t.Fatalf("%s:\n got %s\nwant %s", content, r, want)
		}
		if r.details()["next_action"] != "Ask the user to fix "+path+"; `notes context` shows which file is read." || r.details()["dispatch_state"] != "not_started" {
			t.Fatalf("details: %v", r.details())
		}
		mustJSON(t, "context of an invalid file", contextProfile(t, h), `{"name":null,"source":"none","profiles":[],"file":`+jsonString(path)+`}`)
		c := checkNamed(h.run("doctor"), "settings")
		if c["status"] != "fail" || c["message"] != want || c["fix"] != "Fix "+path+", or move it aside to use defaults and environment variables." {
			t.Fatalf("doctor settings check: %v", c)
		}
	}
}

func TestUnknownProfileIsConfigError(t *testing.T) {
	h := settingsHarness(t)
	path := writeSettings(t, h, `{"profiles": {"work": {}, "home": {}}}`)
	r := h.run("probe", "--profile", "wrk")
	if r.exit != 4 || r.code() != "E_CONFIG" || r.message() != "The requested profile is not in the settings file." {
		t.Fatalf("unknown profile: %s", r)
	}
	mustJSON(t, "details", r.details(), `{"dispatch_state":"not_started","next_action":"Use one of the profiles in details.did_you_mean, or add the profile to `+
		strings.Trim(jsonString(path), `"`)+`.","profile":"wrk","source":"flag","did_you_mean":["work"]}`)
	if !listHasString(r.env["meta"].(map[string]any)["untrusted"], "error.details.profile") {
		t.Fatalf("the requested name is untrusted: %s", r)
	}
	if p := contextProfile(t, h, "--profile", "wrk"); p["name"] != nil || p["source"] != "none" || len(p["profiles"].([]any)) != 2 {
		t.Fatalf("context with an unknown profile: %v", p)
	}
	h.env["NOTES_PROFILE"] = "work"
	if c := checkNamed(h.run("doctor"), "settings"); c["status"] != "pass" || c["message"] != path+" is valid; profile `work` is active (env)." {
		t.Fatalf("doctor: %v", c)
	}
	h.env["NOTES_PROFILE"] = "nope"
	if c := checkNamed(h.run("doctor"), "settings"); c["status"] != "fail" || c["message"] != "The requested profile is not in the settings file." {
		t.Fatalf("doctor with an unknown profile: %v", c)
	}
}

// --profile is a root flag only when settings are declared, and a command may not
// define its own.
func TestProfileFlagIsReserved(t *testing.T) {
	plain := newHarness(t)
	if r := plain.run("context", "--profile", "work"); r.code() != "E_USAGE" || r.details()["problem"] != "unknown_flag" {
		t.Fatalf("without settings there is no --profile: %s", r)
	}
	if _, has := plain.run("context").data()["profile"]; has {
		t.Fatal("without settings context has no profile")
	}
	a := settingsHarness(t).app()
	own := &cobra.Command{Use: "mine", RunE: func(*cobra.Command, []string) error { return nil }}
	own.Flags().String("profile", "", "my own profile")
	a.Root.AddCommand(own)
	a.Register(own, Meta{Summary: "Mine", Effects: []Effect{Observe}, Output: "note", Examples: []string{"notes mine"}})
	a.Settings = append(a.Settings, Setting{Name: "Bad Name"}, Setting{Name: "host"})
	a.InstallBuiltins()
	a.prepareTree()
	all := strings.Join(a.checkTree(), "\n")
	for _, want := range []string{`"mine" defines --profile, which aicli reserves for the active profile when App.Settings is set (CLI-101)`,
		`App.Settings[3]: "Bad Name" is not a snake_case name`, `App.Settings: "host" is declared twice`} {
		if !strings.Contains(all, want) {
			t.Errorf("tree check misses %q:\n%s", want, all)
		}
	}
}

func checkNamed(r tout, name string) map[string]any {
	checks, _ := r.data()["checks"].([]any)
	for _, c := range checks {
		if m, _ := c.(map[string]any); m["check"] == name {
			return m
		}
	}
	return nil
}

func jsonString(s string) string {
	b, _ := encodeJSON(s, false)
	return strings.TrimSpace(string(b))
}
