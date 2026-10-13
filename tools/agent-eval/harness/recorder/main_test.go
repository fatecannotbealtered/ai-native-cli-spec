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
