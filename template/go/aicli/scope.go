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
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"sort"
	"strings"
	"time"
	"unicode/utf8"
)

// Scope files (SEC-010 .. SEC-012): a person writes one to grant this tool effects
// on targets for a while; the CLI only reads it and never creates or edits it.
// Without a valid scope every write is refused before anything is sent.
//
// Protection is honest (SEC-013): a scope stops injected instructions, agent drift
// and wrong targets; it cannot stop an agent running as the same OS user from
// editing the file.

const maxScopeBytes = 64 << 10

type scopeFile struct {
	ScopeID      string            `json:"scope_id"`
	Tool         string            `json:"tool"`
	GrantedBy    string            `json:"granted_by"`
	GrantedAt    string            `json:"granted_at"`
	ExpiresAt    *string           `json:"expires_at"`
	Effects      []string          `json:"effects"`
	Dangerous    bool              `json:"dangerous"`
	Targets      *[]targetSelector `json:"targets"`
	Destinations []string          `json:"destinations"`
	Outputs      []string          `json:"outputs"`
	Roots        []string          `json:"roots"`
	Limits       *scopeLimits      `json:"limits"`
	Revoked      bool              `json:"revoked"`
	Notes        string            `json:"notes"`
}

type targetSelector struct {
	Key   string `json:"key"`
	Match string `json:"match"`
}

type scopeLimits struct {
	MaxOperations *int `json:"max_operations"`
}

// scopeState is the resolved scope: granted, missing, invalid, expired or revoked.
type scopeState struct {
	status string
	path   string
	digest string
	file   *scopeFile
	reason string
}

func (s scopeState) id() *string {
	if s.file != nil && s.status != "invalid" && s.file.ScopeID != "" {
		return strPtr(s.file.ScopeID)
	}
	return nil
}

// scopePath resolves the scope file: <PREFIX>_SCOPE_FILE, else <state>/scope.json.
// There is deliberately no flag (SEC-011).
func (a *App) scopePath(stateDir string) string {
	path := filepath.Join(stateDir, "scope.json")
	if p := strings.TrimSpace(a.env(a.envPrefix() + "_SCOPE_FILE")); p != "" {
		path = p
		if abs, err := filepath.Abs(p); err == nil {
			path = abs
		}
	}
	a.debugOnce("scope file: %s", path)
	return path
}

// loadScope reads and strictly validates the scope file.
func (a *App) loadScope(stateDir string) scopeState {
	path := a.scopePath(stateDir)
	st := scopeState{path: path}
	invalid := func(reason string) scopeState {
		st.status = "invalid"
		st.reason = reason
		return st
	}
	info, err := os.Lstat(path)
	if errors.Is(err, fs.ErrNotExist) {
		st.status = "missing"
		return st
	}
	if err != nil {
		return invalid("it cannot be read")
	}
	mode := info.Mode()
	if mode&fs.ModeSymlink != 0 || mode&fs.ModeIrregular != 0 || !mode.IsRegular() {
		return invalid("it must be a regular file, not a link")
	}
	if info.Size() > maxScopeBytes {
		return invalid("it is larger than 64 KiB")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return invalid("it cannot be read")
	}
	st.digest = "sha256:" + sha256Hex(raw)
	file, reason := parseScope(raw)
	if reason != "" {
		return invalid(reason)
	}
	st.file = file
	if file.Tool != a.toolName() {
		return invalid(fmt.Sprintf("it names the tool %q, not %q", truncateRunes(file.Tool, 40), a.toolName()))
	}
	if file.Revoked {
		st.status = "revoked"
		return st
	}
	if file.ExpiresAt != nil {
		exp, _ := parseTime(*file.ExpiresAt)
		if !a.now().Before(exp) {
			st.status = "expired"
			return st
		}
	}
	st.status = "granted"
	return st
}

// parseScope parses a scope file strictly: one UTF-8 JSON object without a BOM,
// duplicate keys and unknown keys rejected, required keys present and typed.
func parseScope(raw []byte) (*scopeFile, string) {
	if bytes.HasPrefix(raw, []byte("\xef\xbb\xbf")) {
		return nil, "it starts with a byte order mark"
	}
	if !utf8.Valid(raw) {
		return nil, "it is not valid UTF-8"
	}
	top, err := strictKeys(raw)
	if err != nil {
		return nil, err.Error()
	}
	for _, k := range scopeRequiredKeys {
		if !top[k] {
			return nil, fmt.Sprintf("the required key %q is missing", k)
		}
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	var f scopeFile
	if err := dec.Decode(&f); err != nil {
		return nil, scopeDecodeReason(err)
	}
	if _, err := dec.Token(); !errors.Is(err, io.EOF) {
		return nil, "it holds more than one JSON value"
	}
	switch {
	case strings.TrimSpace(f.ScopeID) == "":
		return nil, "scope_id is empty"
	case strings.TrimSpace(f.GrantedBy) == "":
		return nil, "granted_by is empty"
	}
	granted, ok := parseTimeAny(f.GrantedAt)
	if !ok {
		return nil, "granted_at is not an ISO 8601 UTC time"
	}
	if f.ExpiresAt != nil {
		expires, ok := parseTimeAny(*f.ExpiresAt)
		if !ok {
			return nil, "expires_at is not an ISO 8601 UTC time or null"
		}
		if !expires.After(granted) {
			return nil, "expires_at is not after granted_at"
		}
	}
	if f.Effects == nil {
		return nil, "effects must be an array"
	}
	for _, e := range f.Effects {
		if !contains(effectValues, e) {
			return nil, fmt.Sprintf("effects lists an unknown effect %q", truncateRunes(e, 40))
		}
	}
	if top["targets"] && f.Targets == nil {
		return nil, "targets must be an array; leave it out to allow every target"
	}
	if f.Targets != nil {
		for _, t := range *f.Targets {
			if t.Key == "" {
				return nil, "a targets entry has an empty key"
			}
			if t.Match != "" && !contains(targetMatchValues, t.Match) {
				return nil, "a targets entry has an unknown match; use subtree or exact"
			}
		}
	}
	for _, d := range f.Destinations {
		if strings.TrimSpace(d) == "" {
			return nil, "destinations holds an empty entry"
		}
	}
	for _, p := range append(append([]string{}, f.Outputs...), f.Roots...) {
		if !filepath.IsAbs(p) {
			return nil, "outputs and roots must be absolute paths"
		}
	}
	if f.Limits != nil && f.Limits.MaxOperations != nil && *f.Limits.MaxOperations < 1 {
		return nil, "limits.max_operations must be at least 1"
	}
	return &f, ""
}

// scopeDecodeReason says what is wrong with a scope file in the file's own terms,
// not the decoder's: an unknown key (with the closest known one) or a value of the
// wrong type.
func scopeDecodeReason(err error) string {
	if rest, ok := strings.CutPrefix(err.Error(), "json: unknown field "); ok {
		key := strings.Trim(rest, `"`)
		known := append(append([]string{}, scopeRequiredKeys...), scopeOptionalKeys...)
		known = append(known, "key", "match", "max_operations")
		reason := fmt.Sprintf("unknown key `%s`", truncateRunes(key, 40))
		if near := closestWords(key, known); len(near) > 0 {
			reason += fmt.Sprintf(" (did you mean `%s`?)", near[0])
		}
		return reason
	}
	var te *json.UnmarshalTypeError
	if errors.As(err, &te) {
		want := "another type"
		switch te.Type.Kind() {
		case reflect.String:
			want = "a string"
		case reflect.Bool:
			want = "true or false"
		case reflect.Int, reflect.Int64, reflect.Pointer:
			want = "a number"
		case reflect.Slice:
			want = "an array"
		case reflect.Struct:
			want = "an object"
		}
		field := te.Field
		if field == "" {
			field = "a value"
		}
		return fmt.Sprintf("%s must be %s, not %s", field, want, te.Value)
	}
	return "it does not match the scope format"
}

// strictKeys walks a JSON document, rejecting duplicate keys at any depth, and
// returns the keys of the top-level object.
func strictKeys(raw []byte) (map[string]bool, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	tok, err := dec.Token()
	if err != nil {
		return nil, errors.New("it is not JSON")
	}
	if d, ok := tok.(json.Delim); !ok || d != '{' {
		return nil, errors.New("it is not a JSON object")
	}
	top := map[string]bool{}
	if err := walkObject(dec, top, 0); err != nil {
		return nil, err
	}
	return top, nil
}

func walkObject(dec *json.Decoder, keys map[string]bool, depth int) error {
	if depth > 32 {
		return errors.New("it is nested too deeply")
	}
	if keys == nil {
		keys = map[string]bool{}
	}
	for dec.More() {
		tok, err := dec.Token()
		if err != nil {
			return errors.New("it is not valid JSON")
		}
		key, ok := tok.(string)
		if !ok {
			return errors.New("it is not valid JSON")
		}
		if keys[key] {
			return fmt.Errorf("the key %q appears twice", truncateRunes(key, 40))
		}
		keys[key] = true
		if err := walkValue(dec, depth+1); err != nil {
			return err
		}
	}
	if _, err := dec.Token(); err != nil {
		return errors.New("it is not valid JSON")
	}
	return nil
}

func walkValue(dec *json.Decoder, depth int) error {
	tok, err := dec.Token()
	if err != nil {
		return errors.New("it is not valid JSON")
	}
	if d, ok := tok.(json.Delim); ok {
		switch d {
		case '{':
			return walkObject(dec, nil, depth)
		case '[':
			for dec.More() {
				if err := walkValue(dec, depth+1); err != nil {
					return err
				}
			}
			if _, err := dec.Token(); err != nil {
				return errors.New("it is not valid JSON")
			}
		}
	}
	return nil
}

// scopeNeed is what one write needs from the scope.
type scopeNeed struct {
	effects      []string
	dangerous    bool
	targets      []string
	destinations []string
	outputs      []string
	operations   int
}

// subtreeMatch reports whether key lies within selector (SEC-012): equal, or the
// selector followed by one of / ! # : @ (a selector ending in one of them matches
// anything after it).
func subtreeMatch(sel targetSelector, key string) bool {
	if sel.Match == "exact" {
		return key == sel.Key
	}
	if key == sel.Key {
		return true
	}
	if sel.Key == "" || !strings.HasPrefix(key, sel.Key) {
		return false
	}
	boundaries := strings.Join(subtreeBoundaries, "")
	if strings.ContainsRune(boundaries, rune(sel.Key[len(sel.Key)-1])) {
		return true
	}
	return strings.ContainsRune(boundaries, rune(key[len(sel.Key)]))
}

// destinationAllowed checks a delivery destination against the granted ones
// (contract.json scope.destination_match, SEC-012). Destinations have their own
// matcher, never the target subtree rule: a granted URL (with ://) matches by strict
// URL containment (scheme and host exactly, the path by whole segments; user info,
// queries, fragments and dot segments in the destination never match). Any other
// granted destination (an email address, handle or channel) matches exactly, and is
// a prefix only when it ends with "/", at "/" boundaries only: "@", ":", "#" and "!"
// are ordinary characters, so a grant for bob@example.com never covers
// bob@example.com@evil.example.
func destinationAllowed(dest string, granted []string) bool {
	for _, g := range granted {
		if dest == g {
			return true
		}
		if strings.Contains(g, "://") || strings.Contains(dest, "://") {
			if urlWithin(dest, g) {
				return true
			}
			continue
		}
		if strings.HasSuffix(g, "/") && strings.HasPrefix(dest, g) && cleanSegments(dest[len(g):]) {
			return true
		}
	}
	return false
}

// cleanSegments reports whether rest (what follows a granted "dir/" prefix) is one
// or more plain "/"-separated segments: no empty, "." or ".." segment, no backslash
// and no control character.
func cleanSegments(rest string) bool {
	if rest == "" || strings.ContainsAny(rest, "\\") || strings.ContainsFunc(rest, func(r rune) bool { return r < 0x20 || r == 0x7f }) {
		return false
	}
	for _, seg := range strings.Split(rest, "/") {
		if seg == "" || seg == "." || seg == ".." {
			return false
		}
	}
	return true
}

func urlWithin(dest, granted string) bool {
	d, err1 := url.Parse(dest)
	g, err2 := url.Parse(granted)
	if err1 != nil || err2 != nil || d.Scheme == "" || d.Host == "" || g.Host == "" {
		return false
	}
	if d.User != nil || d.RawQuery != "" || d.Fragment != "" || strings.Contains(dest, "\\") {
		return false
	}
	if !strings.EqualFold(d.Scheme, g.Scheme) || !strings.EqualFold(d.Host, g.Host) {
		return false
	}
	ds, ok := urlSegments(d.EscapedPath())
	if !ok {
		return false
	}
	gs, ok := urlSegments(g.EscapedPath())
	if !ok {
		return false
	}
	if len(gs) > len(ds) {
		return false
	}
	for i := range gs {
		if gs[i] != ds[i] {
			return false
		}
	}
	return true
}

// urlSegments splits an escaped URL path into decoded segments, refusing dot
// segments and encoded separators (decoded once).
func urlSegments(escaped string) ([]string, bool) {
	trimmed := strings.Trim(escaped, "/")
	if trimmed == "" {
		return nil, true
	}
	var out []string
	for _, raw := range strings.Split(trimmed, "/") {
		seg, err := url.PathUnescape(raw)
		if err != nil || seg == "." || seg == ".." || strings.ContainsAny(seg, "/\\") {
			return nil, false
		}
		out = append(out, seg)
	}
	return out, true
}

// pathWithin reports whether a local path lies inside one of roots after resolving
// symbolic links (SEC-012).
func pathWithin(p string, roots []string) bool {
	abs, err := filepath.Abs(p)
	if err != nil {
		return false
	}
	target := resolveExisting(abs)
	for _, r := range roots {
		if !filepath.IsAbs(r) {
			continue
		}
		root := resolveExisting(filepath.Clean(r))
		rel, err := filepath.Rel(foldCase(root), foldCase(target))
		if err != nil {
			continue
		}
		if rel == "." || (!strings.HasPrefix(rel, ".."+string(filepath.Separator)) && rel != ".." && !filepath.IsAbs(rel)) {
			return true
		}
	}
	return false
}

// resolveExisting resolves links in the longest existing prefix of p.
func resolveExisting(p string) string {
	p = filepath.Clean(p)
	rest := ""
	cur := p
	for {
		if resolved, err := filepath.EvalSymlinks(cur); err == nil {
			if rest == "" {
				return resolved
			}
			return filepath.Join(resolved, rest)
		}
		parent := filepath.Dir(cur)
		if parent == cur {
			return p
		}
		base := filepath.Base(cur)
		if rest == "" {
			rest = base
		} else {
			rest = filepath.Join(base, rest)
		}
		cur = parent
	}
}

func foldCase(p string) string {
	if runtime.GOOS == "windows" || runtime.GOOS == "darwin" {
		return strings.ToLower(p)
	}
	return p
}

// checkScope refuses a write the scope does not cover (E_FORBIDDEN with
// details.scope), counting the operation budget from the journal.
func (a *App) checkScope(sc scopeState, need scopeNeed, j *journal) *Error {
	missing := map[string]any{}
	status := sc.status
	used := 0
	switch sc.status {
	case "granted":
		var effs []string
		for _, e := range need.effects {
			if e != string(Observe) && !contains(sc.file.Effects, e) {
				effs = append(effs, e)
			}
		}
		if len(effs) > 0 {
			missing["effects"] = effs
		}
		if need.dangerous && !sc.file.Dangerous {
			missing["dangerous"] = true
		}
		if sc.file.Targets != nil {
			for _, t := range need.targets {
				ok := false
				for _, sel := range *sc.file.Targets {
					if subtreeMatch(sel, t) {
						ok = true
						break
					}
				}
				if !ok {
					missing["target"] = t
					break
				}
			}
		}
		var dests []string
		for _, d := range need.destinations {
			if !destinationAllowed(d, sc.file.Destinations) {
				dests = append(dests, d)
			}
		}
		if len(dests) > 0 {
			missing["destinations"] = dests
		}
		var outs []string
		roots := append(append([]string{}, sc.file.Outputs...), sc.file.Roots...)
		for _, p := range need.outputs {
			if !pathWithin(p, roots) {
				outs = append(outs, p)
			}
		}
		if len(outs) > 0 {
			missing["outputs"] = outs
		}
		if len(missing) > 0 {
			status = "insufficient"
			break
		}
		if sc.file.Limits != nil && sc.file.Limits.MaxOperations != nil && need.operations > 0 && j != nil {
			n, e := j.countDispatched(sc.file.ScopeID)
			if e != nil {
				return e
			}
			if n+need.operations > *sc.file.Limits.MaxOperations {
				status, used = "budget_spent", n
				break
			}
		}
		return nil
	default:
		if effs := writeOnly(need.effects); len(effs) > 0 {
			missing["effects"] = effs
		}
		if need.dangerous {
			missing["dangerous"] = true
		}
		if len(need.targets) > 0 {
			missing["target"] = need.targets[0]
		}
		if len(need.destinations) > 0 {
			missing["destinations"] = need.destinations
		}
		if len(need.outputs) > 0 {
			missing["outputs"] = need.outputs
		}
	}
	return a.scopeRefusal(sc, status, need, missing, used)
}

func writeOnly(effects []string) []string {
	var out []string
	for _, e := range effects {
		if e != string(Observe) {
			out = append(out, e)
		}
	}
	return out
}

// scopeRefusal builds E_FORBIDDEN with details.scope (BLUEPRINT 4.5). The example
// grant is the whole file after the change: for a readable scope (insufficient,
// expired, revoked, budget spent) it is the current file with only what is missing
// added, so saving it never takes back what was granted; for a missing or invalid
// file it is a new grant for this write.
func (a *App) scopeRefusal(sc scopeState, status string, need scopeNeed, missing map[string]any, used int) *Error {
	if _, ok := missing["dangerous"]; !ok {
		missing["dangerous"] = false
	}
	if _, ok := missing["effects"]; !ok {
		missing["effects"] = []string{}
	}
	var example map[string]any
	if sc.file != nil && sc.status != "invalid" && sc.status != "missing" {
		example = a.extendedGrant(sc, status, need, used)
	} else {
		example = a.newGrant(need)
	}
	details := map[string]any{"status": status, "path": sc.path, "scope_id": nil, "missing": missing, "grant_example": example}
	if id := sc.id(); id != nil {
		details["scope_id"] = *id
	}
	var message, next string
	switch status {
	case "missing":
		message = "No scope grants this write, so nothing was sent."
		next = "Ask the user to grant this scope by creating %s with the content of details.scope.grant_example, adjusted to what they allow; do not create or edit it yourself."
	case "invalid":
		message = "The scope file is invalid (" + sc.reason + "), so nothing was sent."
		next = "Ask the user to fix %s; details.scope.grant_example shows a valid grant for this write. Do not edit it yourself."
	case "expired":
		message = "The scope has expired, so nothing was sent."
		next = "Ask the user to renew the scope in %s (details.scope.grant_example is the file with a new expires_at); do not edit it yourself."
	case "revoked":
		message = "The scope was revoked, so nothing was sent."
		next = "The user revoked the scope in %s: ask them whether to grant it again (details.scope.grant_example shows it granted); do not edit it yourself."
	case "budget_spent":
		message = "The scope's operation budget is spent, so nothing was sent."
		next = "Ask the user to raise limits.max_operations in %s (details.scope.grant_example shows the file after the change); do not edit it yourself."
	default:
		message = "The scope does not cover this write (details.scope.missing), so nothing was sent."
		next = "Ask the user to add what details.scope.missing lists to %s (details.scope.grant_example shows the whole file after the change); do not edit it yourself."
	}
	return Forbidden(message, map[string]any{
		"scope":          details,
		"dispatch_state": "not_started",
		"next_action":    fmt.Sprintf(next, sc.path),
	})
}

// maxExampleTargets caps the targets an example grant adds.
const maxExampleTargets = 5

// newGrant is an example scope file for this write alone.
func (a *App) newGrant(need scopeNeed) map[string]any {
	now := a.now()
	effects := nonNil(writeOnly(need.effects))
	sort.Strings(effects)
	example := map[string]any{
		"scope_id":   fmt.Sprintf("scp_%s_%s", now.Format("2006-01-02"), a.toolName()),
		"tool":       a.toolName(),
		"granted_by": "<your name>",
		"granted_at": timeString(now),
		"expires_at": timeString(now.Add(7 * 24 * time.Hour)),
		"effects":    effects,
		"dangerous":  need.dangerous,
	}
	var sel []any
	for _, t := range need.targets {
		if len(sel) == maxExampleTargets {
			break
		}
		sel = append(sel, map[string]any{"key": t})
	}
	if len(sel) > 0 {
		example["targets"] = sel
	}
	if len(need.destinations) > 0 {
		example["destinations"] = need.destinations
	}
	if dirs := outputDirs(need.outputs); len(dirs) > 0 {
		example["outputs"] = dirs
	}
	return example
}

// extendedGrant is the current scope file with what this write needs added: the
// union of effects, targets, destinations and outputs, dangerous when needed, a new
// expires_at only when the scope expired, a raised operation budget when it is
// spent, and the same scope_id.
func (a *App) extendedGrant(sc scopeState, status string, need scopeNeed, used int) map[string]any {
	f := sc.file
	effects := append([]string{}, f.Effects...)
	for _, e := range writeOnly(need.effects) {
		if !contains(effects, e) {
			effects = append(effects, e)
		}
	}
	sort.Strings(effects)
	example := map[string]any{
		"scope_id":   f.ScopeID,
		"tool":       f.Tool,
		"granted_by": f.GrantedBy,
		"granted_at": f.GrantedAt,
		"expires_at": nil,
		"effects":    effects,
		"dangerous":  f.Dangerous || need.dangerous,
	}
	if f.ExpiresAt != nil {
		example["expires_at"] = *f.ExpiresAt
	}
	if status == "expired" {
		example["expires_at"] = timeString(a.now().Add(7 * 24 * time.Hour))
	}
	if f.Targets != nil {
		sel := make([]any, 0, len(*f.Targets))
		for _, t := range *f.Targets {
			o := map[string]any{"key": t.Key}
			if t.Match != "" {
				o["match"] = t.Match
			}
			sel = append(sel, o)
		}
		added := 0
		for _, t := range need.targets {
			covered := false
			for _, s := range *f.Targets {
				if subtreeMatch(s, t) {
					covered = true
					break
				}
			}
			if !covered && added < maxExampleTargets {
				sel = append(sel, map[string]any{"key": t})
				added++
			}
		}
		example["targets"] = sel
	}
	dests := append([]string{}, f.Destinations...)
	for _, d := range need.destinations {
		if !destinationAllowed(d, f.Destinations) && !contains(dests, d) {
			dests = append(dests, d)
		}
	}
	if len(dests) > 0 {
		example["destinations"] = dests
	}
	outs := append([]string{}, f.Outputs...)
	roots := append(append([]string{}, f.Outputs...), f.Roots...)
	var uncovered []string
	for _, p := range need.outputs {
		if !pathWithin(p, roots) {
			uncovered = append(uncovered, p)
		}
	}
	for _, d := range outputDirs(uncovered) {
		if !contains(outs, d) {
			outs = append(outs, d)
		}
	}
	if len(outs) > 0 {
		example["outputs"] = outs
	}
	if len(f.Roots) > 0 {
		example["roots"] = f.Roots
	}
	if f.Limits != nil {
		limits := map[string]any{}
		if f.Limits.MaxOperations != nil {
			limits["max_operations"] = *f.Limits.MaxOperations
			if status == "budget_spent" {
				limits["max_operations"] = max(*f.Limits.MaxOperations, used+need.operations)
			}
		}
		example["limits"] = limits
	}
	if f.Notes != "" {
		example["notes"] = f.Notes
	}
	return example
}

// outputDirs is the directories of the given output paths, absolute and unique.
func outputDirs(paths []string) []string {
	var dirs []string
	for _, p := range paths {
		abs, err := filepath.Abs(p)
		if err != nil {
			continue
		}
		if d := filepath.Dir(abs); !contains(dirs, d) {
			dirs = append(dirs, d)
		}
	}
	return dirs
}

// authorizationSummary is context.authorization (CLI-035).
func (a *App) authorizationSummary(sc scopeState) map[string]any {
	out := map[string]any{"status": sc.status, "scope_id": nil, "path": sc.path, "effects": []string{}, "dangerous": false,
		"expires_at": nil}
	if sc.status == "invalid" {
		return out
	}
	if sc.file != nil {
		out["scope_id"] = sc.file.ScopeID
		out["effects"] = nonNil(sc.file.Effects)
		out["dangerous"] = sc.file.Dangerous
		if sc.file.ExpiresAt != nil {
			out["expires_at"] = *sc.file.ExpiresAt
		}
	}
	return out
}
