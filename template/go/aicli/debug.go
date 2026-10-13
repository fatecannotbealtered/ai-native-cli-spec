// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"fmt"
	"sort"
	"strings"

	"github.com/spf13/pflag"
)

// Diagnostics (CLI-102): <PREFIX>_DEBUG turns on "debug: <message>" lines on
// stderr. They never go to stdout and never change the output or the exit code;
// there is no --verbose or --debug flag. The library itself logs only the state
// directory, the scope file, the settings file and active profile, each stage of
// update and the release tags it skips.

// debugOn reports whether <PREFIX>_DEBUG is set to anything but "" or "0".
func (a *App) debugOn() bool {
	v := strings.TrimSpace(a.env(a.envPrefix() + "_DEBUG"))
	return v != "" && v != "0"
}

// Debugf writes "debug: <message>" to stderr when <PREFIX>_DEBUG is set to anything
// but "" or "0". The values of the running command's secret parameters (Meta.Secrets)
// and of Secret settings are replaced by *** in the message.
func (a *App) Debugf(format string, args ...any) {
	if !a.debugOn() {
		return
	}
	msg := fmt.Sprintf(format, args...)
	for _, s := range a.debugSecrets() {
		msg = strings.ReplaceAll(msg, s, "***")
	}
	_, _ = fmt.Fprintf(a.stderr(), "debug: %s\n", msg)
}

// debugOnce is Debugf for the library's own lines: each distinct line once per App.
func (a *App) debugOnce(format string, args ...any) {
	if !a.debugOn() {
		return
	}
	line := fmt.Sprintf(format, args...)
	if a.debugSeen == nil {
		a.debugSeen = map[string]bool{}
	}
	if a.debugSeen[line] {
		return
	}
	a.debugSeen[line] = true
	a.Debugf("%s", line)
}

// debugSecrets lists the secret values a debug line must not show, longest first:
// the running command's secret flags (and secret file inputs) and the Secret
// settings' values from their flag, the environment or their default.
func (a *App) debugSecrets() []string {
	set := map[string]bool{}
	addValue := func(f *pflag.Flag) {
		if f == nil || !f.Changed {
			return
		}
		if sv, ok := f.Value.(pflag.SliceValue); ok {
			for _, v := range sv.GetSlice() {
				set[v] = true
			}
		}
		set[f.Value.String()] = true
	}
	var cmdFlags func(string) *pflag.Flag
	if st := a.run; st != nil && st.cmd != nil {
		cmd := st.cmd
		cmdFlags = func(name string) *pflag.Flag { return lookupFlag(cmd, name) }
		if m := a.metaFor(cmd); m != nil {
			for _, name := range m.Secrets {
				addValue(lookupFlag(cmd, name))
				for file, bound := range m.FileInputs {
					if bound != name && file != name {
						continue
					}
					if fi := st.files[file]; fi != nil {
						set[string(fi.content)] = true
						set[strings.TrimSpace(string(fi.content))] = true
					}
				}
			}
		}
	}
	for _, s := range a.Settings {
		if !s.Secret {
			continue
		}
		if s.Flag != "" && cmdFlags != nil {
			addValue(cmdFlags(s.Flag))
		}
		set[a.env(a.envPrefix()+"_"+s.envName())] = true
		set[s.Default] = true
	}
	delete(set, "")
	out := make([]string, 0, len(set))
	for v := range set {
		out = append(out, v)
	}
	sort.Slice(out, func(i, j int) bool {
		if len(out[i]) != len(out[j]) {
			return len(out[i]) > len(out[j])
		}
		return out[i] < out[j]
	})
	return out
}
