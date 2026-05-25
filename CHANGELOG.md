# Changelog

All notable changes to TorFlash are documented here.

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
