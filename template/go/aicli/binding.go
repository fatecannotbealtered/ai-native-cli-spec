// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/spf13/cobra"
	"github.com/spf13/pflag"
)

// maxFileInputBytes caps one file or stdin input.
const maxFileInputBytes = 64 << 20

// fileInput is the content of a file flag, read once per run so the bytes the tool
// sends are the bytes the token binds.
type fileInput struct {
	path    string
	content []byte
}

// ReadFileInput returns the content of a file-input flag declared in Meta.FileInputs
// ("-" reads stdin). The file is read once per run; the write gate binds the digest
// of exactly these bytes. It returns nil when the flag was not given. Always read
// file inputs through this method, never with os.ReadFile, or the token would bind
// bytes that differ from the ones sent.
func (a *App) ReadFileInput(cmd *cobra.Command, flag string) ([]byte, error) {
	st, perr := a.runFor(cmd)
	if perr != nil {
		return nil, perr
	}
	m := a.metaFor(cmd)
	if m == nil || m.FileInputs[flag] == "" {
		return nil, internalError("ReadFileInput needs a flag declared in Meta.FileInputs.")
	}
	fi, e := a.fileInput(st, cmd, flag)
	if e != nil {
		return nil, e
	}
	if fi == nil {
		return nil, nil
	}
	return append([]byte{}, fi.content...), nil
}

// SetResolved replaces a flag's value with its resolved form before Write (for
// example a ref or URL resolved to an id), so equivalent inputs bind identically
// (CLI-054: bind resolved values, not input forms). Slice flags take every value.
// Only call it for flags the user gave; it keeps the flag marked as given.
func SetResolved(cmd *cobra.Command, flag string, values ...string) error {
	f := lookupFlag(cmd, flag)
	if f == nil || len(values) == 0 {
		return internalError("SetResolved needs a defined flag and at least one value.")
	}
	if sv, ok := f.Value.(pflag.SliceValue); ok {
		if err := sv.Replace(values); err != nil {
			return Usage("invalid_value", "--"+flag, fmt.Sprintf("--%s has a value of the wrong type.", flag))
		}
		f.Changed = true
		return nil
	}
	if err := f.Value.Set(values[0]); err != nil {
		return Usage("invalid_value", "--"+flag, fmt.Sprintf("--%s has a value of the wrong type.", flag))
	}
	f.Changed = true
	return nil
}

// fileInput reads (once) and caches the content behind a file flag.
func (a *App) fileInput(st *runState, cmd *cobra.Command, flag string) (*fileInput, *Error) {
	if fi, ok := st.files[flag]; ok {
		return fi, nil
	}
	f := lookupFlag(cmd, flag)
	if f == nil || !f.Changed {
		return nil, nil
	}
	path := f.Value.String()
	var content []byte
	if path == "-" {
		if st.stdinFlag != "" && st.stdinFlag != flag {
			return nil, Usage("conflicting_flags", "--"+flag, "Only one flag can read stdin (-).")
		}
		st.stdinFlag = flag
		b, err := io.ReadAll(io.LimitReader(a.stdin(), maxFileInputBytes+1))
		if err != nil {
			return nil, ioError("stdin could not be read; nothing was sent.", "Pass the input as a file instead.", err)
		}
		if len(b) > maxFileInputBytes {
			return nil, tooLarge(flag)
		}
		content = b
	} else {
		info, err := os.Stat(path)
		switch {
		case errors.Is(err, fs.ErrNotExist):
			e := Usage("invalid_value", "--"+flag, fmt.Sprintf("The file given to --%s does not exist.", flag))
			e.Details["next_action"] = "Check the path; relative paths are relative to the current directory."
			return nil, e
		case err != nil:
			return nil, ioError(fmt.Sprintf("The file given to --%s could not be read; nothing was sent.", flag), "Check the file's permissions.", err)
		case info.IsDir():
			return nil, Usage("invalid_value", "--"+flag, fmt.Sprintf("--%s needs a file, not a directory.", flag))
		case info.Size() > maxFileInputBytes:
			return nil, tooLarge(flag)
		}
		fh, err := os.Open(path)
		if err != nil {
			return nil, ioError(fmt.Sprintf("The file given to --%s could not be opened; nothing was sent.", flag), "Check the file's permissions.", err)
		}
		b, err := io.ReadAll(io.LimitReader(fh, maxFileInputBytes+1))
		_ = fh.Close()
		if err != nil {
			return nil, ioError(fmt.Sprintf("The file given to --%s could not be read; nothing was sent.", flag), "Check the file.", err)
		}
		if len(b) > maxFileInputBytes {
			return nil, tooLarge(flag)
		}
		content = b
	}
	if st.files == nil {
		st.files = map[string]*fileInput{}
	}
	fi := &fileInput{path: path, content: content}
	st.files[flag] = fi
	return fi, nil
}

func tooLarge(flag string) *Error {
	e := Usage("input_too_large", "--"+flag, fmt.Sprintf("The input given to --%s is larger than %d MiB.", flag, maxFileInputBytes>>20))
	e.Details["next_action"] = "Use a smaller input."
	return e
}

// binding is a normalised invocation (CLI-054): every supplied parameter except the
// control flags, by canonical name, with file contents and secrets as digests.
type binding struct {
	command string
	values  map[string]string // name -> bound value
	digests map[string]string // name -> sha256 of the bound value
	display map[string]any    // snake_case name -> value shown in preview.inputs
	labels  map[string]string // name -> parameter label used in token_mismatch
	inv     string
}

// bindInvocation normalises the running command's invocation. key keys the digests
// of secret values (HMAC), so no unsalted hash of a secret is ever stored.
func (a *App) bindInvocation(st *runState, cmd *cobra.Command, m *Meta, key []byte) (*binding, *Error) {
	b := &binding{command: cmdPath(cmd), values: map[string]string{}, digests: map[string]string{},
		display: map[string]any{}, labels: map[string]string{}}
	boundFromFile := map[string]string{}
	for _, file := range sortedKeys(m.FileInputs) {
		bound := m.FileInputs[file]
		if _, dup := boundFromFile[bound]; !dup {
			boundFromFile[bound] = file
		}
	}
	var failure *Error
	cmd.Flags().VisitAll(func(f *pflag.Flag) {
		if failure != nil || !f.Changed || contains(excludedFromBinding, f.Name) {
			return
		}
		if bound, isFile := m.FileInputs[f.Name]; isFile {
			fi, e := a.fileInput(st, cmd, f.Name)
			if e != nil {
				failure = e
				return
			}
			secret := m.isSecret(f.Name) || m.isSecret(bound)
			var shown any = map[string]any{"sha256": sha256Hex(fi.content), "bytes": len(fi.content)}
			if secret {
				shown = "***"
			}
			b.add(bound, contentValue(fi.content, secret, key, bound), shown, labelFor(cmd, bound, f.Name))
			return
		}
		if _, isBound := boundFromFile[f.Name]; isBound {
			secret := m.isSecret(f.Name)
			value := f.Value.String()
			shown := displayString(value)
			if secret {
				shown = "***"
			}
			b.add(f.Name, contentValue([]byte(value), secret, key, f.Name), shown, "--"+f.Name)
			return
		}
		value := flagValue(f)
		var shown any
		if m.isSecret(f.Name) {
			value = secretDigest(key, f.Name, []byte(value))
			shown = "***"
		} else {
			shown = displayFlag(f)
		}
		b.add(f.Name, value, shown, "--"+f.Name)
	})
	if failure != nil {
		return nil, failure
	}
	if !st.positionalConsumed {
		if args := cmd.Flags().Args(); len(args) > 0 {
			enc, _ := canonicalJSON(claimStrings(args))
			shown := make([]any, len(args))
			for i, s := range args {
				shown[i] = displayString(s)
			}
			b.add("args", string(enc), shown, "args")
		}
	}
	sum, err := canonicalJSON(b.digests)
	if err != nil {
		return nil, internalError("The invocation could not be encoded.")
	}
	b.inv = "sha256:" + sha256Hex(sum)
	return b, nil
}

func (b *binding) add(name, value string, shown any, label string) {
	b.values[name] = value
	b.digests[name] = "sha256:" + sha256Hex([]byte(value))
	b.display[snake(name)] = shown
	b.labels[name] = label
}

// labelFor names the parameter of a file-bound value: the literal flag when the
// command has one, else the file flag.
func labelFor(cmd *cobra.Command, bound, file string) string {
	if lookupFlag(cmd, bound) != nil {
		return "--" + bound
	}
	return "--" + file
}

// flagValue is a flag's bound value: Value.String() for scalars, a JSON array for
// slices (order kept). Invalid UTF-8 is bound by the digest of its raw bytes.
func flagValue(f *pflag.Flag) string {
	if sv, ok := f.Value.(pflag.SliceValue); ok {
		enc, _ := canonicalJSON(claimStrings(sv.GetSlice()))
		return string(enc)
	}
	return claimString(f.Value.String())
}

// contentValue binds content by digest: sha256 for plain content, an HMAC keyed with
// the confirm key for secrets.
func contentValue(content []byte, secret bool, key []byte, name string) string {
	if secret {
		return secretDigest(key, name, content)
	}
	return "sha256:" + sha256Hex(content)
}

// secretDigest is the keyed digest that binds a secret without revealing it.
func secretDigest(key []byte, name string, value []byte) string {
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte("aicli-secret/2\x00"))
	mac.Write([]byte(name))
	mac.Write([]byte{0})
	mac.Write(value)
	return "hmac-sha256:" + hex.EncodeToString(mac.Sum(nil))
}

func sha256Hex(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// displayFlag is a flag's value as shown in preview.inputs.
func displayFlag(f *pflag.Flag) any {
	if sv, ok := f.Value.(pflag.SliceValue); ok {
		list := sv.GetSlice()
		out := make([]any, len(list))
		for i, s := range list {
			out[i] = displayString(s)
		}
		return out
	}
	s := f.Value.String()
	switch typ, _ := flagType(f); typ {
	case "boolean":
		if v, err := strconv.ParseBool(s); err == nil {
			return v
		}
	case "integer", "number":
		if _, err := strconv.ParseFloat(s, 64); err == nil {
			return json.Number(s)
		}
	}
	return displayString(s)
}

// displayString shows a value, or its digest and size when it is long.
func displayString(s string) any {
	if utf8.RuneCountInString(s) <= 256 && utf8.ValidString(s) {
		return s
	}
	return map[string]any{"sha256": sha256Hex([]byte(s)), "bytes": len(s)}
}

func snake(name string) string {
	return strings.ReplaceAll(name, "-", "_")
}

// claimString keeps valid UTF-8 as is and replaces anything else with the digest of
// its raw bytes, so distinct inputs never collapse into one canonical value.
func claimString(s string) string {
	if utf8.ValidString(s) {
		return s
	}
	return "bytes-sha256:" + sha256Hex([]byte(s))
}

func claimStrings(list []string) []string {
	out := make([]string, len(list))
	for i, s := range list {
		out[i] = claimString(s)
	}
	return out
}

// canonicalJSON encodes v with sorted object keys, no whitespace and minimal string
// escaping: '"' and '\' are escaped, control characters below U+0020 become \u00xx
// (lowercase hex), everything else is literal UTF-8. Supported values: nil, bool,
// string (valid UTF-8 only), int, int64, []string, []any, map[string]string and
// map[string]any. The Python library encodes claims the same way.
func canonicalJSON(v any) ([]byte, error) {
	var b bytes.Buffer
	if err := writeCanonical(&b, v); err != nil {
		return nil, err
	}
	return b.Bytes(), nil
}

func writeCanonical(b *bytes.Buffer, v any) error {
	switch x := v.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		if x {
			b.WriteString("true")
		} else {
			b.WriteString("false")
		}
	case string:
		return writeCanonicalString(b, x)
	case int:
		b.WriteString(strconv.Itoa(x))
	case int64:
		b.WriteString(strconv.FormatInt(x, 10))
	case []string:
		b.WriteByte('[')
		for i, s := range x {
			if i > 0 {
				b.WriteByte(',')
			}
			if err := writeCanonicalString(b, s); err != nil {
				return err
			}
		}
		b.WriteByte(']')
	case []any:
		b.WriteByte('[')
		for i, s := range x {
			if i > 0 {
				b.WriteByte(',')
			}
			if err := writeCanonical(b, s); err != nil {
				return err
			}
		}
		b.WriteByte(']')
	case map[string]string:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		b.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				b.WriteByte(',')
			}
			if err := writeCanonicalString(b, k); err != nil {
				return err
			}
			b.WriteByte(':')
			if err := writeCanonicalString(b, x[k]); err != nil {
				return err
			}
		}
		b.WriteByte('}')
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		b.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				b.WriteByte(',')
			}
			if err := writeCanonicalString(b, k); err != nil {
				return err
			}
			b.WriteByte(':')
			if err := writeCanonical(b, x[k]); err != nil {
				return err
			}
		}
		b.WriteByte('}')
	default:
		return fmt.Errorf("canonical JSON: unsupported type %T", v)
	}
	return nil
}

func writeCanonicalString(b *bytes.Buffer, s string) error {
	if !utf8.ValidString(s) {
		return errors.New("canonical JSON: string is not valid UTF-8")
	}
	const hexDigits = "0123456789abcdef"
	b.WriteByte('"')
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c == '"':
			b.WriteString(`\"`)
		case c == '\\':
			b.WriteString(`\\`)
		case c < 0x20:
			b.WriteString(`\u00`)
			b.WriteByte(hexDigits[c>>4])
			b.WriteByte(hexDigits[c&0xf])
		default:
			b.WriteByte(c)
		}
	}
	b.WriteByte('"')
	return nil
}
