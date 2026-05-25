# ⚡ TorFlash

English · **[Русский](README.ru.md)**

> Search torrents on [rutor.info](https://rutor.info), download and auto-copy them to a USB flash drive — splitting large files for FAT32 along the way.

<p align="center">
  <img src="screenshot.png" alt="TorFlash screenshot" width="800">
</p>

A Linux desktop app in PyQt5. Downloads via `libtorrent-rasterbar`, scrapes rutor.info over HTTP, stores movies on your flash drive — no more "plug in the stick, open KTorrent, wait, copy manually" dance.

## Features

### Search
- 🔍 **Mirror fallback** across `rutor.info`, `rutor.is`, `rutor.org`
- 📂 **Category filter** — movies / series / cartoons / games / music / books / software / sport / etc.
- 🕘 **Query history** with autocomplete
- 🖼 **Poster + description** in the detail panel (parsed from the torrent page)
- 🧲 **Magnet + .torrent** — fetches `.torrent` straight from rutor for instant metadata

### Library & seeding
- 📚 **Persistent library**: everything stays in `~/Storage` and keeps seeding while the app is open
- 🌱 **Seeding restored on startup** via `resume_data` + cached `.torrent` files
- ⏯ **Pause / Resume / Re-check** per torrent, queue multiple downloads
- 🎞 **Mediainfo** in detail panel (codec, resolution, audio tracks, duration) via `mediainfo` or `ffprobe`

### Flash drive
- 💾 **Auto-detect** USB at `/run/media/$USER/*`, copy to `Movies/`
- ✂️ **Smart splitting** for FAT32 (> 3.9 GiB):
  - **MKV** via `mkvmerge --split size:NM` — each part is a standalone playable MKV
  - Other formats — byte-split with the extension preserved (`name.part000.mkv`)
- 📁 **Flash overview tab**: free space, listed contents, per-file delete, open in file manager
- ⏏ **Safe eject** (`udisksctl unmount` + `power-off`); shows which process is holding the device if busy
- 🔁 **Pending flash copy** survives restart — flag stored in `library.json`, auto-copies when the torrent finishes and the flash is back

### App & control
- 🎯 **Open in KTorrent** in one click
- 📊 Inline progress in the same panel (blue — download, green — copy), no blocking modals
- 🎨 **Theme**: auto / light / dark
- 🚦 **Rate limits** (down / up KB/s) in settings
- ⚙️ **Settings**: autostart at login, hidden start, minimize-to-tray on close
- 🔄 **Self-update** from GitHub Releases — manual + automatic daily check
- 🔧 **CLI mode** for headless use: `torflash_cli.py search QUERY | list | download URL | remove HASH`

## Screenshot

UI: list on the left, detail card on the right, progress embedded in the card.

## Install

### Pre-built binary (recommended)

```bash
mkdir -p ~/Apps/TorFlash && cd ~/Apps/TorFlash
curl -L -o TorFlash https://github.com/steveast/torflash/releases/latest/download/TorFlash
chmod +x TorFlash
./TorFlash
```

The binary is built with PyInstaller; it bundles Python, PyQt5, libtorrent and requests. Only system libraries are required: Qt5, glibc, OpenSSL.

### From source

You'll need Python 3.11+ and these system packages (Arch):

```bash
sudo pacman -S libtorrent-rasterbar python-pyqt5 python-requests mkvtoolnix-cli
git clone https://github.com/steveast/torflash.git
cd torflash
python3 src/rutor_search.py
```

For other distros, `libtorrent-rasterbar` with Python bindings ships as `python3-libtorrent` on Debian/Ubuntu or `python-libtorrent` on rpm-based systems.

## Building the binary yourself

```bash
python3 -m venv --system-site-packages .build-venv
.build-venv/bin/pip install pyinstaller
.build-venv/bin/pyinstaller --clean --noconfirm TorFlash.spec
# Output: dist/TorFlash
```

## Networking notes

For restrictive networks (VPNs, corporate firewalls):

- UDP trackers and DHT bootstrap may be blocked → the app only uses HTTPS/HTTP trackers
- Metadata is taken **directly** from rutor's `.torrent` file (no DHT roundtrip needed)
- uTP between peers stays enabled — that's TCP-fallback BitTorrent over UDP and usually traverses NATs even when raw UDP is filtered

## Usage

1. Type a query → Enter
2. Pick a result in the list (details show on the right)
3. Double-click or press "Download → flash"
4. Progress: blue = downloading, green = copying
5. Done — press ⏏ to safely eject

Uncheck "Mirror to flash" and everything just lands in `~/Storage`.

## Architecture

- `src/rutor_search.py` — main module
- `SearchWorker` — rutor.info HTML scraping with regex (no BeautifulSoup)
- `SeedSession` — persistent `libtorrent.session`, library in `~/.local/share/TorFlash/library.json`, resume data, `.torrent` cache
- `DownloadWorker` — adds torrents to the shared session, watches progress, leaves them seeding when done
- `CopyWorker` — streaming copy with MKV-aware splitting (mkvmerge) or fallback byte-split
- `UpdateChecker` / `UpdateDownloader` — GitHub Releases API + `os.execv` self-restart after update
- `SettingsDialog` — autostart (`~/.config/autostart/TorFlash.desktop`), hidden start, minimize-to-tray
- `MainWindow` — `QTabWidget` (search + library tabs), split-views inside each

## Logs

When running the bundled binary, logs are written to `~/.local/share/TorFlash/torflash.log`.

## License

MIT
