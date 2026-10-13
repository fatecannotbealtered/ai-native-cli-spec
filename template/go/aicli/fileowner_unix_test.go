// Vendored from ai-native-cli-spec template/go/aicli. Do not edit in tool repos; run scripts/sync-spec.js.
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Sean Guo

//go:build unix

package aicli

import (
	"io/fs"
	"os"
	"syscall"
	"testing"
	"time"
)

type ownedInfo struct {
	uid  uint32
	mode fs.FileMode
}

func (o ownedInfo) Name() string       { return "scope.json" }
func (o ownedInfo) Size() int64        { return 0 }
func (o ownedInfo) Mode() fs.FileMode  { return o.mode }
func (o ownedInfo) ModTime() time.Time { return time.Time{} }
func (o ownedInfo) IsDir() bool        { return false }
func (o ownedInfo) Sys() any           { return &syscall.Stat_t{Uid: o.uid} }

// C4: a scope or settings file is trusted only when the user owns it and nobody
// else can write it (a file owned by another user cannot be made here without root).
func TestC4FileOwnedByAnotherUserIsNotTrusted(t *testing.T) {
	me := uint32(os.Getuid())
	for _, c := range []struct {
		info ownedInfo
		want string
	}{
		{ownedInfo{me, 0o600}, ""},
		{ownedInfo{me + 1, 0o600}, "/x/scope.json is not owned by the current user"},
		{ownedInfo{me, 0o620}, "/x/scope.json is writable by other users; only its owner may write it"},
		{ownedInfo{me, 0o602}, "/x/scope.json is writable by other users; only its owner may write it"},
	} {
		if got := fileTrustProblem("/x/scope.json", c.info); got != c.want {
			t.Errorf("uid %d mode %v: got %q, want %q", c.info.uid, c.info.mode, got, c.want)
		}
	}
}
