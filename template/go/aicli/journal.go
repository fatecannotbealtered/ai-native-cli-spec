// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"time"
)

// The operation journal (CLI-058, CLI-060): one record per operation at
// <state>/operations/<id>.json, replaced atomically (temp file + fsync + rename);
// single use is the exclusive creation of <id>.consumed. A record is written before
// anything is sent; if it cannot be written, nothing is sent.

// record is an operation record as stored.
type record struct {
	V            int             `json:"v"`
	OperationID  string          `json:"operation_id"`
	Kind         string          `json:"kind"` // single, batch or item
	BatchID      *string         `json:"batch_id"`
	Command      string          `json:"command"`
	Status       string          `json:"status"`
	Target       *targetJSON     `json:"target"`
	Effects      []string        `json:"effects"`
	Dangerous    bool            `json:"dangerous"`
	ScopeID      *string         `json:"scope_id"`
	CreatedAt    string          `json:"created_at"`
	ExpiresAt    string          `json:"expires_at"`
	DispatchedAt *string         `json:"dispatched_at"`
	LeaseUntil   *string         `json:"lease_until"`
	FinishedAt   *string         `json:"finished_at"`
	Claims       *claims         `json:"claims"`
	Preview      json.RawMessage `json:"preview"`
	Receipt      json.RawMessage `json:"receipt"`
	Error        *recordError    `json:"error"`
	Resolution   *resolution     `json:"resolution"`
	Items        []string        `json:"items"`
	ReplayOf     *string         `json:"replay_of,omitempty"`
	LateReceipt  json.RawMessage `json:"late_receipt,omitempty"`
	// Result is the confirmed write's data.result (at most maxStoredResult bytes),
	// so an idempotent replay can return the original ref and url (CLI-059, CLI-062).
	Result json.RawMessage `json:"result,omitempty"`
}

// maxStoredResult caps the result kept in a record.
const maxStoredResult = 4096

type targetJSON struct {
	Key   string `json:"key"`
	Kind  string `json:"kind,omitempty"`
	Label string `json:"label,omitempty"`
}

type recordError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

type resolution struct {
	Outcome    string `json:"outcome"`
	Evidence   string `json:"evidence"`
	ResolvedAt string `json:"resolved_at"`
}

func strPtr(s string) *string { return &s }

func timeString(t time.Time) string { return t.UTC().Format(time.RFC3339) }

func parseTime(s string) (time.Time, bool) {
	t, err := time.Parse(time.RFC3339, s)
	return t, err == nil
}

// effectiveStatus applies expiry and lease rules to a stored status (BLUEPRINT 5.3).
func effectiveStatus(r *record, now time.Time) string {
	switch r.Status {
	case "planned":
		if exp, ok := parseTime(r.ExpiresAt); !ok || now.After(exp) {
			return "expired"
		}
	case "dispatching":
		if r.LeaseUntil == nil {
			return "unknown"
		}
		if lease, ok := parseTime(*r.LeaseUntil); !ok || now.After(lease) {
			return "unknown"
		}
	}
	return r.Status
}

func unresolved(status string) bool { return contains(unresolvedStatusValues, status) }

// journal is the operations directory of one state dir.
type journal struct {
	dir string
}

// openJournal returns the journal of stateDir. With create it makes the operations
// directory (0700). A path that is a link or not a directory is E_IO (fail closed).
func (a *App) openJournal(stateDir string, create bool) (*journal, *Error) {
	dir := filepath.Join(stateDir, "operations")
	info, err := os.Lstat(dir)
	if errors.Is(err, fs.ErrNotExist) && create {
		if mkErr := os.Mkdir(dir, 0o700); mkErr != nil && !errors.Is(mkErr, fs.ErrExist) {
			return nil, ioError("The operation journal cannot be created; nothing was sent.", fmt.Sprintf("Check that %s is writable.", stateDir), mkErr)
		}
		info, err = os.Lstat(dir)
	}
	if errors.Is(err, fs.ErrNotExist) {
		return &journal{dir: dir}, nil
	}
	if err != nil {
		return nil, ioError("The operation journal cannot be read; nothing was sent.", fmt.Sprintf("Check %s.", dir), err)
	}
	if info.Mode()&fs.ModeSymlink != 0 || info.Mode()&fs.ModeIrregular != 0 || !info.IsDir() {
		return nil, ioError("The operation journal path is not a directory; the tool refuses writes.",
			fmt.Sprintf("Ask the user to inspect %s and move it aside; the tool recreates the journal.", dir), nil)
	}
	return &journal{dir: dir}, nil
}

func (j *journal) recordPath(id string) string { return filepath.Join(j.dir, id+".json") }

// modTime is the last write of a record file (the zero time's epoch when unknown).
func (j *journal) modTime(id string) time.Time {
	if info, err := os.Stat(j.recordPath(id)); err == nil {
		return info.ModTime().UTC()
	}
	return time.Unix(0, 0).UTC()
}

// write stores a record atomically: temp file, fsync, rename over the old version.
func (j *journal) write(r *record) *Error {
	if !validOperationID(r.OperationID) {
		return internalError("Refusing to write a record with an invalid operation id.")
	}
	b, err := json.Marshal(r)
	if err != nil {
		return internalError("The operation record cannot be encoded.")
	}
	if err := writeFileAtomic(j.dir, r.OperationID+".json", append(b, '\n')); err != nil {
		return ioError("The operation record cannot be written; nothing was sent.",
			fmt.Sprintf("Check that %s is writable.", j.dir), err)
	}
	return nil
}

// writeFileAtomic replaces dir/name with data via a synced temporary file.
func writeFileAtomic(dir, name string, data []byte) error {
	tmp, err := os.CreateTemp(dir, name+".tmp-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	done := false
	defer func() {
		if !done {
			_ = os.Remove(tmpName)
		}
	}()
	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if runtime.GOOS != "windows" {
		if err := os.Chmod(tmpName, 0o600); err != nil {
			return err
		}
	}
	if err := renameRetry(tmpName, filepath.Join(dir, name)); err != nil {
		return err
	}
	done = true
	syncDir(dir)
	return nil
}

// renameRetry renames, retrying briefly on Windows where a concurrent reader can
// hold the target open for a moment.
func renameRetry(from, to string) error {
	var err error
	for attempt := 0; attempt < 50; attempt++ {
		if err = os.Rename(from, to); err == nil || runtime.GOOS != "windows" {
			return err
		}
		time.Sleep(time.Duration(2+attempt) * time.Millisecond)
	}
	return err
}

// readFileRetry reads a file, retrying briefly on Windows while a writer replaces it.
func readFileRetry(path string) ([]byte, error) {
	var b []byte
	var err error
	for attempt := 0; attempt < 50; attempt++ {
		b, err = os.ReadFile(path)
		if err == nil || errors.Is(err, fs.ErrNotExist) || runtime.GOOS != "windows" {
			return b, err
		}
		time.Sleep(time.Duration(2+attempt) * time.Millisecond)
	}
	return b, err
}

// errCorruptRecord marks a record file that cannot be trusted.
var errCorruptRecord = errors.New("corrupt operation record")

// read loads a record. exists is false when there is no record; err is
// errCorruptRecord when the file exists but cannot be trusted.
func (j *journal) read(id string) (*record, bool, error) {
	if !validOperationID(id) {
		return nil, false, nil
	}
	b, err := readFileRetry(j.recordPath(id))
	if errors.Is(err, fs.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, true, err
	}
	var r record
	if err := json.Unmarshal(b, &r); err != nil {
		return nil, true, errCorruptRecord
	}
	if r.V != 2 || r.OperationID != id || !contains([]string{"single", "batch", "item"}, r.Kind) ||
		!contains(operationStatusValues, r.Status) || r.Status == "expired" {
		return nil, true, errCorruptRecord
	}
	if r.Kind != "batch" && r.Target == nil {
		return nil, true, errCorruptRecord
	}
	return &r, true, nil
}

// consume creates the single-use marker for id. It returns false when the marker
// already exists (the token was used); the exclusive create is atomic across processes.
func (j *journal) consume(id string, at time.Time) (bool, *Error) {
	path := filepath.Join(j.dir, id+".consumed")
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if errors.Is(err, fs.ErrExist) {
		return false, nil
	}
	if err != nil {
		return false, ioError("The single-use marker cannot be written; nothing was sent.", fmt.Sprintf("Check that %s is writable.", j.dir), err)
	}
	_, _ = fmt.Fprintf(f, "{\"operation_id\":%q,\"consumed_at\":%q}\n", id, timeString(at))
	_ = f.Sync()
	_ = f.Close()
	syncDir(j.dir)
	return true, nil
}

// isConsumed reports whether the single-use marker exists.
func (j *journal) isConsumed(id string) (bool, *Error) {
	_, err := os.Lstat(filepath.Join(j.dir, id+".consumed"))
	if err == nil {
		return true, nil
	}
	if errors.Is(err, fs.ErrNotExist) {
		return false, nil
	}
	return true, ioError("The single-use marker cannot be read; nothing was sent.", fmt.Sprintf("Check %s.", j.dir), err)
}

// lock serialises read-modify-write of one record between the dispatching process
// and operation resolve. Stale locks (older than 30 s) are broken.
func (j *journal) lock(id string) (func(), *Error) {
	path := filepath.Join(j.dir, id+".lock")
	deadline := time.Now().Add(3 * time.Second)
	for {
		f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
		if err == nil {
			_ = f.Close()
			return func() { _ = os.Remove(path) }, nil
		}
		if !errors.Is(err, fs.ErrExist) {
			return nil, ioError("The operation record cannot be locked.", fmt.Sprintf("Check that %s is writable.", j.dir), err)
		}
		if info, statErr := os.Stat(path); statErr == nil && time.Since(info.ModTime()) > 30*time.Second {
			_ = os.Remove(path)
			continue
		}
		if time.Now().After(deadline) {
			return nil, ioError("The operation record is locked by another process.", "Run the command again in a moment.", nil)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// listed is one journal entry: a record, or a corrupt file.
type listed struct {
	id      string
	rec     *record
	corrupt bool
	modTime time.Time
}

// list reads every record in the journal. A missing journal is empty.
func (j *journal) list() ([]listed, *Error) {
	entries, err := os.ReadDir(j.dir)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, ioError("The operation journal cannot be listed; nothing was sent.", fmt.Sprintf("Check %s.", j.dir), err)
	}
	var out []listed
	for _, ent := range entries {
		name := ent.Name()
		if !strings.HasSuffix(name, ".json") {
			continue
		}
		id := strings.TrimSuffix(name, ".json")
		if !validOperationID(id) {
			continue
		}
		var mod time.Time
		if info, err := ent.Info(); err == nil {
			mod = info.ModTime()
		}
		r, exists, err := j.read(id)
		switch {
		case !exists:
			continue
		case err != nil:
			out = append(out, listed{id: id, corrupt: true, modTime: mod})
		default:
			out = append(out, listed{id: id, rec: r, modTime: mod})
		}
	}
	return out, nil
}

// blocking finds an operation whose outcome is unresolved on any of targets, or a
// corrupt record (whose target is unknown, so it blocks every target).
func (j *journal) blocking(targets []string, exclude map[string]bool, now time.Time) (string, *Error) {
	all, e := j.list()
	if e != nil {
		return "", e
	}
	want := toSet(targets)
	sort.Slice(all, func(i, k int) bool { return all[i].id < all[k].id })
	for _, l := range all {
		if exclude[l.id] {
			continue
		}
		if l.corrupt {
			return l.id, nil
		}
		if l.rec.Kind == "batch" || l.rec.Resolution != nil || l.rec.Target == nil {
			continue
		}
		if unresolved(effectiveStatus(l.rec, now)) && want[l.rec.Target.Key] {
			return l.id, nil
		}
	}
	return "", nil
}

// countDispatched counts operations confirmed under a scope (for limits.max_operations).
func (j *journal) countDispatched(scopeID string) (int, *Error) {
	all, e := j.list()
	if e != nil {
		return 0, e
	}
	n := 0
	for _, l := range all {
		if l.rec == nil || l.rec.Kind == "batch" || l.rec.ScopeID == nil || *l.rec.ScopeID != scopeID {
			continue
		}
		if l.rec.DispatchedAt != nil {
			n++
		}
	}
	return n, nil
}

// findIdempotent returns the latest dispatched record of cmd that used idem.
func (j *journal) findIdempotent(cmd, idem, exclude string) (*record, *Error) {
	all, e := j.list()
	if e != nil {
		return nil, e
	}
	var best *record
	for _, l := range all {
		r := l.rec
		if r == nil || r.OperationID == exclude || r.Claims == nil || r.Claims.Idem != idem || r.Command != cmd {
			continue
		}
		if r.Status == "planned" {
			continue
		}
		if best == nil || r.CreatedAt > best.CreatedAt {
			best = r
		}
	}
	return best, nil
}

// prune removes settled records older than the retention window and keeps at most
// maxRecords, oldest settled first. Unresolved and corrupt records are never removed.
func (j *journal) prune(now time.Time) {
	all, e := j.list()
	if e != nil {
		return
	}
	horizon := now.Add(-time.Duration(retentionDays) * 24 * time.Hour)
	type entry struct {
		id      string
		created string
	}
	var settled []entry
	kept := 0
	for _, l := range all {
		if l.corrupt {
			kept++
			continue
		}
		st := effectiveStatus(l.rec, now)
		if unresolved(st) || st == "planned" {
			kept++
			continue
		}
		ref := l.rec.CreatedAt
		if l.rec.FinishedAt != nil {
			ref = *l.rec.FinishedAt
		}
		if t, ok := parseTime(ref); ok && t.Before(horizon) {
			j.remove(l.id)
			continue
		}
		settled = append(settled, entry{l.id, l.rec.CreatedAt})
	}
	if kept+len(settled) <= maxRecords {
		return
	}
	sort.Slice(settled, func(i, k int) bool { return settled[i].created < settled[k].created })
	excess := kept + len(settled) - maxRecords
	for i := 0; i < excess && i < len(settled); i++ {
		j.remove(settled[i].id)
	}
}

func (j *journal) remove(id string) {
	_ = os.Remove(j.recordPath(id))
	_ = os.Remove(filepath.Join(j.dir, id+".consumed"))
}
