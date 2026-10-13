// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"bytes"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestKeyIsCreatedOnceWithPrivateMode(t *testing.T) {
	dir := t.TempDir()
	a := &App{Name: "notes", StateDir: filepath.Join(dir, "state")}
	state, e := a.ensureStateDir()
	if e != nil {
		t.Fatal(e)
	}
	k1, e := a.loadKey(state, true)
	if e != nil || len(k1) != keySize {
		t.Fatalf("key: %v %v", k1, e)
	}
	k2, e := a.loadKey(state, false)
	if e != nil || !bytes.Equal(k1, k2) {
		t.Fatal("the key changed between loads")
	}
	if runtime.GOOS != "windows" {
		info, _ := os.Stat(filepath.Join(state, "confirm.key"))
		if info.Mode().Perm() != 0o600 {
			t.Fatalf("key mode %v", info.Mode().Perm())
		}
		sinfo, _ := os.Stat(state)
		if sinfo.Mode().Perm() != 0o700 {
			t.Fatalf("state dir mode %v", sinfo.Mode().Perm())
		}
	}
	entries, _ := os.ReadDir(state)
	for _, e := range entries {
		if strings.Contains(e.Name(), ".tmp") {
			t.Fatalf("temporary key file left behind: %s", e.Name())
		}
	}
}

func TestConcurrentKeyCreationAgreesOnOneKey(t *testing.T) {
	dir := t.TempDir()
	a := &App{Name: "notes", StateDir: filepath.Join(dir, "state")}
	state, e := a.ensureStateDir()
	if e != nil {
		t.Fatal(e)
	}
	const n = 24
	keys := make([][]byte, n)
	errs := make([]*Error, n)
	var wg sync.WaitGroup
	start := make(chan struct{})
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			keys[i], errs[i] = a.loadKey(state, true)
		}(i)
	}
	close(start)
	wg.Wait()
	for i := range keys {
		if errs[i] != nil {
			t.Fatalf("goroutine %d: %v", i, errs[i])
		}
		if !bytes.Equal(keys[i], keys[0]) {
			t.Fatal("two goroutines got different keys")
		}
	}
}

func TestBrokenKeyFailsClosed(t *testing.T) {
	cases := map[string]func(t *testing.T, path string){
		"empty":     func(t *testing.T, p string) { mustWrite(t, p, nil) },
		"31 bytes":  func(t *testing.T, p string) { mustWrite(t, p, make([]byte, 31)) },
		"33 bytes":  func(t *testing.T, p string) { mustWrite(t, p, make([]byte, 33)) },
		"directory": func(t *testing.T, p string) { _ = os.Mkdir(p, 0o700) },
	}
	if runtime.GOOS != "windows" {
		cases["group readable"] = func(t *testing.T, p string) {
			mustWrite(t, p, make([]byte, keySize))
			_ = os.Chmod(p, 0o640)
		}
		if os.Geteuid() != 0 {
			cases["unreadable"] = func(t *testing.T, p string) {
				mustWrite(t, p, make([]byte, keySize))
				_ = os.Chmod(p, 0o000)
			}
		}
	}
	cases["symlink"] = func(t *testing.T, p string) {
		target := filepath.Join(filepath.Dir(p), "real.key")
		mustWrite(t, target, make([]byte, keySize))
		if err := os.Symlink(target, p); err != nil {
			t.Skipf("symlinks unavailable: %v", err)
		}
	}
	for name, setup := range cases {
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			a := &App{Name: "notes", StateDir: filepath.Join(dir, "state")}
			state, e := a.ensureStateDir()
			if e != nil {
				t.Fatal(e)
			}
			setup(t, filepath.Join(state, "confirm.key"))
			for _, create := range []bool{true, false} {
				key, e := a.loadKey(state, create)
				if e == nil || e.Code != "E_IO" || key != nil {
					t.Fatalf("create=%v: want E_IO, got %v %v", create, key, e)
				}
				if na, _ := e.Details["next_action"].(string); na == "" {
					t.Fatal("E_IO without next_action")
				}
			}
		})
	}
}

func mustWrite(t *testing.T, p string, b []byte) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(p), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, b, 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestStateDirMustBeAPrivateDirectory(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "afile")
	mustWrite(t, file, []byte("x"))
	if _, e := (&App{Name: "notes", StateDir: file}).ensureStateDir(); e == nil || e.Code != "E_IO" {
		t.Fatalf("a file as state dir: %v", e)
	}
	real := filepath.Join(dir, "real")
	_ = os.Mkdir(real, 0o700)
	link := filepath.Join(dir, "link")
	if err := os.Symlink(real, link); err == nil {
		if _, e := (&App{Name: "notes", StateDir: link}).ensureStateDir(); e == nil || e.Code != "E_IO" {
			t.Fatalf("a symlinked state dir: %v", e)
		}
	} else {
		t.Logf("symlinks unavailable: %v", err)
	}
	if runtime.GOOS != "windows" {
		open := filepath.Join(dir, "open")
		_ = os.Mkdir(open, 0o755)
		_ = os.Chmod(open, 0o755)
		if _, e := (&App{Name: "notes", StateDir: open}).ensureStateDir(); e == nil || e.Code != "E_IO" {
			t.Fatalf("a world-readable state dir: %v", e)
		}
	}
}

func TestStateDirResolution(t *testing.T) {
	a := &App{Name: "notes", Env: func(k string) string {
		if k == "NOTES_STATE_DIR" {
			return "/tmp/from-env"
		}
		return ""
	}}
	p, e := a.stateDirPath()
	if e != nil || filepath.Base(p) != "from-env" {
		t.Fatalf("env override: %q %v", p, e)
	}
	a.StateDir = "/tmp/explicit"
	p, _ = a.stateDirPath()
	if filepath.Base(p) != "explicit" {
		t.Fatalf("StateDir override: %q", p)
	}
	b := &App{Name: "notes", Env: func(string) string { return "" }}
	p, _ = b.stateDirPath()
	if filepath.Base(p) != ".notes" {
		t.Fatalf("default: %q", p)
	}
}

func TestJournalPathBlockedFailsClosed(t *testing.T) {
	h := newHarness(t)
	if err := os.MkdirAll(h.state, 0o700); err != nil {
		t.Fatal(err)
	}
	mustWrite(t, filepath.Join(h.state, "operations"), []byte("not a dir"))
	for _, args := range [][]string{{"--dry-run"}, {}} {
		r := h.run(append([]string{"note", "update", "1", "--title", "X"}, args...)...)
		if r.exit != 1 || r.code() != "E_IO" || strings.Contains(r.stdout, "ct2_") {
			t.Fatalf("%v: want E_IO, got %s", args, r)
		}
	}
	// a token minted earlier cannot be confirmed through a blocked journal either
	h2 := newHarness(t)
	tok := h2.mustToken("note", "update", "1", "--title", "X")
	ops := filepath.Join(h2.state, "operations")
	if err := os.RemoveAll(ops); err != nil {
		t.Fatal(err)
	}
	mustWrite(t, ops, []byte("not a dir"))
	r := h2.run("note", "update", "1", "--title", "X", "--confirm", tok)
	if r.exit != 1 || r.code() != "E_IO" || h2.up.Writes() != 0 {
		t.Fatalf("confirm through a blocked journal: %s", r)
	}
}

func TestAtomicRecordWritesNeverExposePartialContent(t *testing.T) {
	dir := t.TempDir()
	j := &journal{dir: dir}
	id := "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"
	small := &record{V: 2, OperationID: id, Kind: "single", Command: "x", Status: "planned", Target: &targetJSON{Key: "k"},
		Effects: []string{"change"}, CreatedAt: "2026-01-01T00:00:00Z", ExpiresAt: "2026-01-01T00:10:00Z"}
	big := *small
	big.Preview = json.RawMessage(`{"pad":"` + strings.Repeat("x", 64<<10) + `"}`)
	if e := j.write(small); e != nil {
		t.Fatal(e)
	}
	done := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < 150; i++ {
			r := small
			if i%2 == 0 {
				r = &big
			}
			if e := j.write(r); e != nil {
				t.Errorf("write %d: %v", i, e)
				return
			}
		}
		close(done)
	}()
	reads := 0
	for {
		select {
		case <-done:
			wg.Wait()
			if reads == 0 {
				t.Fatal("no reads happened")
			}
			entries, _ := os.ReadDir(dir)
			for _, e := range entries {
				if strings.Contains(e.Name(), ".tmp") {
					t.Fatalf("temporary file left: %s", e.Name())
				}
			}
			return
		default:
		}
		r, exists, err := j.read(id)
		if !exists || err != nil || r == nil {
			t.Fatalf("read saw a missing or partial record: exists=%v err=%v", exists, err)
		}
		reads++
	}
}

func TestConsumeIsExclusiveAcrossGoroutines(t *testing.T) {
	j := &journal{dir: t.TempDir()}
	const n = 64
	wins := make(chan bool, n)
	var wg sync.WaitGroup
	start := make(chan struct{})
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			ok, e := j.consume("op_aaaaaaaaaaaaaaaaaaaaaaaaaa", time.Now())
			if e != nil {
				t.Error(e)
			}
			wins <- ok
		}()
	}
	close(start)
	wg.Wait()
	close(wins)
	count := 0
	for w := range wins {
		if w {
			count++
		}
	}
	if count != 1 {
		t.Fatalf("%d goroutines consumed the same operation", count)
	}
}

// TestHelperConfirmProcess is the child side of TestConcurrentConfirmsFromProcesses.
func TestHelperConfirmProcess(t *testing.T) {
	if os.Getenv("AICLI_HELPER_CONFIRM") != "1" {
		t.Skip("helper process")
	}
	go_ := os.Getenv("AICLI_HELPER_GO")
	for {
		if _, err := os.Stat(go_); err == nil {
			break
		}
		time.Sleep(time.Millisecond)
	}
	u := newUpstream()
	counter := os.Getenv("AICLI_HELPER_COUNTER")
	u.hook = func(string, string) error {
		f, err := os.OpenFile(counter, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600)
		if err == nil {
			_, _ = f.WriteString("write\n")
			_ = f.Close()
		}
		return nil
	}
	a := testApp(u)
	a.StateDir = os.Getenv("AICLI_HELPER_STATE")
	scope := os.Getenv("AICLI_HELPER_SCOPE")
	a.Env = func(k string) string {
		if k == "NOTES_SCOPE_FILE" {
			return scope
		}
		return ""
	}
	var out bytes.Buffer
	a.Stdout, a.Stderr = &out, &out
	exit := a.Execute([]string{"note", "update", "1", "--title", "Proc", "--confirm", os.Getenv("AICLI_HELPER_TOKEN")})
	_ = os.WriteFile(os.Getenv("AICLI_HELPER_RESULT"), []byte(strconv.Itoa(exit)+"\n"+out.String()), 0o600)
}

func TestConcurrentConfirmsFromProcesses(t *testing.T) {
	if testing.Short() {
		t.Skip("spawns processes")
	}
	h := newHarness(t)
	tok := h.mustToken("note", "update", "1", "--title", "Proc")
	goFile := filepath.Join(h.dir, "go")
	counter := filepath.Join(h.dir, "counter")
	const n = 4
	var cmds []*exec.Cmd
	for i := 0; i < n; i++ {
		c := exec.Command(os.Args[0], "-test.run=^TestHelperConfirmProcess$", "-test.count=1")
		c.Env = append(os.Environ(), "AICLI_HELPER_CONFIRM=1", "AICLI_HELPER_GO="+goFile, "AICLI_HELPER_COUNTER="+counter,
			"AICLI_HELPER_STATE="+h.state, "AICLI_HELPER_SCOPE="+h.scope, "AICLI_HELPER_TOKEN="+tok,
			"AICLI_HELPER_RESULT="+filepath.Join(h.dir, "result"+strconv.Itoa(i)))
		if err := c.Start(); err != nil {
			t.Fatal(err)
		}
		cmds = append(cmds, c)
	}
	time.Sleep(300 * time.Millisecond)
	mustWrite(t, goFile, []byte("go"))
	for _, c := range cmds {
		if err := c.Wait(); err != nil {
			t.Fatalf("helper: %v", err)
		}
	}
	success := 0
	for i := 0; i < n; i++ {
		b, err := os.ReadFile(filepath.Join(h.dir, "result"+strconv.Itoa(i)))
		if err != nil {
			t.Fatal(err)
		}
		lines := strings.SplitN(string(b), "\n", 2)
		switch lines[0] {
		case "0":
			success++
		case "6":
			if !strings.Contains(lines[1], "token_used") {
				t.Fatalf("a losing process did not report token_used: %s", lines[1])
			}
		default:
			t.Fatalf("unexpected exit %s: %s", lines[0], lines[1])
		}
	}
	writes, _ := os.ReadFile(counter)
	if success != 1 || strings.Count(string(writes), "write") != 1 {
		t.Fatalf("want one success and one write across processes, got %d successes and %d writes",
			success, strings.Count(string(writes), "write"))
	}
}

func TestCorruptRecordBlocksEveryTargetUntilResolved(t *testing.T) {
	h := newHarness(t)
	r := h.run("note", "update", "1", "--title", "X", "--dry-run")
	id := r.data()["operation_id"].(string)
	mustWrite(t, filepath.Join(h.state, "operations", id+".json"), []byte("{not json"))
	for _, target := range []string{"1", "2"} {
		blocked := h.run("note", "update", target, "--title", "Y", "--dry-run")
		expectConflict(t, blocked, "unresolved_operation")
	}
	show := h.run("operation", "show", id)
	if show.exit != 0 || show.data()["status"] != "unknown" {
		t.Fatalf("corrupt record view: %s", show)
	}
	// An unreadable record could be any of the tool's writes, so resolving it needs
	// a scope covering every write effect the tool has.
	res := h.run("operation", "resolve", id, "--outcome", "not_applied", "--evidence", "inspected the upstream")
	if res.code() != "E_FORBIDDEN" {
		t.Fatalf("a scope without every write effect must not resolve a corrupt record: %s", res)
	}
	h.grant(map[string]any{"effects": []string{"change", "deliver", "artifact", "persist"}})
	res = h.run("operation", "resolve", id, "--outcome", "not_applied", "--evidence", "inspected the upstream")
	if res.exit != 0 {
		t.Fatalf("resolving a corrupt record: %s", res)
	}
	if ok := h.run("note", "update", "2", "--title", "Y", "--dry-run"); ok.exit != 0 {
		t.Fatalf("still blocked: %s", ok)
	}
	// the token of the corrupt record can no longer be used
	conf := h.run("note", "update", "1", "--title", "X", "--confirm", r.token())
	if conf.exit == 0 {
		t.Fatalf("a token of a corrupt record confirmed: %s", conf)
	}
}

func TestPruneKeepsUnresolvedRecords(t *testing.T) {
	dir := t.TempDir()
	j := &journal{dir: dir}
	now := time.Date(2026, 10, 12, 0, 0, 0, 0, time.UTC)
	old := timeString(now.Add(-40 * 24 * time.Hour))
	mk := func(id, status string, lease *string) {
		r := &record{V: 2, OperationID: id, Kind: "single", Command: "x", Status: status, Target: &targetJSON{Key: "k"},
			Effects: []string{"change"}, CreatedAt: old, ExpiresAt: old, FinishedAt: &old, LeaseUntil: lease}
		if e := j.write(r); e != nil {
			t.Fatal(e)
		}
	}
	mk("op_aaaaaaaaaaaaaaaaaaaaaaaaaa", "applied", nil)
	mk("op_bbbbbbbbbbbbbbbbbbbbbbbbbb", "unknown", nil)
	mk("op_cccccccccccccccccccccccccc", "dispatching", &old)
	mk("op_dddddddddddddddddddddddddd", "planned", nil)
	j.prune(now)
	for id, want := range map[string]bool{"op_aaaaaaaaaaaaaaaaaaaaaaaaaa": false, "op_bbbbbbbbbbbbbbbbbbbbbbbbbb": true,
		"op_cccccccccccccccccccccccccc": true, "op_dddddddddddddddddddddddddd": false} {
		_, exists, _ := j.read(id)
		if exists != want {
			t.Errorf("%s: exists=%v, want %v", id, exists, want)
		}
	}
}

func TestEffectiveStatus(t *testing.T) {
	now := time.Date(2026, 10, 12, 12, 0, 0, 0, time.UTC)
	past, future := timeString(now.Add(-time.Minute)), timeString(now.Add(time.Minute))
	cases := []struct {
		status, exp string
		lease       *string
		want        string
	}{
		{"planned", future, nil, "planned"},
		{"planned", past, nil, "expired"},
		{"dispatching", future, &future, "dispatching"},
		{"dispatching", future, &past, "unknown"},
		{"dispatching", future, nil, "unknown"},
		{"applied", past, nil, "applied"},
		{"resolved", past, nil, "resolved"},
	}
	for _, c := range cases {
		r := &record{Status: c.status, ExpiresAt: c.exp, LeaseUntil: c.lease}
		if got := effectiveStatus(r, now); got != c.want {
			t.Errorf("%s: got %s, want %s", c.status, got, c.want)
		}
	}
}

// TestJunctionsAreRefusedOnWindows covers the link type an unprivileged Windows user
// can create: a directory junction (a reparse point).
func TestJunctionsAreRefusedOnWindows(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("directory junctions are Windows-only")
	}
	junction := func(t *testing.T, link, target string) {
		t.Helper()
		if out, err := exec.Command("cmd", "/c", "mklink", "/J", link, target).CombinedOutput(); err != nil {
			t.Skipf("mklink /J failed: %v %s", err, out)
		}
	}
	dir := t.TempDir()
	real := filepath.Join(dir, "real")
	_ = os.Mkdir(real, 0o700)
	link := filepath.Join(dir, "link")
	junction(t, link, real)
	if _, e := (&App{Name: "notes", StateDir: link}).ensureStateDir(); e == nil || e.Code != "E_IO" {
		t.Fatalf("a junction as the state dir: %v", e)
	}
	h := newHarness(t)
	if err := os.MkdirAll(h.state, 0o700); err != nil {
		t.Fatal(err)
	}
	elsewhere := filepath.Join(h.dir, "elsewhere")
	_ = os.Mkdir(elsewhere, 0o700)
	junction(t, filepath.Join(h.state, "operations"), elsewhere)
	r := h.run("note", "update", "1", "--title", "X", "--dry-run")
	if r.exit != 1 || r.code() != "E_IO" {
		t.Fatalf("a junction as the journal: %s", r)
	}
}
