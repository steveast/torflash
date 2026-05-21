# ⚡ TorFlash

English · **[Русский](README.ru.md)**

> Search torrents on [rutor.info](https://rutor.info), download and auto-copy them to a USB flash drive — splitting large files for FAT32 along the way.

<p align="center">
  <img src="screenshot.png" alt="TorFlash screenshot" width="800">
</p>

A Linux desktop app in PyQt5. Downloads via `libtorrent-rasterbar`, scrapes rutor.info over HTTP, stores movies on your flash drive — no more "plug in the stick, open KTorrent, wait, copy manually" dance.

## Features

- 🔍 **Search** on rutor.info with automatic mirror fallback (`rutor.info`, `rutor.is`, `rutor.org`)
- 🧲 **Magnet + .torrent**: fetches the `.torrent` straight from rutor — instant metadata, no waiting on DHT
- 📚 **Persistent library**: every download stays in `~/Storage` and keeps seeding while the app is open
- 🌱 **Seeding** is restored on startup via `resume_data` + cached `.torrent` files
- 💾 **Mirror to flash → `Movies`**: auto-detects USB mount in `/run/media/$USER/*`, creates the folder
- ✂️ **Auto-split** files > 3.9 GiB for FAT32:
  - **MKV** files are split with `mkvmerge --split size:NM` — each part is a valid playable MKV
  - Other formats fall back to byte-split with extension preserved (`name.part000.mkv`)
- ⏏ **Safe eject** with one click (`udisksctl unmount` + `power-off`); shows which process holds the device if busy
- 🎯 **Open in KTorrent** in one click — for low-seeded torrents
- 📊 Progress **inline** (blue — download, green — copy), no blocking modals
- ⚙️ **Settings**: autostart at login, hidden start, minimize-to-tray on close
- 🔄 **Self-update** from GitHub Releases — option in tray menu

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
python3 rutor_search.py
```

For other distros, `libtorrent-rasterbar` with Python bindings ships as `python3-libtorrent` on Debian/Ubuntu or `python-libtorrent` on rpm-based systems.

## Building the binary yourself

```bash
python3 -m venv --system-site-packages .build-venv
.build-venv/bin/pip install pyinstaller
.build-venv/bin/pyinstaller --onefile --windowed --name TorFlash \
    --add-data "torflash.svg:." \
    --add-data "torflash-tray.svg:." \
    --add-data "torflash-tray-22.png:." \
    --add-data "torflash-tray-32.png:." \
    --add-data "torflash-tray-48.png:." \
    rutor_search.py
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

- `rutor_search.py` — single module (~1700 lines)
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
