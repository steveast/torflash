"""TorFlash: константы, пути и глобальное состояние прокси (без Qt)."""

import sys
from pathlib import Path


APP_NAME = "TorFlash"


APP_VERSION = "1.9.1"


GITHUB_REPO = "steveast/torflash"


# Публичный ключ minisign для проверки подписи обновлений (вторая строка .pub,
# key id 9C9CBE581B22E937). Непусто → обновление ставится только с валидной
# подписью .minisig (приватный ключ — в repo secret MINISIGN_KEY, подпись в CI).
MINISIGN_PUBKEY = "RWQ36SIbWL6cnKN/xGvXX4TOAD7n1cvJ6lYMA6wEpWQpu6fxvhxwRf7r"


def _assets_dir() -> Path:
    """Папка с иконками и ресурсами. PyInstaller кладёт --add-data в _MEIPASS,
    при запуске из исходников — ../assets относительно src/."""
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        return Path(mei)
    return Path(__file__).resolve().parent.parent / "assets"


ASSETS_DIR = _assets_dir()


LIBRARY_DIR = Path.home() / ".local" / "share" / "TorFlash"


TORRENTS_CACHE_DIR = LIBRARY_DIR / "torrents"


RESUME_DIR = LIBRARY_DIR / "resume"


LIBRARY_FILE = LIBRARY_DIR / "library.json"


STORAGE_DEFAULT = Path.home() / "Storage"


AUTOSTART_FILE = Path.home() / ".config" / "autostart" / "TorFlash.desktop"


EXTRA_TRACKERS = [
    "https://tracker.opentrackr.org:443/announce",
    "https://tracker.gbitt.info:443/announce",
    "https://tracker1.520.jp:443/announce",
    "http://tracker.openbittorrent.com:80/announce",
    "http://retracker.local/announce",
]


SEARCH_HISTORY_MAX = 30


_proxy: str = ""


def _proxies() -> dict:
    """Return requests-compatible proxies dict from the global proxy setting."""
    return {"http": _proxy, "https": _proxy} if _proxy else {}


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


FAT32_MAX_PART = int(3.9 * 1024 ** 3)


VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm", ".ts", ".m2ts",
              ".mpg", ".mpeg", ".wmv", ".flv", ".3gp", ".vob"}


STATS_FILE = LIBRARY_DIR / "stats.json"


def set_proxy(p: str):
    global _proxy
    _proxy = p or ""


def current_proxy() -> str:
    return _proxy
