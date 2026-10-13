// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Sean Guo

package aicli

import (
	"strings"
	"testing"
)

func TestParseVersionIsStrict(t *testing.T) {
	good := map[string]Version{
		"1.2.3":                {1, 2, 3, "", ""},
		"v1.2.3":               {1, 2, 3, "", ""},
		"0.0.0":                {0, 0, 0, "", ""},
		"1.0.0-rc.1":           {1, 0, 0, "rc.1", ""},
		"1.0.0-alpha-1.x+b.42": {1, 0, 0, "alpha-1.x", "b.42"},
		"10.20.30":             {10, 20, 30, "", ""},
	}
	for s, want := range good {
		got, err := ParseVersion(s)
		if err != nil || got != want {
			t.Errorf("%q: got %+v %v", s, got, err)
		}
	}
	bad := []string{"", "1", "1.2", "1.2.3.4", "01.2.3", "1.02.3", "1.2.03", "V1.2.3", "vv1.2.3", " 1.2.3", "1.2.3 ",
		"1.2.3-", "1.2.3+", "1.2.3-01", "1.2.3-a..b", "1.0.12&calc", "1.2.3|whoami", "1.2.3;rm -rf /", "1.2.3`x`",
		"1.2.3$(x)", "1.2.3\n", "1.2.3%PATH%", "^1.2.3", "~1.2.3", "latest", "1.2.x", "99999999999999999999.0.0",
		"1.2.3-" + strings.Repeat("a", 200)}
	for _, s := range bad {
		if _, err := ParseVersion(s); err == nil {
			t.Errorf("%q was accepted", s)
		}
	}
}

func TestCompareVersionsFollowsSemverPrecedence(t *testing.T) {
	order := []string{"1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11",
		"1.0.0-rc.1", "1.0.0", "1.0.1", "1.1.0", "2.0.0"}
	for i := 0; i < len(order); i++ {
		for j := 0; j < len(order); j++ {
			a, _ := ParseVersion(order[i])
			b, _ := ParseVersion(order[j])
			want := 0
			if i < j {
				want = -1
			} else if i > j {
				want = 1
			}
			if got := CompareVersions(a, b); got != want {
				t.Errorf("compare(%s, %s) = %d, want %d", order[i], order[j], got, want)
			}
		}
	}
	a, _ := ParseVersion("1.0.0+build.1")
	b, _ := ParseVersion("1.0.0+build.2")
	if CompareVersions(a, b) != 0 {
		t.Error("build metadata must not affect precedence")
	}
}

func TestPackageManagerArgs(t *testing.T) {
	v, _ := ParseVersion("v1.4.0+meta")
	cases := []struct {
		manager, pkg string
		want         []string
	}{
		{"npm", "@acme/notes-cli", []string{"npm", "install", "-g", "@acme/notes-cli@1.4.0"}},
		{"pnpm", "notes-cli", []string{"pnpm", "add", "-g", "notes-cli@1.4.0"}},
		{"yarn", "notes-cli", []string{"yarn", "global", "add", "notes-cli@1.4.0"}},
		{"go", "github.com/acme/notes-cli/cmd/notes", []string{"go", "install", "github.com/acme/notes-cli/cmd/notes@v1.4.0"}},
		{"pip", "notes-cli", []string{"pip", "install", "--upgrade", "notes-cli==1.4.0"}},
		{"brew", "acme/tap/notes-cli", []string{"brew", "upgrade", "acme/tap/notes-cli"}},
	}
	for _, c := range cases {
		got, err := PackageManagerArgs(c.manager, c.pkg, v)
		if err != nil || strings.Join(got, " ") != strings.Join(c.want, " ") {
			t.Errorf("%s %s: got %v %v", c.manager, c.pkg, got, err)
		}
	}
	bad := []struct{ manager, pkg string }{
		{"npm", "notes&calc"}, {"npm", "Notes"}, {"npm", "--global"}, {"npm", "notes cli"}, {"npm", "notes|x"},
		{"go", "notes"}, {"go", "github.com/acme/../evil"}, {"go", "github.com/acme/x;rm"},
		{"pip", "notes cli"}, {"pip", "-r requirements.txt"}, {"brew", "Notes"}, {"brew", "x;y"},
		{"apt", "notes"}, {"", "notes"}, {"npm", "%PATH%"}, {"npm", "notes^"},
	}
	for _, c := range bad {
		if args, err := PackageManagerArgs(c.manager, c.pkg, v); err == nil {
			t.Errorf("%s %q accepted: %v", c.manager, c.pkg, args)
		}
	}
	pre, _ := ParseVersion("1.4.0-rc.1")
	if _, err := PackageManagerArgs("pip", "notes-cli", pre); err == nil {
		t.Error("pip must refuse pre-releases")
	}
	// every produced argument stays within a cmd.exe-safe character set
	safe := func(s string) bool {
		for _, r := range s {
			if (r < 'a' || r > 'z') && (r < 'A' || r > 'Z') && (r < '0' || r > '9') && !strings.ContainsRune("@/._~+=-", r) {
				return false
			}
		}
		return true
	}
	for _, c := range cases {
		args, _ := PackageManagerArgs(c.manager, c.pkg, pre)
		for _, a := range args {
			if !safe(a) {
				t.Errorf("unsafe argument %q", a)
			}
		}
	}
}

func TestCheckUpdateTarget(t *testing.T) {
	if _, e := CheckUpdateTarget("1.2.3", "1.0.12&calc", false); e == nil || e.Code != "E_USAGE" {
		t.Fatalf("an injected target must be E_USAGE: %v", e)
	}
	if _, e := CheckUpdateTarget("1.2.3", "1.2.0", false); e == nil || e.Details["parameter"] != "--target-version" {
		t.Fatalf("a downgrade must be refused: %v", e)
	}
	if v, e := CheckUpdateTarget("1.2.3", "1.2.0", true); e != nil || v.String() != "1.2.0" {
		t.Fatalf("an allowed downgrade: %v %v", v, e)
	}
	if v, e := CheckUpdateTarget("1.2.3", "v1.3.0", false); e != nil || v.String() != "1.3.0" {
		t.Fatalf("an upgrade: %v %v", v, e)
	}
	if _, e := CheckUpdateTarget("dev", "0.1.0", false); e != nil {
		t.Fatalf("a development build accepts any valid target: %v", e)
	}
}
