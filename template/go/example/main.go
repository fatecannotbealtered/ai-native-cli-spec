// Command notes is the example AI-native CLI built on the aicli library
// (ai-native-cli-spec 2.0, BLUEPRINT 5.5). Its upstream is a fake notes service kept
// in a JSON file (NOTES_UPSTREAM_FILE, default in the temp directory).
package main

import (
	"os"
	"path/filepath"
)

func main() {
	path := os.Getenv("NOTES_UPSTREAM_FILE")
	if path == "" {
		path = filepath.Join(os.TempDir(), "aicli-notes-example.json")
	}
	os.Exit(newApp(fileStore(path), nil).Execute(os.Args[1:]))
}
