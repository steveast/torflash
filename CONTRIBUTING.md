# Contributing to TorFlash

Thanks for your interest! Here's how to get started.

## Quick start

```bash
# Clone and run from source (Arch Linux)
sudo pacman -S libtorrent-rasterbar python-pyqt5 python-requests mkvtoolnix-cli
git clone https://github.com/steveast/torflash.git
cd torflash
python3 src/rutor_search.py
```

For Debian/Ubuntu use `python3-libtorrent` and `python3-pyqt5`.

## Project structure

```
src/
  rutor_search.py     — main module, UI, download/copy logic
  rutor_meta.py       — torrent metadata fetching
  mediainfo.py        — mediainfo/ffprobe wrapper
  themes.py           — light/dark/auto theme
  providers/
    base.py           — base class for search providers
    rutor.py          — rutor.info provider
    nnm.py            — NoNaMe-Club provider
    rutracker.py      — RuTracker provider
assets/               — icons, screenshots
```

## Adding a search provider

1. Create `src/providers/yoursite.py`
2. Subclass `BaseProvider` from `providers/base.py`
3. Implement `search(query, category)` and `fetch_details(url)`
4. Register it in `src/rutor_search.py` provider list
5. Test with `python3 src/torflash_cli.py search "test"`

## Code style

- No external linters enforced — just keep it consistent with existing code
- PyQt5 signals/slots, no threads except `QThread` workers
- Regex-based HTML parsing (no BeautifulSoup dependency)

## Submitting changes

1. Fork the repo
2. Create a feature branch: `git checkout -b feat/my-feature`
3. Commit with a descriptive message
4. Open a Pull Request against `main`

## Reporting bugs

Open a [GitHub Issue](https://github.com/steveast/torflash/issues) with:
- What you expected vs what happened
- TorFlash version (`--version` or Settings tab)
- Linux distro and desktop environment
- Log file: `~/.local/share/TorFlash/torflash.log`
