package main

import (
	"path/filepath"
	"strings"
	"testing"
)

func TestChildEnv(t *testing.T) {
	priv := privateConfig{
		SetEnv:          map[string]string{"TOOL_HOST": "http://upstream.test", "TOOL_TOKEN": "agent-token"},
		DropEnv:         []string{"GENERIC_TOKEN"},
		DropEnvPrefixes: []string{"TOOL_"},
	}
	home := filepath.Join("trial", "home")
	environ := []string{
		"PATH=/usr/bin",
		"HOME=/home/someone",
		"XDG_CONFIG_HOME=/home/someone/.config",
		"generic_token=leak",        // dropped case-insensitively
		"TOOL_STATE_DIR=/elsewhere", // dropped by prefix
		"TOOL_TOKEN=agent-supplied", // replaced by set_env
		"OTHER=kept",
	}
	env := map[string][]string{}
	for _, kv := range childEnv(environ, priv, home) {
		k, v, _ := strings.Cut(kv, "=")
		env[k] = append(env[k], v)
	}
	want := map[string]string{
		"PATH":         "/usr/bin",
		"OTHER":        "kept",
		"HOME":         home,
		"USERPROFILE":  home,
		"APPDATA":      filepath.Join(home, "AppData", "Roaming"),
		"LOCALAPPDATA": filepath.Join(home, "AppData", "Local"),
		"TOOL_HOST":    "http://upstream.test",
		"TOOL_TOKEN":   "agent-token",
	}
	for k, v := range want {
		if got := env[k]; len(got) != 1 || got[0] != v {
			t.Errorf("%s = %v, want exactly [%s]", k, got, v)
		}
	}
	for _, k := range []string{"XDG_CONFIG_HOME", "generic_token", "TOOL_STATE_DIR"} {
		if got, ok := env[k]; ok {
			t.Errorf("%s should be dropped, got %v", k, got)
		}
	}
}

func TestTrialSetEnv(t *testing.T) {
	priv := privateConfig{
		SetEnv:   map[string]string{"TOOL_HOST": "http://upstream.test", "TOOL_TOKEN": "agent-token"},
		TrialEnv: []string{"tool_host"},
	}
	// no overrides: the private set_env as it is
	got, err := trialSetEnv(priv, runConfig{RealBin: "bin"})
	if err != nil || len(got) != 2 || got["TOOL_HOST"] != "http://upstream.test" {
		t.Fatalf("no overrides: got %v, %v", got, err)
	}
	// an allowed override replaces the value, whatever the case of its name
	got, err = trialSetEnv(priv, runConfig{RealBin: "bin", SetEnv: map[string]string{"Tool_Host": "http://127.0.0.1:5000"}})
	if err != nil {
		t.Fatal(err)
	}
	if got["Tool_Host"] != "http://127.0.0.1:5000" || got["TOOL_TOKEN"] != "agent-token" || len(got) != 2 {
		t.Errorf("allowed override: got %v", got)
	}
	if _, ok := got["TOOL_HOST"]; ok {
		t.Errorf("the overridden variable is still set under its old spelling: %v", got)
	}
	// anything not in trial_env is refused, set_env keys included
	for _, k := range []string{"TOOL_SCOPE_FILE", "TOOL_TOKEN"} {
		if _, err := trialSetEnv(priv, runConfig{RealBin: "bin", SetEnv: map[string]string{k: "x"}}); err == nil {
			t.Errorf("%s: override accepted, want an error", k)
		}
	}
	// a private config without trial_env allows no override
	priv.TrialEnv = nil
	if _, err := trialSetEnv(priv, runConfig{RealBin: "bin", SetEnv: map[string]string{"TOOL_HOST": "x"}}); err == nil {
		t.Error("override accepted without trial_env")
	}
}
