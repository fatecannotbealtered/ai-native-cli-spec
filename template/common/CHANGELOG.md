# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!--
How this file is used:
- The build embeds it: `{{TOOL_NAME}} changelog --since <version>` serves it to agents after an
  update (CLI-037), and the release workflow copies the version's section into the release notes
  (REPO-032). Write entries for both readers: name the commands, flags, fields and error codes
  that changed.
- Add entries under [Unreleased]. `npm version <patch|minor|major>` turns them into a dated
  section for the new version (scripts/sync-version.js), and scripts/check-version.js fails CI
  while the newest dated section differs from the package.json version (REPO-031).
- A change that breaks agents (envelope, keys, error codes, command paths or parameters, the
  write flow) needs a new MAJOR version (REPO-030).
- Replace YYYY-MM-DD below with the date of the first release when you tag it.
-->

## [Unreleased]

### Added

### Changed

### Fixed

### Deprecated

### Removed

### Security

## [{{VERSION}}] - YYYY-MM-DD

### Added

- First release.
