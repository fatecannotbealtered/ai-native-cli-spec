// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"unicode/utf8"

	"github.com/spf13/cobra"
	"github.com/spf13/pflag"
)

// Settings and profiles (CLI-100, CLI-101). A tool declares the values it reads
// (host, project, ...) in App.Settings and reads each with App.Setting: a command
// flag, then <PREFIX>_<ENV>, then the active profile of the settings file, then the
// default. The settings file is <PREFIX>_SETTINGS_FILE, else
// <state_dir>/settings.json (not config.json, which 1.x tools keep in their own
// format):
//
//	{"default_profile": "work", "profiles": {"work": {"host": "https://gitlab.acme.test"}}}
//
// The active profile is --profile (a root flag aicli registers when App.Settings is
// set), then <PREFIX>_PROFILE, then default_profile, then the only profile. Secrets
// never go in the settings file (SEC-040).

// Setting is one value a tool reads through App.Setting. Name is snake_case; Env
// defaults to upper(Name) and is read as <PREFIX>_<Env>; Flag names a command flag
// that overrides the setting when the running command has it and it is set. A Secret
// setting never comes from the settings file and is redacted from debug lines.
type Setting struct {
	Name        string
	Env         string
	Flag        string
	Default     string
	Secret      bool
	Description string
}

// envName is the setting's environment variable without the tool prefix.
func (s Setting) envName() string {
	if s.Env != "" {
		return s.Env
	}
	return strings.ToUpper(s.Name)
}

const maxSettingsBytes = 64 << 10

var profileNamePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)

// settingsFile is the parsed settings file. reason is set when the file is invalid.
type settingsFile struct {
	path           string
	exists         bool
	reason         string
	defaultProfile string
	profiles       map[string]map[string]string
	names          []string // profile names, sorted
}

// profileState is the active profile and where it came from (context.profile).
// requested is a profile asked for by --profile or <PREFIX>_PROFILE that the
// settings file does not define.
type profileState struct {
	file      settingsFile
	name      string
	source    string
	requested string
}

// settingsPath is <PREFIX>_SETTINGS_FILE (made absolute), else
// <state_dir>/settings.json; "" when no state directory can be resolved.
func (a *App) settingsPath() string {
	if p := strings.TrimSpace(a.env(a.envPrefix() + "_SETTINGS_FILE")); p != "" {
		if abs, err := filepath.Abs(p); err == nil {
			return abs
		}
		return p
	}
	dir, e := a.stateDirPath()
	if e != nil {
		return ""
	}
	return filepath.Join(dir, "settings.json")
}

// loadSettings reads and strictly validates the settings file at path (CLI-101),
// the way loadScope reads the scope file. A missing file is no profiles.
func (a *App) loadSettings(path string) settingsFile {
	sf := settingsFile{path: path, profiles: map[string]map[string]string{}, names: []string{}}
	if path == "" {
		return sf
	}
	info, err := os.Lstat(path)
	if errors.Is(err, fs.ErrNotExist) {
		return sf
	}
	sf.exists = true
	if err != nil {
		sf.reason = "it cannot be read"
		return sf
	}
	mode := info.Mode()
	if mode&fs.ModeSymlink != 0 || mode&fs.ModeIrregular != 0 || !mode.IsRegular() {
		sf.reason = "it must be a regular file, not a link"
		return sf
	}
	if info.Size() > maxSettingsBytes {
		sf.reason = "it is larger than 64 KiB"
		return sf
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		sf.reason = "it cannot be read"
		return sf
	}
	def, profiles, reason := a.parseSettings(raw)
	if reason != "" {
		sf.reason = reason
		return sf
	}
	sf.defaultProfile, sf.profiles = def, profiles
	sf.names = sortedKeys(profiles)
	return sf
}

// parseSettings parses a settings file strictly: one UTF-8 JSON object without a
// BOM or duplicate keys, only default_profile and profiles at the top, profile names
// that match the pattern, declared settings with scalar values, no secrets.
// Problems are reported in sorted key order, the first one wins.
func (a *App) parseSettings(raw []byte) (string, map[string]map[string]string, string) {
	if bytes.HasPrefix(raw, []byte("\xef\xbb\xbf")) {
		return "", nil, "it starts with a byte order mark"
	}
	if !utf8.Valid(raw) {
		return "", nil, "it is not valid UTF-8"
	}
	if _, err := strictKeys(raw); err != nil {
		return "", nil, err.Error()
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var top map[string]any
	if err := dec.Decode(&top); err != nil {
		return "", nil, "it is not valid JSON"
	}
	if _, err := dec.Token(); !errors.Is(err, io.EOF) {
		return "", nil, "it holds more than one JSON value"
	}
	known := []string{"default_profile", "profiles"}
	for _, k := range sortedKeys(top) {
		if !contains(known, k) {
			reason := fmt.Sprintf("unknown key `%s`", truncateRunes(k, 40))
			if near := closestWords(k, known); len(near) > 0 {
				reason += fmt.Sprintf(" (did you mean `%s`?)", near[0])
			}
			return "", nil, reason
		}
	}
	def := ""
	switch v := top["default_profile"].(type) {
	case nil:
	case string:
		def = v
	default:
		return "", nil, "default_profile must be a string or null"
	}
	profiles := map[string]map[string]string{}
	rawProfiles := map[string]any{}
	if p, has := top["profiles"]; has && p != nil {
		obj, ok := p.(map[string]any)
		if !ok {
			return "", nil, "profiles must be an object"
		}
		rawProfiles = obj
	}
	for _, name := range sortedKeys(rawProfiles) {
		shown := truncateRunes(name, 64)
		if !profileNamePattern.MatchString(name) {
			return "", nil, fmt.Sprintf("the profile name `%s` is not allowed (use a-z, 0-9, _ and -, up to 64 characters)", shown)
		}
		values, ok := rawProfiles[name].(map[string]any)
		if !ok {
			return "", nil, fmt.Sprintf("profile `%s` must be an object", shown)
		}
		profile := map[string]string{}
		for _, key := range sortedKeys(values) {
			s, declared := a.settingNamed(key)
			switch {
			case !declared:
				return "", nil, fmt.Sprintf("profile `%s` sets `%s`, which is not a setting of %s", shown, truncateRunes(key, 40), a.toolName())
			case s.Secret:
				return "", nil, fmt.Sprintf("`%s` is a secret; secrets go in the OS keyring, not the settings file (SEC-040)", key)
			}
			switch v := values[key].(type) {
			case string:
				profile[key] = v
			case json.Number:
				profile[key] = v.String()
			case bool:
				profile[key] = fmt.Sprint(v)
			default:
				return "", nil, fmt.Sprintf("`%s` in profile `%s` must be a string, number or true/false", key, shown)
			}
		}
		profiles[name] = profile
	}
	if def != "" {
		if _, ok := profiles[def]; !ok {
			return "", nil, fmt.Sprintf("default_profile `%s` is not one of its profiles", truncateRunes(def, 64))
		}
	}
	return def, profiles, ""
}

// settingNamed finds a declared setting.
func (a *App) settingNamed(name string) (Setting, bool) {
	for _, s := range a.Settings {
		if s.Name == name {
			return s, true
		}
	}
	return Setting{}, false
}

// profileState resolves the active profile: --profile, then <PREFIX>_PROFILE, then
// default_profile, then the only profile. It is computed once per run.
func (a *App) profileState() profileState {
	if st := a.run; st != nil && st.profile != nil {
		return *st.profile
	}
	ps := profileState{file: a.loadSettings(a.settingsPath()), source: "none"}
	switch {
	case !ps.file.exists:
		a.debugOnce("settings file: %s (not found)", ps.file.path)
	case ps.file.reason != "":
		a.debugOnce("settings file: %s (invalid: %s)", ps.file.path, ps.file.reason)
	default:
		a.debugOnce("settings file: %s (valid)", ps.file.path)
	}
	if ps.file.reason == "" {
		requested, source := "", ""
		if f := a.profileFlag(); f != nil && f.Changed {
			requested, source = f.Value.String(), "flag"
		} else if v := a.env(a.envPrefix() + "_PROFILE"); v != "" {
			requested, source = v, "env"
		}
		_, defined := ps.file.profiles[requested]
		switch {
		case requested != "" && defined:
			ps.name, ps.source = requested, source
		case requested != "":
			ps.requested = requested
		case ps.file.defaultProfile != "":
			ps.name, ps.source = ps.file.defaultProfile, "config"
		case len(ps.file.names) == 1:
			ps.name, ps.source = ps.file.names[0], "single"
		}
	}
	if ps.name != "" {
		a.debugOnce("active profile: %s (%s)", ps.name, ps.source)
	} else {
		a.debugOnce("active profile: none")
	}
	if st := a.run; st != nil {
		cp := ps
		st.profile = &cp
	}
	return ps
}

// profileFlag is the root's --profile flag (registered when App.Settings is set).
func (a *App) profileFlag() *pflag.Flag {
	if a.Settings == nil || a.Root == nil {
		return nil
	}
	return a.Root.PersistentFlags().Lookup("profile")
}

// settingsError reports an invalid settings file (E_CONFIG).
func (a *App) settingsError(sf settingsFile) *Error {
	return NewError("E_CONFIG", fmt.Sprintf("The settings file %s is invalid: %s.", sf.path, sf.reason), map[string]any{
		"dispatch_state": "not_started",
		"next_action":    fmt.Sprintf("Ask the user to fix %s; `%s context` shows which file is read.", sf.path, a.toolName()),
	})
}

// unknownProfileError reports a requested profile the settings file does not define.
func (a *App) unknownProfileError(ps profileState) *Error {
	name := truncateRunes(ps.requested, 64)
	e := NewError("E_CONFIG", fmt.Sprintf("No profile named `%s` in %s.", name, ps.file.path), map[string]any{
		"dispatch_state": "not_started",
		"next_action":    fmt.Sprintf("Use one of the profiles `%s context` lists, or ask the user to add `%s`.", a.toolName(), name),
	})
	if near := closestWords(ps.requested, ps.file.names); len(near) > 0 {
		e.Details["did_you_mean"] = near
	}
	return e
}

// Setting returns the value of a declared setting and where it came from (CLI-100):
// the setting's Flag when the command has it and it is set ("flag"), then
// <PREFIX>_<Env> ("env"), then the active profile ("profile"), then the default
// ("default"). cmd may be nil (the running command is used). An invalid settings
// file or a requested profile that does not exist is E_CONFIG, whatever the source.
func (a *App) Setting(cmd *cobra.Command, name string) (value, source string, err *Error) {
	s, ok := a.settingNamed(name)
	if !ok {
		return "", "", internalError("App.Setting: %q is not declared in App.Settings.", name)
	}
	ps := a.profileState()
	if ps.file.reason != "" {
		return "", "", a.settingsError(ps.file)
	}
	if ps.requested != "" {
		return "", "", a.unknownProfileError(ps)
	}
	if cmd == nil && a.run != nil {
		cmd = a.run.cmd
	}
	if s.Flag != "" && cmd != nil {
		if f := lookupFlag(cmd, s.Flag); f != nil && f.Changed {
			return f.Value.String(), "flag", nil
		}
	}
	if v := a.env(a.envPrefix() + "_" + s.envName()); v != "" {
		return v, "env", nil
	}
	if ps.name != "" {
		if v, ok := ps.file.profiles[ps.name][s.Name]; ok {
			return v, "profile", nil
		}
	}
	return s.Default, "default", nil
}

// profileContext is context.profile (CLI-101). It never fails: an invalid settings
// file reports no profile and its path; a requested profile that does not exist is
// not active.
func (a *App) profileContext() map[string]any {
	ps := a.profileState()
	var file any
	if ps.file.exists {
		file = ps.file.path
	}
	out := map[string]any{"name": nil, "source": "none", "profiles": []string{}, "file": file}
	if ps.file.reason != "" {
		return out
	}
	out["profiles"] = nonNil(ps.file.names)
	if ps.name != "" {
		out["name"], out["source"] = ps.name, ps.source
	}
	return out
}

// settingsCheck is doctor's settings check (CLI-101).
func (a *App) settingsCheck() Check {
	ps := a.profileState()
	switch {
	case ps.file.reason != "":
		return Check{"settings", "fail", a.settingsError(ps.file).Message,
			fmt.Sprintf("Fix %s, or move it aside to use defaults and environment variables.", ps.file.path)}
	case ps.requested != "":
		e := a.unknownProfileError(ps)
		next, _ := e.Details["next_action"].(string)
		return Check{"settings", "fail", e.Message, next}
	case !ps.file.exists:
		return Check{"settings", "pass", "No settings file; defaults and environment variables apply.", ""}
	case ps.name == "":
		return Check{"settings", "pass", fmt.Sprintf("%s is valid; no profile is active.", ps.file.path), ""}
	}
	return Check{"settings", "pass", fmt.Sprintf("%s is valid; profile `%s` is active (%s).", ps.file.path, ps.name, ps.source), ""}
}

// checkSettings reports declaration problems of App.Settings and commands that define
// --profile while aicli owns it (tree check, CLI-100, CLI-101).
func (a *App) checkSettings() []string {
	if a.Settings == nil {
		return nil
	}
	var problems []string
	seen := map[string]bool{}
	for i, s := range a.Settings {
		if !snakeCase.MatchString(s.Name) {
			problems = append(problems, fmt.Sprintf("App.Settings[%d]: %q is not a snake_case name", i, s.Name))
		}
		if seen[s.Name] {
			problems = append(problems, fmt.Sprintf("App.Settings: %q is declared twice", s.Name))
		}
		seen[s.Name] = true
	}
	walkAll(a.Root, func(c *cobra.Command) {
		if c == a.Root || isCobraInternal(c) {
			return
		}
		if f := c.LocalFlags().Lookup("profile"); f != nil && f != a.profileFlag() {
			problems = append(problems, fmt.Sprintf("%q defines --profile, which aicli reserves for the active profile when App.Settings is set (CLI-101)", cmdPath(c)))
		}
	})
	sort.Strings(problems)
	return problems
}
