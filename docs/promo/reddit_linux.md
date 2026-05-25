# Reddit: r/linux

**Title:** TorFlash — search torrents and auto-copy to USB flash drive (PyQt5, libtorrent, open source)

**Body:**

Hey everyone,

I built a Linux desktop app that scratches a very specific itch: searching for torrents, downloading them, and copying to a USB flash drive — all in one window.

**The problem it solves:** My TV reads files from a USB stick but only FAT32, so anything over 4 GB needs splitting. The usual workflow is: open browser → find torrent → open KTorrent → wait → open file manager → copy → realize it's too big → split manually → eject. TorFlash does all of this in one click.

**What it does:**
- Search across Rutor, NoNaMe-Club, and RuTracker (with login/proxy)
- Download via libtorrent (parallel, file selection, resume)
- Auto-detect USB at `/run/media/$USER/*`
- Smart FAT32 splitting: MKV files via `mkvmerge` (each part is playable), others via byte-split
- Safe eject with `udisksctl`
- Library with persistent seeding, mediainfo, speed graph, daily stats
- Light/dark/auto themes, Russian/English UI

**Tech stack:** Python, PyQt5, libtorrent-rasterbar, regex HTML parsing (no BeautifulSoup)

**Install:**
```
curl -L -o TorFlash https://github.com/steveast/torflash/releases/latest/download/TorFlash
chmod +x TorFlash && ./TorFlash
```

Single binary, no pip install, no Docker. AppImage also available.

GitHub: https://github.com/steveast/torflash
Website: https://steveast.github.io/torflash/

MIT license. Feedback and PRs welcome!

---

# Reddit: r/selfhosted

**Title:** TorFlash — torrent search + download + USB flash copy, all in one Linux app

**Body:**

Built a desktop app for a specific workflow: search torrents → download → copy to USB flash drive with FAT32 splitting. One window, no manual steps.

**Why I built it:** I watch movies on a TV that reads USB sticks (FAT32 only). The "search → download → split → copy → eject" dance got old fast.

**Features:**
- Multi-source search: Rutor, NoNaMe-Club, RuTracker
- libtorrent downloads with parallel transfers, file selection
- Auto-detect USB, smart MKV splitting for FAT32
- Persistent library with seeding, mediainfo, speed graphs
- Self-update from GitHub Releases
- CLI mode for headless use: `torflash_cli.py search QUERY`

Single binary or AppImage, no deps to install.

https://github.com/steveast/torflash

Would love feedback from anyone with a similar setup!
