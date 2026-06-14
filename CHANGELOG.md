# Changelog

All notable changes to TorFlash are documented here.

## [1.9.1] — 2026-06-14
Bugfix release.
- Fix crash when clicking through search results on a slow network — a
  background poster/metadata thread could be garbage-collected while still
  running ("QThread: Destroyed while thread is still running" → abort). All
  background fetchers are now held until they finish.
- Fix "magnet sometimes doesn't copy": NNM/RuTracker listings carry no magnet
  (it lives on the detail page) — copy now fetches it on demand and reports
  honestly when unavailable, and writes both the clipboard and the X11 PRIMARY
  selection.
- Internal: source reorganized into a proper `torflash/` package (one class
  per file); no behaviour change.

## [1.9.0] — 2026-06-14
Security and stability release (post-review hardening).
- Verified auto-update: the downloaded binary is now checked against the
  release SHA-256 before install; mismatch or missing checksum aborts the
  update (CI publishes `.sha256` for the binary and AppImage)
- Path-traversal protection: malicious torrent file names can no longer write
  or delete outside the target folder (copy-to-flash, library remove, CLI)
- Thread-safe seeding session — shared library/handles/stats guarded by a lock
- Atomic `library.json` / `stats.json` writes (temp file + rename) — no more
  corrupted state on crash or full disk
- Clean shutdown waits for in-flight downloads/copies; quitting mid-download no
  longer leaves half-written files or deletes partial downloads
- Safe eject hardening: subprocess timeouts (no UI hang), correct parent-device
  resolution for NVMe/eMMC, re-entrancy guard
- RuTracker credentials file restricted to 0600
- Fixed the headless CLI (`torflash_cli.py`), broken by the multi-provider refactor
- Expanded test suite (28 → 51 tests)

## [1.8.0] — 2026-05-26
- Single-instance guard — prevents launching multiple copies
- KDE Plasma 6 font fix — explicit Noto Sans to avoid Qt5/Qt6 format mismatch
- Issue templates, GitHub Pages landing page
- CI: tests in GitHub Actions, PyQt5 mocking for headless environments
- AUR package, AppImage build, .desktop file, contributing guide

## [1.7.0] — 2026-05-25
- Global proxy support for all providers
- Eject status feedback (shows which process holds the device)
- i18n — Russian/English with language switcher in settings

## [1.6.0] — 2026-05-25
- RuTracker provider (login + proxy support)
- Poster screenshots gallery (click to enlarge)
- Live download/upload speed graph
- Daily stats — today's and all-time totals (90-day retention)
- Expanded test suite

## [1.5.0] — 2026-05-24
- Parallel downloads
- File selection within torrents
- Incremental copy (resume interrupted transfers)
- Download/upload statistics
- Keyboard shortcuts
- Desktop notifications on completion

## [1.4.0] — 2026-05-24
- Flash drive management tab (contents, per-file delete, open in FM)
- NoNaMe-Club search provider
- Download queue fixes

## [1.3.0] — 2026-05-24
- Posters and descriptions in detail panel
- Category filter (movies, series, games, music, etc.)
- Search history with autocomplete
- Download queue
- Mediainfo in detail panel (codec, resolution, audio)
- Light / Dark / Auto themes
- CLI mode (`torflash_cli.py`)

## [1.2.0] — 2026-05-23
- Library detail view
- MKV-aware splitting via `mkvmerge`
- Themed icons
- Logging to `~/.local/share/TorFlash/torflash.log`

## [1.1.0] — 2026-05-23
- Persistent seeding — library stays and seeds while app is open
- Library tab
- Settings tab (autostart, rate limits)
- Autostart at login

## [1.0.0] — 2026-05-23
- Initial release
- Search torrents on rutor.info (mirror fallback)
- Download via libtorrent
- Auto-detect USB flash drive
- Smart splitting for FAT32 (MKV via mkvmerge, byte-split fallback)
- Safe eject (udisksctl)
