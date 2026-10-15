// Command recorder stands in for the CLI under test during agent evals.
//
// The agent runs it under the tool's name: a copy in each trial workspace (sub-agent trials) or one copy
// on PATH (run.py). For every call it:
//   - runs the real binary named in the trial's run-config.json, in an environment built from the
//     recorder's private config, which carries the upstream address and the agent identity's credentials
//     and never appears in the agent's environment or workspace;
//   - points the real binary's home and config directories at a per-trial folder, so trials share no state
//     with each other or with the user's own profiles;
//   - passes stdin, stdout, stderr and the exit code through unchanged;
//   - appends one JSON line per call to <trial>/calls.jsonl and keeps the raw stdout and stderr bytes
//     under <trial>/calls/.
//
// The private config's path is set at build time (go build -ldflags "-X main.secretsPath=<path>");
// harness/build_recorder.py takes it from the eval configuration. harness/seed.py writes the file from
// tool.recorder_config():
//
//	{"tool": "gitlab-cli",
//	 "set_env": {"GITLAB_CLI_HOST": "http://localhost:8929", "GITLAB_CLI_TOKEN": "...", "GITLAB_CLI_NO_UPDATE_CHECK": "1"},
//	 "drop_env": ["GITLAB_TOKEN", "GITLAB_HOST"],
//	 "drop_env_prefixes": ["GITLAB_CLI_"],
//	 "trial_env": ["GITLAB_CLI_HOST"]}
//
// The trial folder is the folder holding this copy of the recorder when it contains run-config.json,
// else EVAL_RUN_DIR (the single shared copy run.py puts on PATH). Its run-config.json names the real
// binary and may override variables of set_env for that trial ({"real_bin": "...", "set_env":
// {"GITLAB_CLI_HOST": "http://127.0.0.1:50123"}}, how a lost-reply trial reaches the fault proxy), but
// only those the private config lists in trial_env; any other is refused, so a file in the agent's
// workspace cannot inject a variable such as the tool's scope-file variable.
package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

var secretsPath = ""

// privateConfig is the environment the recorder gives the real binary.
type privateConfig struct {
	Tool            string            `json:"tool"`
	SetEnv          map[string]string `json:"set_env"`
	DropEnv         []string          `json:"drop_env"`
	DropEnvPrefixes []string          `json:"drop_env_prefixes"`
	TrialEnv        []string          `json:"trial_env"`
}

type runConfig struct {
	RealBin string            `json:"real_bin"`
	SetEnv  map[string]string `json:"set_env"`
}

type record struct {
	ID          string   `json:"id"`
	Start       string   `json:"start"`
	DurationMS  int64    `json:"duration_ms"`
	Argv        []string `json:"argv"`
	Exit        int      `json:"exit"`
	StdoutBytes int      `json:"stdout_bytes"`
	StderrBytes int      `json:"stderr_bytes"`
	StdoutSHA   string   `json:"stdout_sha256"`
	TrialEnv    []string `json:"trial_env,omitempty"` // variables run-config.json overrode (names only)
	Error       string   `json:"error,omitempty"`
}

func main() {
	os.Exit(run())
}

func run() int {
	// The trial folder is the folder this copy of the recorder lives in, so parallel trials (each with its
	// own copy) never share state. EVAL_RUN_DIR is only a fallback for the single shared copy.
	runDir := ""
	if exe, err := os.Executable(); err == nil {
		if _, err := os.Stat(filepath.Join(filepath.Dir(exe), "run-config.json")); err == nil {
			runDir = filepath.Dir(exe)
		}
	}
	if runDir == "" {
		runDir = os.Getenv("EVAL_RUN_DIR")
	}
	if runDir == "" || secretsPath == "" {
		fmt.Fprintf(os.Stderr, "%s: not configured for this workspace\n", commandName())
		return 1
	}
	var priv privateConfig
	if err := readJSON(secretsPath, &priv); err != nil {
		fmt.Fprintln(os.Stderr, "recorder: cannot read its private config:", err)
		return 1
	}
	var cfg runConfig
	if err := readJSON(filepath.Join(runDir, "run-config.json"), &cfg); err != nil {
		fmt.Fprintln(os.Stderr, "recorder: cannot read run config:", err)
		return 1
	}
	if cfg.RealBin == "" {
		fmt.Fprintln(os.Stderr, "recorder: run-config.json names no real_bin")
		return 1
	}
	setEnv, envErr := trialSetEnv(priv, cfg)
	if envErr != nil {
		fmt.Fprintln(os.Stderr, "recorder:", envErr)
		return 1
	}
	priv.SetEnv = setEnv

	home := filepath.Join(runDir, "home")
	for _, d := range []string{home, filepath.Join(home, "AppData", "Roaming"), filepath.Join(home, "AppData", "Local"), filepath.Join(runDir, "calls")} {
		_ = os.MkdirAll(d, 0o700)
	}

	cmd := exec.Command(cfg.RealBin, os.Args[1:]...)
	cmd.Env = childEnv(os.Environ(), priv, home)
	// Pass stdin straight through: the agent's shell may hold an open pipe that never closes, so the
	// recorder must not read it; the real binary reads it only when a command asks for input.
	cmd.Stdin = os.Stdin
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	start := time.Now()
	err := cmd.Run()
	dur := time.Since(start)

	exit := 0
	errText := ""
	if err != nil {
		var ee *exec.ExitError
		if errors.As(err, &ee) {
			exit = ee.ExitCode()
		} else {
			exit = 127
			errText = err.Error()
			fmt.Fprintln(&stderr, "recorder: failed to start real binary:", err)
		}
	}

	_, _ = os.Stdout.Write(stdout.Bytes())
	_, _ = os.Stderr.Write(stderr.Bytes())

	id := fmt.Sprintf("%d-%d", start.UnixNano(), os.Getpid())
	sum := sha256.Sum256(stdout.Bytes())
	_ = os.WriteFile(filepath.Join(runDir, "calls", id+".out"), stdout.Bytes(), 0o600)
	_ = os.WriteFile(filepath.Join(runDir, "calls", id+".err"), stderr.Bytes(), 0o600)
	overridden := make([]string, 0, len(cfg.SetEnv))
	for k := range cfg.SetEnv {
		overridden = append(overridden, k)
	}
	sort.Strings(overridden)
	rec := record{
		ID: id, Start: start.UTC().Format(time.RFC3339Nano), DurationMS: dur.Milliseconds(),
		Argv: os.Args[1:], Exit: exit,
		StdoutBytes: stdout.Len(), StderrBytes: stderr.Len(),
		StdoutSHA: hex.EncodeToString(sum[:]), TrialEnv: overridden, Error: errText,
	}
	if line, err := json.Marshal(rec); err == nil {
		if f, err := os.OpenFile(filepath.Join(runDir, "calls.jsonl"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600); err == nil {
			_, _ = f.Write(append(line, '\n'))
			_ = f.Close()
		}
	}
	return exit
}

// childEnv copies the agent's environment without anything that could carry the tool's credentials or
// redirect its state (the home and XDG directories, drop_env, drop_env_prefixes and every variable it is
// about to set), then sets the per-trial home and the private config's set_env.
func childEnv(environ []string, priv privateConfig, home string) []string {
	drop := map[string]bool{
		"HOME": true, "USERPROFILE": true, "APPDATA": true, "LOCALAPPDATA": true,
		"XDG_CONFIG_HOME": true, "XDG_STATE_HOME": true, "XDG_CACHE_HOME": true, "XDG_DATA_HOME": true,
	}
	for _, k := range priv.DropEnv {
		drop[strings.ToUpper(k)] = true
	}
	for k := range priv.SetEnv {
		drop[strings.ToUpper(k)] = true
	}
	var env []string
	for _, kv := range environ {
		k := kv
		if i := strings.IndexByte(kv, '='); i > 0 {
			k = kv[:i]
		}
		up := strings.ToUpper(k)
		if drop[up] || hasAnyPrefix(up, priv.DropEnvPrefixes) {
			continue
		}
		env = append(env, kv)
	}
	env = append(env,
		"HOME="+home,
		"USERPROFILE="+home,
		"APPDATA="+filepath.Join(home, "AppData", "Roaming"),
		"LOCALAPPDATA="+filepath.Join(home, "AppData", "Local"),
	)
	keys := make([]string, 0, len(priv.SetEnv))
	for k := range priv.SetEnv {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		env = append(env, k+"="+priv.SetEnv[k])
	}
	return env
}

// trialSetEnv is the private config's set_env with the trial's overrides from run-config.json applied.
// A trial may override only the variables listed in trial_env (compared case-insensitively).
func trialSetEnv(priv privateConfig, cfg runConfig) (map[string]string, error) {
	out := make(map[string]string, len(priv.SetEnv)+len(cfg.SetEnv))
	for k, v := range priv.SetEnv {
		out[k] = v
	}
	allowed := map[string]bool{}
	for _, k := range priv.TrialEnv {
		allowed[strings.ToUpper(k)] = true
	}
	keys := make([]string, 0, len(cfg.SetEnv))
	for k := range cfg.SetEnv {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		if !allowed[strings.ToUpper(k)] {
			return nil, fmt.Errorf("run-config.json sets %s, which trial_env in the private config does not allow", k)
		}
		for existing := range out {
			if strings.EqualFold(existing, k) {
				delete(out, existing)
			}
		}
		out[k] = cfg.SetEnv[k]
	}
	return out, nil
}

func hasAnyPrefix(upperKey string, prefixes []string) bool {
	for _, p := range prefixes {
		if p != "" && strings.HasPrefix(upperKey, strings.ToUpper(p)) {
			return true
		}
	}
	return false
}

// commandName is the name the agent called the recorder by, without .exe.
func commandName() string {
	name := filepath.Base(os.Args[0])
	if strings.EqualFold(filepath.Ext(name), ".exe") {
		name = name[:len(name)-len(".exe")]
	}
	return name
}

func readJSON(path string, v any) error {
	b, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return json.Unmarshal(bytes.TrimPrefix(b, []byte("\xef\xbb\xbf")), v)
}
