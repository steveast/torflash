#!/usr/bin/env python3
"""TorFlash — поиск торрентов rutor.info и закачка на флешку с разбиением для FAT32."""

APP_NAME = "TorFlash"
APP_VERSION = "1.3.0"
GITHUB_REPO = "steveast/torflash"

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from html import unescape
from pathlib import Path
from urllib.parse import quote

import requests

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

# Категории rutor.info: id → название.
# URL поиска: /search/0/<cat>/000/0/<query>; <cat>=0 — все.
RUTOR_CATEGORIES = [
    (0, "Все"),
    (1, "Зарубежные фильмы"),
    (5, "Наше кино"),
    (4, "Зарубежные сериалы"),
    (16, "Наши сериалы"),
    (7, "Мультфильмы"),
    (8, "Игры"),
    (9, "Аниме"),
    (10, "Музыка"),
    (11, "Книги"),
    (12, "Спорт и здоровье"),
    (13, "Юмор"),
    (14, "Документальные"),
    (15, "Софт"),
    (17, "Зарубежные мультфильмы"),
]
SEARCH_HISTORY_MAX = 30
from PyQt5.QtCore import Qt, QSettings, QThread, pyqtSignal
from PyQt5.QtGui import QFont, QGuiApplication, QIcon
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QStyle,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

MIRRORS = [
    "https://rutor.info",
    "https://rutor.is",
    "http://rutor.org",
]
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

ROW_RE = re.compile(r"<tr[^>]*class=['\"]?(?:gai|tum)['\"]?[^>]*>(.*?)</tr>", re.S)
CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
MAGNET_RE = re.compile(r'href="(magnet:\?[^"]+)"')
PAGE_RE = re.compile(r'href="(/torrent/\d+[^"]*)"')
TITLE_RE = re.compile(r'<a href="/torrent/\d+[^"]*">(.*?)</a>', re.S)
SEED_RE = re.compile(r'<span class="green">.*?(\d+)\s*</span>', re.S)
LEECH_RE = re.compile(r'<span class="red">[^<\d]*(\d+)\s*</span>', re.S)
DOWNLOAD_RE = re.compile(r'href="(?P<u>(?://|https?://)d\.rutor\.[^"]+/download/\d+[^"]*)"')
TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(html: str) -> str:
    return unescape(TAG_RE.sub("", html).replace("\xa0", " ")).strip()


def parse(html: str, base: str) -> list[dict]:
    results = []
    for row in ROW_RE.findall(html):
        cells = CELL_RE.findall(row)
        if len(cells) < 3:
            continue
        magnet_m = MAGNET_RE.search(row)
        title_m = TITLE_RE.search(row)
        page_m = PAGE_RE.search(row)
        if not (magnet_m and title_m):
            continue
        seed_m = SEED_RE.search(cells[-1])
        leech_m = LEECH_RE.search(cells[-1])
        dl_m = DOWNLOAD_RE.search(row)
        dl_url = ""
        if dl_m:
            dl_url = dl_m.group("u")
            if dl_url.startswith("//"):
                dl_url = "https:" + dl_url
        results.append({
            "date": strip_tags(cells[0]),
            "title": strip_tags(title_m.group(1)),
            "size": strip_tags(cells[-2]),
            "seeds": seed_m.group(1) if seed_m else "0",
            "leech": leech_m.group(1) if leech_m else "0",
            "magnet": magnet_m.group(1),
            "torrent_url": dl_url,
            "page": base + page_m.group(1) if page_m else "",
        })
    return results


# FAT32 не поддерживает файлы >= 4 GiB. Берём запас.
FAT32_MAX_PART = int(3.9 * 1024 ** 3)
DL_STATES = [
    "в очереди",
    "проверка",
    "получение метаданных",
    "скачивание",
    "завершено",
    "раздача",
    "выделение места",
    "проверка fastresume",
]


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


SIZE_RE = re.compile(r"([\d.,]+)\s*(KB|MB|GB|TB|B)?", re.I)
SIZE_FACTORS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}


def parse_size_text(s: str) -> int:
    if not s:
        return 0
    s = s.replace("\xa0", " ").strip()
    m = SIZE_RE.match(s)
    if not m:
        return 0
    try:
        val = float(m.group(1).replace(",", "."))
    except ValueError:
        return 0
    unit = (m.group(2) or "B").upper()
    return int(val * SIZE_FACTORS.get(unit, 1))


def fmt_time(seconds) -> str:
    if seconds is None:
        return "—"
    try:
        s = int(seconds)
    except (TypeError, ValueError, OverflowError):
        return "—"
    if s < 0 or s > 24 * 3600 * 99:
        return "—"
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def detect_flash_mount() -> str | None:
    base = Path(f"/run/media/{os.getlogin()}")
    if not base.exists():
        return None
    for child in base.iterdir():
        if child.is_dir() and os.access(child, os.W_OK):
            return str(child)
    return None


class SeedSession:
    """Постоянная libtorrent-сессия. Хранит библиотеку, переустанавливает торренты на старте."""

    def __init__(self):
        import libtorrent as lt
        self.lt = lt
        LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
        TORRENTS_CACHE_DIR.mkdir(exist_ok=True)
        RESUME_DIR.mkdir(exist_ok=True)
        STORAGE_DEFAULT.mkdir(parents=True, exist_ok=True)
        self.ses = lt.session({
            "listen_interfaces": "0.0.0.0:6881",
            "alert_mask": (
                lt.alert.category_t.error_notification
                | lt.alert.category_t.status_notification
                | lt.alert.category_t.storage_notification
            ),
            "enable_dht": True,
            "enable_lsd": False,
            "enable_upnp": False,
            "enable_natpmp": False,
            "announce_to_all_trackers": True,
            "announce_to_all_tiers": True,
            "enable_outgoing_utp": True,
            "enable_incoming_utp": True,
            "dht_bootstrap_nodes": (
                "router.bittorrent.com:6881,"
                "router.utorrent.com:6881,"
                "dht.transmissionbt.com:6881"
            ),
        })
        print(f"[seed] listening on {self.ses.listen_port()}", flush=True)
        self.handles: dict = {}
        self.library: dict = self._load_library()
        self._restore_torrents()

    def _load_library(self) -> dict:
        if LIBRARY_FILE.exists():
            try:
                return json.loads(LIBRARY_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_library(self):
        try:
            LIBRARY_FILE.write_text(
                json.dumps(self.library, ensure_ascii=False, indent=2)
            )
        except OSError as e:
            print(f"[seed] failed to save library: {e}", flush=True)

    def _hash_str(self, handle) -> str:
        try:
            ih = handle.info_hashes()
            v1 = str(ih.v1)
            return v1 if v1 != "0" * 40 else str(ih.v2)
        except AttributeError:
            return str(handle.info_hash())

    def _restore_torrents(self):
        for hid, meta in list(self.library.items()):
            rfile = RESUME_DIR / f"{hid}.dat"
            tfile = TORRENTS_CACHE_DIR / f"{hid}.torrent"
            try:
                if rfile.exists():
                    # read_resume_data возвращает add_torrent_params со всей нужной инфой
                    params = self.lt.read_resume_data(rfile.read_bytes())
                elif tfile.exists():
                    params = self.lt.add_torrent_params()
                    params.ti = self.lt.torrent_info(
                        self.lt.bdecode(tfile.read_bytes())
                    )
                    params.save_path = meta.get("save_path", str(STORAGE_DEFAULT))
                else:
                    params = self.lt.parse_magnet_uri(meta.get("magnet", ""))
                    params.save_path = meta.get("save_path", str(STORAGE_DEFAULT))
                params.trackers = list({*(params.trackers or []), *EXTRA_TRACKERS})
                handle = self.ses.add_torrent(params)
                self.handles[hid] = handle
                print(
                    f"[seed] restored {meta.get('title','?')[:60]} ({hid[:8]})",
                    flush=True,
                )
            except (RuntimeError, OSError, ValueError, TypeError) as e:
                print(f"[seed] restore failed for {hid}: {e}", flush=True)

    def add(self, magnet: str, torrent_url: str, save_path: str):
        params = None
        torrent_bytes = None
        if torrent_url:
            try:
                r = requests.get(
                    torrent_url, headers=HEADERS, timeout=20, allow_redirects=True
                )
                r.raise_for_status()
                torrent_bytes = r.content
                ti = self.lt.torrent_info(self.lt.bdecode(torrent_bytes))
                params = self.lt.add_torrent_params()
                params.ti = ti
            except (requests.RequestException, RuntimeError) as e:
                print(f"[seed] .torrent fetch failed: {e}, fallback to magnet", flush=True)
                params = None
        if params is None:
            params = self.lt.parse_magnet_uri(magnet)
        params.save_path = save_path
        params.trackers = list({*(params.trackers or []), *EXTRA_TRACKERS})
        handle = self.ses.add_torrent(params)
        handle.force_dht_announce()
        info_hash = self._hash_str(handle)
        self.handles[info_hash] = handle
        if info_hash not in self.library:
            self.library[info_hash] = {
                "hash": info_hash,
                "title": params.ti.name() if params.ti else "(получение метаданных…)",
                "size": params.ti.total_size() if params.ti else 0,
                "magnet": magnet,
                "torrent_url": torrent_url,
                "save_path": save_path,
                "added_at": time.time(),
                "completed_at": None,
            }
            if torrent_bytes:
                try:
                    (TORRENTS_CACHE_DIR / f"{info_hash}.torrent").write_bytes(torrent_bytes)
                except OSError:
                    pass
            self._save_library()
        return info_hash

    def update_metadata(self, info_hash: str):
        h = self.handles.get(info_hash)
        if not h or not h.status().has_metadata:
            return
        info = h.torrent_file()
        if info_hash in self.library:
            self.library[info_hash]["title"] = info.name()
            self.library[info_hash]["size"] = info.total_size()
            tfile = TORRENTS_CACHE_DIR / f"{info_hash}.torrent"
            if not tfile.exists():
                try:
                    ct = self.lt.create_torrent(info)
                    tfile.write_bytes(self.lt.bencode(ct.generate()))
                except Exception as e:
                    print(f"[seed] dump .torrent failed: {e}", flush=True)
            self._save_library()

    def remove(self, info_hash: str, delete_files: bool = False):
        h = self.handles.pop(info_hash, None)
        if h:
            try:
                self.ses.remove_torrent(h, 1 if delete_files else 0)
            except RuntimeError:
                pass
        meta = self.library.pop(info_hash, None)
        if delete_files and meta:
            # libtorrent's option=1 удалит payload. На всякий — подчистим пустую папку.
            save_path = Path(meta.get("save_path", STORAGE_DEFAULT))
            tfile = TORRENTS_CACHE_DIR / f"{info_hash}.torrent"
            if tfile.exists():
                try:
                    ti = self.lt.torrent_info(self.lt.bdecode(tfile.read_bytes()))
                    name = ti.name()
                    target = save_path / name
                    if target.exists():
                        if target.is_dir():
                            shutil.rmtree(target, ignore_errors=True)
                        else:
                            target.unlink(missing_ok=True)
                except Exception:
                    pass
        (TORRENTS_CACHE_DIR / f"{info_hash}.torrent").unlink(missing_ok=True)
        (RESUME_DIR / f"{info_hash}.dat").unlink(missing_ok=True)
        self._save_library()

    def get_status(self, info_hash: str):
        h = self.handles.get(info_hash)
        if not h:
            return None
        s = h.status()
        meta = self.library.get(info_hash, {})
        return {
            "hash": info_hash,
            "title": meta.get("title", "?"),
            "size": meta.get("size", 0),
            "progress": s.progress,
            "state_id": s.state,
            "state": DL_STATES[s.state] if 0 <= s.state < len(DL_STATES) else str(s.state),
            "download_rate": s.download_rate,
            "upload_rate": s.upload_rate,
            "num_peers": s.num_peers,
            "num_seeds": s.num_seeds,
            "is_seeding": s.is_seeding,
            "has_metadata": s.has_metadata,
            "save_path": meta.get("save_path", str(STORAGE_DEFAULT)),
        }

    def all_statuses(self) -> list:
        return [s for s in (self.get_status(h) for h in list(self.handles)) if s]

    def drain_alerts(self):
        for a in self.ses.pop_alerts():
            if isinstance(a, self.lt.save_resume_data_alert):
                try:
                    hid = self._hash_str(a.handle)
                    buf = self.lt.write_resume_data_buf(a.params)
                    (RESUME_DIR / f"{hid}.dat").write_bytes(buf)
                except Exception as e:
                    print(f"[seed] write resume failed: {e}", flush=True)
            else:
                msg = a.message()
                low = msg.lower()
                if "error" in low or "fail" in low:
                    print(f"[seed][alert] {type(a).__name__}: {msg}", flush=True)

    def request_save_resume_all(self):
        for h in list(self.handles.values()):
            if h.is_valid() and h.status().has_metadata:
                h.save_resume_data()

    def apply_rate_limits(self, down_kbps: int, up_kbps: int):
        """0 — без ограничений. libtorrent ждёт байты/с."""
        try:
            settings = self.ses.get_settings()
            settings["download_rate_limit"] = down_kbps * 1024 if down_kbps > 0 else 0
            settings["upload_rate_limit"] = up_kbps * 1024 if up_kbps > 0 else 0
            self.ses.apply_settings(settings)
            print(f"[seed] rate limits: ↓{down_kbps}KB/s ↑{up_kbps}KB/s", flush=True)
        except (AttributeError, RuntimeError) as e:
            print(f"[seed] apply_rate_limits failed: {e}", flush=True)

    def shutdown(self):
        self.request_save_resume_all()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.drain_alerts()
            time.sleep(0.2)


class DownloadWorker(QThread):
    progress = pyqtSignal(int, str)
    done = pyqtSignal(str, list, str)   # save_dir, rel_paths, info_hash (остаётся в seed)
    failed = pyqtSignal(str)

    def __init__(self, seed: SeedSession, magnet: str, save_dir: str,
                 torrent_url: str = "", mark_pending_flash: bool = False):
        super().__init__()
        self.seed = seed
        self.magnet = magnet
        self.torrent_url = torrent_url
        self.save_dir = save_dir
        self.mark_pending_flash = mark_pending_flash
        self._cancel = False
        self.info_hash = ""

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            print(f"[DL] start save_dir={self.save_dir}", flush=True)
            self.info_hash = self.seed.add(self.magnet, self.torrent_url, self.save_dir)
            handle = self.seed.handles[self.info_hash]
            if self.mark_pending_flash and self.info_hash in self.seed.library:
                self.seed.library[self.info_hash]["pending_flash_copy"] = True
                self.seed._save_library()
            print(f"[DL] hash={self.info_hash[:8]} pending_flash={self.mark_pending_flash}", flush=True)

            self.progress.emit(0, "Получение метаданных…")
            meta_deadline = time.monotonic() + 180
            while not handle.status().has_metadata:
                if self._cancel:
                    self.seed.remove(self.info_hash, delete_files=True)
                    self.failed.emit("Отменено")
                    return
                if time.monotonic() > meta_deadline:
                    s = handle.status()
                    self.seed.remove(self.info_hash, delete_files=True)
                    self.failed.emit(
                        f"Метаданные не получены за 3 мин (пиров: {s.num_peers})"
                    )
                    return
                s = handle.status()
                self.progress.emit(0, f"Получение метаданных… пиров: {s.num_peers}")
                time.sleep(1)
            self.seed.update_metadata(self.info_hash)

            info = handle.torrent_file()
            total = info.total_size()
            files = info.files()
            rel_paths = [files.file_path(i) for i in range(files.num_files())]

            dl_start = time.monotonic()
            tick = 0
            while True:
                if self._cancel:
                    self.seed.remove(self.info_hash, delete_files=True)
                    self.failed.emit("Отменено")
                    return
                s = handle.status()
                state = DL_STATES[s.state] if 0 <= s.state < len(DL_STATES) else str(s.state)
                pct = int(s.progress * 100)
                elapsed = time.monotonic() - dl_start
                downloaded = s.progress * total
                eta_s = None
                if s.download_rate > 1024 and s.progress < 1.0:
                    eta_s = (total - downloaded) / s.download_rate
                line = (
                    f"{state} · {human_bytes(downloaded)}/{human_bytes(total)} "
                    f"· ↓ {human_bytes(s.download_rate)}/s · пиров: {s.num_peers} "
                    f"· ETA {fmt_time(eta_s)} · прошло {fmt_time(elapsed)}"
                )
                self.progress.emit(pct, line)
                if tick % 5 == 0:
                    print(f"[DL] {line}", flush=True)
                if s.is_seeding or s.progress >= 1.0:
                    break
                time.sleep(1)
                tick += 1

            print("[DL] complete, kept in seed session", flush=True)
            if self.info_hash in self.seed.library:
                self.seed.library[self.info_hash]["completed_at"] = time.time()
                self.seed._save_library()
            self.done.emit(self.save_dir, rel_paths, self.info_hash)
        except Exception as e:
            self.failed.emit(f"Ошибка: {e}")


class CopyWorker(QThread):
    progress = pyqtSignal(int, str)
    done = pyqtSignal(list)  # список сообщений (что разбито, что скопировано целиком)
    failed = pyqtSignal(str)

    def __init__(self, src_dir: str, rel_paths: list[str], dst_dir: str, chunk_size: int):
        super().__init__()
        self.src_dir = src_dir
        self.rel_paths = rel_paths
        self.dst_dir = dst_dir
        self.chunk_size = chunk_size
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            sources = [Path(self.src_dir) / p for p in self.rel_paths]
            sources = [p for p in sources if p.is_file()]
            total_bytes = sum(p.stat().st_size for p in sources)
            if total_bytes == 0:
                self.failed.emit("Нет файлов для копирования")
                return
            self._start = time.monotonic()
            self._total = total_bytes
            copied = 0
            report = []
            has_mkvmerge = shutil.which("mkvmerge") is not None
            for src in sources:
                rel = src.relative_to(self.src_dir)
                dst = Path(self.dst_dir) / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                size = src.stat().st_size
                if size <= self.chunk_size:
                    copied = self._stream_copy(src, dst, copied, total_bytes, f"копирую {rel.name}")
                    report.append(f"✓ {rel} ({human_bytes(size)})")
                elif src.suffix.lower() == ".mkv" and has_mkvmerge:
                    parts = self._mkvmerge_split(src, dst, copied, total_bytes)
                    copied += size
                    report.append(
                        f"M {rel} → {parts} проигрываемых MKV-частей через mkvmerge"
                    )
                else:
                    parts = self._split_copy(src, dst, copied, total_bytes)
                    copied += size
                    report.append(f"✂ {rel} → {parts} частей по ≤ {human_bytes(self.chunk_size)}")
                if self._cancel:
                    self.failed.emit("Отменено")
                    return
            self.done.emit(report)
        except OSError as e:
            self.failed.emit(f"Ошибка ввода-вывода: {e}")

    def _stream_copy(self, src: Path, dst: Path, copied: int, total: int, label: str) -> int:
        buf_size = 4 * 1024 * 1024
        with open(src, "rb") as fin, open(dst, "wb") as fout:
            while True:
                if self._cancel:
                    return copied
                buf = fin.read(buf_size)
                if not buf:
                    break
                fout.write(buf)
                copied += len(buf)
                self.progress.emit(int(copied * 100 / total), self._stat_line(copied, label))
        return copied

    def _stat_line(self, copied: int, label: str) -> str:
        elapsed = time.monotonic() - self._start
        rate = copied / elapsed if elapsed > 0.2 else 0
        eta = (self._total - copied) / rate if rate > 1024 else None
        return (
            f"{label} · ↑ {human_bytes(rate)}/s "
            f"· ETA {fmt_time(eta)} · прошло {fmt_time(elapsed)}"
        )

    def _mkvmerge_split(self, src: Path, dst: Path, copied: int, total: int) -> int:
        """Режет MKV по keyframe'ам через mkvmerge — каждая часть валидный MKV.

        Имена частей: name-001.mkv, name-002.mkv, ..."""
        chunk_mb = max(64, self.chunk_size // (1024 * 1024))
        cmd = [
            "mkvmerge",
            "--gui-mode",
            "-o", str(dst),
            "--split", f"size:{chunk_mb}M",
            str(src),
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        src_size = src.stat().st_size
        for line in proc.stdout or []:
            if self._cancel:
                proc.terminate()
                break
            m = re.search(r"#GUI#progress\s+(\d+)%", line) or re.search(r"Progress:\s*(\d+)%", line)
            if m:
                local_pct = int(m.group(1))
                global_done = copied + (local_pct / 100.0) * src_size
                global_pct = int(global_done * 100 / total) if total else 0
                self.progress.emit(
                    global_pct,
                    self._stat_line(int(global_done), f"mkvmerge {src.name} {local_pct}%"),
                )
        proc.wait()
        if proc.returncode not in (0, 1):  # 1 = warnings, still produces output
            raise OSError(f"mkvmerge exit {proc.returncode}")
        # Подсчёт получившихся частей
        produced = sorted(dst.parent.glob(f"{dst.stem}-*{dst.suffix}"))
        return len(produced)

    def _split_copy(self, src: Path, dst: Path, copied: int, total: int) -> int:
        buf_size = 4 * 1024 * 1024
        part_idx = 0
        # Расширение сохраняется в конце: name.part001.mkv (а не name.mkv.part001)
        stem, ext = dst.stem, dst.suffix
        with open(src, "rb") as fin:
            while True:
                if self._cancel:
                    return part_idx
                part_name = f"{stem}.part{part_idx:03d}{ext}"
                part_path = dst.with_name(part_name)
                written = 0
                with open(part_path, "wb") as fout:
                    while written < self.chunk_size:
                        if self._cancel:
                            return part_idx
                        to_read = min(buf_size, self.chunk_size - written)
                        buf = fin.read(to_read)
                        if not buf:
                            break
                        fout.write(buf)
                        written += len(buf)
                        copied_now = copied + part_idx * self.chunk_size + written
                        self.progress.emit(
                            int(copied_now * 100 / total),
                            self._stat_line(
                                copied_now,
                                f"режу {dst.name} · часть {part_idx + 1}",
                            ),
                        )
                if written == 0:
                    part_path.unlink(missing_ok=True)
                    break
                part_idx += 1
                if written < self.chunk_size:
                    break
        return part_idx

def _version_tuple(v: str) -> tuple:
    parts = []
    for x in v.lstrip("v").split("."):
        try:
            parts.append(int(x))
        except ValueError:
            break
    return tuple(parts)


class UpdateChecker(QThread):
    found = pyqtSignal(str, str, str)   # version, asset_url, asset_name
    up_to_date = pyqtSignal(str)        # current_version
    failed = pyqtSignal(str)

    def run(self):
        try:
            r = requests.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                headers={"Accept": "application/vnd.github+json", "User-Agent": APP_NAME},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            tag = data.get("tag_name", "").lstrip("v")
            if not tag:
                self.failed.emit("Релиз без tag_name")
                return
            if _version_tuple(tag) <= _version_tuple(APP_VERSION):
                self.up_to_date.emit(APP_VERSION)
                return
            for asset in data.get("assets", []):
                name = asset.get("name", "")
                if name.startswith("TorFlash") and not name.endswith((".asc", ".sig", ".sha256")):
                    self.found.emit(tag, asset["browser_download_url"], name)
                    return
            self.failed.emit("Не найден бинарный asset в релизе")
        except requests.RequestException as e:
            self.failed.emit(f"Сеть: {e}")
        except (ValueError, KeyError) as e:
            self.failed.emit(f"Ответ GitHub: {e}")


class UpdateDownloader(QThread):
    progress = pyqtSignal(int, str)
    done = pyqtSignal(str)              # путь к новому бинарнику (.new)
    failed = pyqtSignal(str)

    def __init__(self, url: str, target_dir: str):
        super().__init__()
        self.url = url
        self.target_dir = target_dir

    def run(self):
        try:
            target = Path(self.target_dir) / "TorFlash.new"
            r = requests.get(self.url, stream=True, timeout=60)
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            written = 0
            with open(target, "wb") as f:
                for chunk in r.iter_content(chunk_size=128 * 1024):
                    if chunk:
                        f.write(chunk)
                        written += len(chunk)
                        pct = int(written * 100 / total) if total else 0
                        self.progress.emit(
                            pct,
                            f"Загрузка обновления: {human_bytes(written)}"
                            + (f"/{human_bytes(total)}" if total else ""),
                        )
            target.chmod(0o755)
            self.done.emit(str(target))
        except requests.RequestException as e:
            self.failed.emit(f"Сеть: {e}")
        except OSError as e:
            self.failed.emit(f"Запись на диск: {e}")


class SearchWorker(QThread):
    done = pyqtSignal(list, str)
    failed = pyqtSignal(str)

    def __init__(self, query: str, category: int = 0):
        super().__init__()
        self.query = query
        self.category = category

    def run(self):
        last_err = ""
        for base in MIRRORS:
            if self.category:
                url = f"{base}/search/0/{self.category}/000/0/{quote(self.query)}"
            else:
                url = f"{base}/search/{quote(self.query)}"
            try:
                r = requests.get(url, headers=HEADERS, timeout=10)
                r.raise_for_status()
                results = parse(r.text, base)
                self.done.emit(results, base)
                return
            except requests.RequestException as e:
                last_err = f"{base}: {e}"
        self.failed.emit(last_err or "Все зеркала недоступны")


MAGNET_HASH_RE = re.compile(r"btih:([a-f0-9]+)", re.I)


class MetaFetcher(QThread):
    """Фоновое получение детальной инфы о торренте с rutor.info."""

    fetched = pyqtSignal(str, dict)  # url, details

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        try:
            from rutor_meta import fetch_torrent_details
        except ImportError as e:
            print(f"[meta] rutor_meta module missing: {e}", flush=True)
            return
        try:
            data = fetch_torrent_details(self.url)
            self.fetched.emit(self.url, data)
        except Exception as e:
            print(f"[meta] fetch failed: {e}", flush=True)


class PosterFetcher(QThread):
    """Фоновая загрузка картинки постера. Поддерживает Referer (для хотлинк-сайтов)."""

    loaded = pyqtSignal(str, bytes)  # url, image_bytes

    def __init__(self, url: str, referer: str = ""):
        super().__init__()
        self.url = url
        self.referer = referer

    def run(self):
        headers = dict(HEADERS)
        if self.referer:
            headers["Referer"] = self.referer
        last_err = None
        for attempt in range(2):
            try:
                r = requests.get(self.url, headers=headers, timeout=10)
                r.raise_for_status()
                if len(r.content) > 0:
                    self.loaded.emit(self.url, r.content)
                return
            except (requests.RequestException, OSError) as e:
                last_err = e
                time.sleep(0.5)
        msg = str(last_err) if last_err else "unknown"
        # Не шумим про типичные «мёртвые/враждебные» хостинги:
        # DNS-фейлы, обрывы соединения, 403/404 от хотлинк-щитов.
        if any(s in msg for s in (
            "Name or service not known",
            "NameResolutionError",
            "Temporary failure in name resolution",
            "nodename nor servname",
            "RemoteDisconnected",
            "Connection aborted",
            "Connection reset",
            "404 Client Error",
            "403 Client Error",
        )):
            return
        print(f"[poster] fetch failed after retry: {msg}", flush=True)


def themed_icon(name: str, style=None, fallback=None) -> QIcon:
    """Сначала пробуем иконку из системной темы (Breeze, Adwaita, …),
    затем — стандартную из Qt-стиля. Возвращаем пустую QIcon в крайнем случае."""
    icon = QIcon.fromTheme(name)
    if not icon.isNull() and icon.availableSizes():
        return icon
    if style is not None and fallback is not None:
        return style.standardIcon(fallback)
    return QIcon()


class SettingsDialog(QDialog):
    applied = pyqtSignal()

    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} — настройки")
        self.settings = settings
        v = QVBoxLayout(self)
        v.setSpacing(10)

        # Поведение окна
        self.cb_minimize = QCheckBox("Сворачивать в трей при закрытии окна")
        self.cb_minimize.setChecked(
            settings.value("minimize_on_close", True, type=bool)
        )
        v.addWidget(self.cb_minimize)
        self.cb_autostart = QCheckBox("Запускать при входе в систему")
        self.cb_autostart.setChecked(AUTOSTART_FILE.exists())
        v.addWidget(self.cb_autostart)
        self.cb_hidden = QCheckBox("Скрытый старт (только иконка в трее)")
        self.cb_hidden.setChecked(
            settings.value("start_hidden", False, type=bool)
        )
        v.addWidget(self.cb_hidden)
        self.cb_auto_update = QCheckBox("Автоматически проверять обновления (раз в сутки)")
        self.cb_auto_update.setChecked(
            settings.value("auto_check_updates", True, type=bool)
        )
        v.addWidget(self.cb_auto_update)

        # Тема
        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("Тема:"))
        self.cb_theme = QComboBox()
        for label, val in (("Системная", "auto"), ("Светлая", "light"), ("Тёмная", "dark")):
            self.cb_theme.addItem(label, val)
        current_theme = settings.value("theme", "auto", type=str)
        for i in range(self.cb_theme.count()):
            if self.cb_theme.itemData(i) == current_theme:
                self.cb_theme.setCurrentIndex(i)
                break
        theme_row.addWidget(self.cb_theme, 1)
        v.addLayout(theme_row)

        # Лимиты скорости
        v.addWidget(QLabel("<b>Лимиты скорости</b> (КБ/с, 0 — без ограничений):"))
        rate_form = QFormLayout()
        rate_form.setHorizontalSpacing(10)
        self.sp_down = QSpinBox()
        self.sp_down.setRange(0, 1_000_000)
        self.sp_down.setSuffix(" КБ/с")
        self.sp_down.setValue(settings.value("rate_limit_down", 0, type=int))
        self.sp_up = QSpinBox()
        self.sp_up.setRange(0, 1_000_000)
        self.sp_up.setSuffix(" КБ/с")
        self.sp_up.setValue(settings.value("rate_limit_up", 0, type=int))
        rate_form.addRow("Скачивание ↓:", self.sp_down)
        rate_form.addRow("Раздача ↑:", self.sp_up)
        v.addLayout(rate_form)

        info = QLabel(
            "Скачивание всегда идёт в <b>~/Storage</b>. Файлы хранятся там до "
            "ручного удаления из вкладки «Моя раздача»."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #888; padding-top: 6px;")
        v.addWidget(info)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)

    def _apply(self):
        self.settings.setValue("minimize_on_close", self.cb_minimize.isChecked())
        self.settings.setValue("start_hidden", self.cb_hidden.isChecked())
        self.settings.setValue("auto_check_updates", self.cb_auto_update.isChecked())
        self.settings.setValue("theme", self.cb_theme.currentData())
        self.settings.setValue("rate_limit_down", int(self.sp_down.value()))
        self.settings.setValue("rate_limit_up", int(self.sp_up.value()))
        self._apply_autostart(self.cb_autostart.isChecked())
        self.applied.emit()
        self.accept()

    def _apply_autostart(self, enabled: bool):
        if enabled:
            AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
            exe = sys.executable if getattr(sys, "frozen", False) else f"/usr/bin/python3 {Path(__file__).resolve()}"
            icon = Path(__file__).resolve().parent / "torflash.svg"
            content = (
                "[Desktop Entry]\n"
                "Type=Application\n"
                f"Name={APP_NAME}\n"
                f"Exec={exe} --hidden\n"
                f"Icon={icon}\n"
                "Terminal=false\n"
                "X-GNOME-Autostart-enabled=true\n"
                f"X-KDE-autostart-after=panel\n"
            )
            AUTOSTART_FILE.write_text(content)
            AUTOSTART_FILE.chmod(0o755)
        else:
            AUTOSTART_FILE.unlink(missing_ok=True)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} — торренты rutor → флешка")
        self.resize(1300, 760)
        icon_path = Path(__file__).parent / "torflash.svg"
        if icon_path.exists():
            from PyQt5.QtGui import QIcon
            self.setWindowIcon(QIcon(str(icon_path)))

        # состояние
        self.results: list[dict] = []
        self.search_worker: SearchWorker | None = None
        self.dl_worker: DownloadWorker | None = None
        self.copy_worker: CopyWorker | None = None
        self.dl_result: dict | None = None
        self.dl_phase: str = ""           # "dl" | "copy"
        self.dl_progress: tuple = (0, "")
        flash = detect_flash_mount()
        if flash:
            self.dst_dir: str = str(Path(flash) / "Movies")
            self._initial_use_flash = True
        else:
            self.dst_dir = str(Path.home() / "Storage")
            self._initial_use_flash = False
        self.settings = QSettings("TorFlash", "TorFlash")
        self.seed = SeedSession()
        self._build_ui()
        self._apply_style()
        self._build_tray()
        # Тикер для обновления вкладки «Моя раздача»
        from PyQt5.QtCore import QTimer
        self._lib_timer = QTimer(self)
        self._lib_timer.setInterval(2000)
        self._lib_timer.timeout.connect(self._refresh_library)
        self._lib_timer.start()
        # Тикер для drain_alerts/resume save
        self._alerts_timer = QTimer(self)
        self._alerts_timer.setInterval(1500)
        self._alerts_timer.timeout.connect(self.seed.drain_alerts)
        self._alerts_timer.start()
        # Периодически просим libtorrent сохранить resume_data
        self._resume_timer = QTimer(self)
        self._resume_timer.setInterval(60_000)
        self._resume_timer.timeout.connect(self.seed.request_save_resume_all)
        self._resume_timer.start()
        self._flash_timer = QTimer(self)
        self._flash_timer.setInterval(5000)
        self._flash_timer.timeout.connect(self._refresh_flash_info)
        self._flash_timer.start()
        # Авто-проверка обновлений раз в сутки
        self._auto_update_timer = QTimer(self)
        self._auto_update_timer.setInterval(24 * 60 * 60 * 1000)
        self._auto_update_timer.timeout.connect(self._maybe_check_updates)
        self._auto_update_timer.start()
        # Применяем сохранённые настройки скорости и темы
        self._apply_settings()
        self._refresh_library()

    # ---------- UI building ----------

    def _build_ui(self):
        from PyQt5.QtWidgets import QTabWidget
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 6)
        root.setSpacing(8)

        tabs = QTabWidget()
        tabs.addTab(self._build_search_tab(), "Поиск")
        tabs.addTab(self._build_library_tab(), "Моя раздача")
        tabs.addTab(self._build_flash_tab(), "Флешка")
        tabs.currentChanged.connect(self._on_tab_changed)
        root.addWidget(tabs, 1)
        self.tabs = tabs

        self.setStatusBar(QStatusBar())

    def _build_search_tab(self) -> QWidget:
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 6, 0, 0)
        v.setSpacing(8)

        # Поисковая строка с категорией
        search_row = QHBoxLayout()
        self.category_combo = QComboBox()
        for cid, name in RUTOR_CATEGORIES:
            self.category_combo.addItem(name, cid)
        last_cat = self.settings.value("last_category", 0, type=int)
        for i, (cid, _) in enumerate(RUTOR_CATEGORIES):
            if cid == last_cat:
                self.category_combo.setCurrentIndex(i)
                break
        search_row.addWidget(self.category_combo)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Название фильма, игры, дистрибутива…")
        self.input.returnPressed.connect(self.start_search)
        # История запросов: QCompleter
        history = self.settings.value("search_history", [], type=list) or []
        self._search_history = list(history)
        self.search_completer = QCompleter(self._search_history)
        self.search_completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.input.setCompleter(self.search_completer)
        search_row.addWidget(self.input, 1)
        self.search_btn = QPushButton("Искать")
        self.search_btn.setDefault(True)
        self.search_btn.clicked.connect(self.start_search)
        search_row.addWidget(self.search_btn)
        v.addLayout(search_row)

        # Папка назначения (только если включена флешка) + опции
        dst_row = QHBoxLayout()
        self.flash_check = QCheckBox("Дублировать на флешку (Movies)")
        self.flash_check.setChecked(self._initial_use_flash)
        self.flash_check.setToolTip(
            "Загрузка всегда идёт в ~/Storage. "
            "При включении дополнительно копируем на флешку в /Movies с разбиением для FAT32."
        )
        self.flash_check.toggled.connect(self._on_flash_toggle)
        dst_row.addWidget(self.flash_check)
        self.dst_edit = QLineEdit(self.dst_dir)
        self.dst_edit.setReadOnly(True)
        self.dst_edit.setToolTip("Папка, куда дополнительно копируем (флешка)")
        dst_row.addWidget(self.dst_edit, 1)
        self.dst_btn = QToolButton()
        self.dst_btn.setText("…")
        self.dst_btn.setToolTip("Выбрать папку вручную")
        self.dst_btn.clicked.connect(self.choose_destination)
        dst_row.addWidget(self.dst_btn)
        self.flash_redetect = QToolButton()
        self.flash_redetect.setText("⟳")
        self.flash_redetect.setToolTip("Найти флешку заново")
        self.flash_redetect.clicked.connect(self.redetect_flash)
        dst_row.addWidget(self.flash_redetect)
        self.eject_btn = QToolButton()
        self.eject_btn.setText("⏏")
        self.eject_btn.setToolTip("Безопасно извлечь флешку")
        self.eject_btn.clicked.connect(self.eject_flash)
        dst_row.addWidget(self.eject_btn)
        v.addLayout(dst_row)

        # Сплиттер: список ← → детали
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_list())
        splitter.addWidget(self._build_detail())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([720, 520])
        v.addWidget(splitter, 1)
        return wrap

    def _build_library_tab(self) -> QWidget:
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(8)
        info = QLabel(
            f"Все скачанные торренты лежат в <b>{STORAGE_DEFAULT}</b> и раздаются, "
            "пока приложение открыто. Файлы не удаляются автоматически."
        )
        info.setStyleSheet("color: #888;")
        info.setWordWrap(True)
        v.addWidget(info)

        # Прогресс активного авто-копирования на флешку
        self.lib_copy_box = QFrame()
        self.lib_copy_box.setObjectName("progressBox")
        self.lib_copy_box.setVisible(False)
        cb = QVBoxLayout(self.lib_copy_box)
        cb.setContentsMargins(12, 8, 12, 8)
        cb.setSpacing(4)
        self.lib_copy_phase = QLabel("")
        self.lib_copy_phase.setStyleSheet("font-weight: 600;")
        cb.addWidget(self.lib_copy_phase)
        self.lib_copy_bar = QProgressBar()
        self.lib_copy_bar.setMinimum(0)
        self.lib_copy_bar.setMaximum(100)
        self.lib_copy_bar.setProperty("phase", "copy")
        cb.addWidget(self.lib_copy_bar)
        bottom = QHBoxLayout()
        self.lib_copy_status = QLabel("")
        self.lib_copy_status.setStyleSheet("color: #888;")
        self.lib_copy_status.setWordWrap(True)
        bottom.addWidget(self.lib_copy_status, 1)
        self.lib_copy_cancel = QPushButton("Отмена")
        self.lib_copy_cancel.setIcon(themed_icon("process-stop", self.style(), QStyle.SP_DialogCancelButton))
        self.lib_copy_cancel.clicked.connect(self._cancel_pending_copy)
        bottom.addWidget(self.lib_copy_cancel)
        cb.addLayout(bottom)
        v.addWidget(self.lib_copy_box)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_library_list())
        splitter.addWidget(self._build_library_detail())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([720, 520])
        v.addWidget(splitter, 1)
        return wrap

    def _build_library_list(self) -> QWidget:
        self.lib_table = QTableWidget(0, 6)
        self.lib_table.setHorizontalHeaderLabels(
            ["Название", "Размер", "Прогресс", "↓", "↑", "Пиров"]
        )
        self.lib_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.lib_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.lib_table.setSelectionMode(QTableWidget.SingleSelection)
        self.lib_table.setAlternatingRowColors(True)
        self.lib_table.verticalHeader().setVisible(False)
        lh = self.lib_table.horizontalHeader()
        lh.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, 6):
            lh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.lib_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.lib_table.customContextMenuRequested.connect(self._lib_context_menu)
        self.lib_table.itemSelectionChanged.connect(self._on_lib_selection_changed)
        return self.lib_table

    def _build_flash_tab(self) -> QWidget:
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(8)
        self.flash_summary = QLabel("Флешка не подключена")
        self.flash_summary.setObjectName("flashInfo")
        self.flash_summary.setProperty("state", "off")
        v.addWidget(self.flash_summary)

        actions = QHBoxLayout()
        self.flash_refresh_btn = QPushButton("Обновить")
        self.flash_refresh_btn.setIcon(
            themed_icon("view-refresh", self.style(), QStyle.SP_BrowserReload)
        )
        self.flash_refresh_btn.clicked.connect(self._refresh_flash_tab)
        actions.addWidget(self.flash_refresh_btn)
        self.flash_open_btn = QPushButton("Открыть папку")
        self.flash_open_btn.setIcon(
            themed_icon("folder-open", self.style(), QStyle.SP_DirOpenIcon)
        )
        self.flash_open_btn.clicked.connect(self._open_flash_folder)
        actions.addWidget(self.flash_open_btn)
        self.flash_eject_btn = QPushButton("Безопасно извлечь")
        self.flash_eject_btn.setIcon(
            themed_icon("media-eject", self.style(), QStyle.SP_DialogCancelButton)
        )
        self.flash_eject_btn.clicked.connect(self.eject_flash)
        actions.addWidget(self.flash_eject_btn)
        actions.addStretch()
        v.addLayout(actions)

        self.flash_files_table = QTableWidget(0, 3)
        self.flash_files_table.setHorizontalHeaderLabels(["Файл", "Размер", "Изменён"])
        self.flash_files_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.flash_files_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.flash_files_table.setAlternatingRowColors(True)
        self.flash_files_table.verticalHeader().setVisible(False)
        fh = self.flash_files_table.horizontalHeader()
        fh.setSectionResizeMode(0, QHeaderView.Stretch)
        fh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        fh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.flash_files_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.flash_files_table.customContextMenuRequested.connect(self._flash_file_menu)
        v.addWidget(self.flash_files_table, 1)

        return wrap

    def _on_tab_changed(self, index: int):
        if index == 2:
            self._refresh_flash_tab()

    def _refresh_flash_tab(self):
        mount = detect_flash_mount()
        if not mount:
            self.flash_summary.setText("Флешка не подключена")
            self.flash_summary.setProperty("state", "off")
            self.flash_summary.style().unpolish(self.flash_summary)
            self.flash_summary.style().polish(self.flash_summary)
            self.flash_files_table.setRowCount(0)
            return
        try:
            usage = shutil.disk_usage(mount)
            fs = ""
            try:
                fs = subprocess.run(
                    ["findmnt", "-no", "FSTYPE", mount],
                    capture_output=True, text=True, check=True, timeout=2,
                ).stdout.strip()
            except (subprocess.SubprocessError, OSError):
                pass
            self.flash_summary.setText(
                f"<b>{Path(mount).name}</b> ({mount})"
                + (f" · {fs}" if fs else "")
                + f" · свободно <b>{human_bytes(usage.free)}</b> из {human_bytes(usage.total)}"
            )
            free_ratio = usage.free / usage.total if usage.total else 1
            self.flash_summary.setProperty(
                "state", "warn" if free_ratio < 0.1 else "ok"
            )
            self.flash_summary.style().unpolish(self.flash_summary)
            self.flash_summary.style().polish(self.flash_summary)
        except OSError as e:
            self.flash_summary.setText(f"Ошибка: {e}")
            return
        # Перечислим содержимое /Movies (если есть) или корня
        target = Path(mount) / "Movies"
        if not target.exists():
            target = Path(mount)
        rows = []
        try:
            for p in sorted(target.rglob("*")):
                if p.is_file():
                    try:
                        st = p.stat()
                        rows.append((p.relative_to(mount), st.st_size, st.st_mtime))
                    except OSError:
                        continue
        except OSError as e:
            self.flash_summary.setText(f"Ошибка чтения: {e}")
            return
        self.flash_files_table.setRowCount(len(rows))
        for i, (rel, size, mtime) in enumerate(rows):
            it_name = QTableWidgetItem(str(rel))
            it_size = QTableWidgetItem(human_bytes(size))
            it_size.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            it_mtime = QTableWidgetItem(time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)))
            for col, it in enumerate((it_name, it_size, it_mtime)):
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                self.flash_files_table.setItem(i, col, it)

    def _open_flash_folder(self):
        mount = detect_flash_mount()
        if mount:
            try:
                subprocess.Popen(["xdg-open", mount])
            except OSError as e:
                self.statusBar().showMessage(f"Ошибка: {e}", 3000)

    def _flash_file_menu(self, pos):
        item = self.flash_files_table.itemAt(pos)
        if not item:
            return
        row = item.row()
        name_item = self.flash_files_table.item(row, 0)
        if not name_item:
            return
        mount = detect_flash_mount()
        if not mount:
            return
        full = Path(mount) / name_item.text()
        menu = QMenu(self)
        act_open = menu.addAction("Открыть")
        act_open.triggered.connect(
            lambda: subprocess.Popen(["xdg-open", str(full)])
        )
        menu.addSeparator()
        act_del = menu.addAction("Удалить с флешки")
        act_del.triggered.connect(lambda: self._flash_delete_file(full))
        menu.exec_(self.flash_files_table.viewport().mapToGlobal(pos))

    def _flash_delete_file(self, path: Path):
        try:
            path.unlink(missing_ok=True)
            self._refresh_flash_tab()
            self.statusBar().showMessage(f"Удалено: {path.name}", 3000)
        except OSError as e:
            self.statusBar().showMessage(f"Ошибка удаления: {e}", 4000)

    def _build_library_detail(self) -> QWidget:
        outer = QScrollArea()
        outer.setWidgetResizable(True)
        outer.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        v = QVBoxLayout(inner)
        v.setContentsMargins(16, 8, 16, 16)
        v.setSpacing(10)

        self.lib_empty = QLabel("Выберите торрент в списке слева")
        self.lib_empty.setObjectName("emptyHint")
        self.lib_empty.setAlignment(Qt.AlignCenter)
        v.addWidget(self.lib_empty)

        self.lib_detail_card = QWidget()
        self.lib_detail_card.setVisible(False)
        card = QVBoxLayout(self.lib_detail_card)
        card.setContentsMargins(0, 0, 0, 0)
        card.setSpacing(10)

        self.lib_title = QLabel()
        self.lib_title.setWordWrap(True)
        f = QFont(); f.setPointSize(13); f.setBold(True)
        self.lib_title.setFont(f)
        card.addWidget(self.lib_title)

        meta_box = QFrame()
        meta_box.setObjectName("metaBox")
        meta = QFormLayout(meta_box)
        meta.setLabelAlignment(Qt.AlignRight)
        meta.setContentsMargins(12, 12, 12, 12)
        meta.setHorizontalSpacing(14)
        meta.setVerticalSpacing(6)
        self.lib_status_val = QLabel()
        self.lib_size_val = QLabel()
        self.lib_downloaded_val = QLabel()
        self.lib_rates_val = QLabel()
        self.lib_peers_val = QLabel()
        self.lib_path_val = QLabel()
        self.lib_path_val.setWordWrap(True)
        self.lib_path_val.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lib_ratio_val = QLabel()
        self.lib_pending_val = QLabel()
        self.lib_media_val = QLabel()
        self.lib_media_val.setStyleSheet("color: #888;")
        self.lib_media_val.setWordWrap(True)
        meta.addRow("Статус:", self.lib_status_val)
        meta.addRow("Размер:", self.lib_size_val)
        meta.addRow("Скачано:", self.lib_downloaded_val)
        meta.addRow("Скорости:", self.lib_rates_val)
        meta.addRow("Пиры:", self.lib_peers_val)
        meta.addRow("Папка:", self.lib_path_val)
        meta.addRow("Отдано:", self.lib_ratio_val)
        meta.addRow("На флешку:", self.lib_pending_val)
        meta.addRow("Медиа:", self.lib_media_val)
        card.addWidget(meta_box)

        self.lib_progress_bar = QProgressBar()
        self.lib_progress_bar.setMinimum(0)
        self.lib_progress_bar.setMaximum(100)
        card.addWidget(self.lib_progress_bar)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        style = self.style()
        self.lib_pause_btn = QPushButton("Пауза")
        self.lib_pause_btn.setIcon(themed_icon("media-playback-pause", style, QStyle.SP_MediaPause))
        self.lib_pause_btn.clicked.connect(self._lib_pause_toggle)
        actions.addWidget(self.lib_pause_btn)
        self.lib_recheck_btn = QPushButton("Проверить")
        self.lib_recheck_btn.setIcon(themed_icon("view-refresh", style, QStyle.SP_BrowserReload))
        self.lib_recheck_btn.setToolTip("Принудительная проверка пиров на диске")
        self.lib_recheck_btn.clicked.connect(self._lib_force_recheck)
        actions.addWidget(self.lib_recheck_btn)
        self.lib_open_btn = QPushButton("Папка")
        self.lib_open_btn.setIcon(themed_icon("folder-open", style, QStyle.SP_DirOpenIcon))
        self.lib_open_btn.clicked.connect(self._lib_open_current_folder)
        actions.addWidget(self.lib_open_btn)
        self.lib_flash_btn_panel = QPushButton("На флешку")
        self.lib_flash_btn_panel.setIcon(themed_icon("drive-removable-media-usb", style, QStyle.SP_DriveHDIcon))
        self.lib_flash_btn_panel.setToolTip("Запланировать копирование на флешку (произойдёт при появлении флешки)")
        self.lib_flash_btn_panel.clicked.connect(self._lib_queue_flash)
        actions.addWidget(self.lib_flash_btn_panel)
        actions.addStretch()
        self.lib_remove_btn = QPushButton("Удалить")
        self.lib_remove_btn.setIcon(themed_icon("list-remove", style, QStyle.SP_TrashIcon))
        self.lib_remove_btn.setToolTip("Убрать из раздачи (файлы оставить)")
        self.lib_remove_btn.clicked.connect(self._lib_remove_current_keep)
        actions.addWidget(self.lib_remove_btn)
        self.lib_delete_btn = QPushButton("Удалить + файлы")
        self.lib_delete_btn.setIcon(themed_icon("edit-delete", style, QStyle.SP_DialogDiscardButton))
        self.lib_delete_btn.clicked.connect(self._lib_remove_current_delete)
        actions.addWidget(self.lib_delete_btn)
        card.addLayout(actions)
        card.addStretch()

        v.addWidget(self.lib_detail_card)
        v.addStretch()
        outer.setWidget(inner)
        return outer

    def _build_list(self) -> QWidget:
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Дата", "Название", "Размер", "S", "L"])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        for i in (0, 2, 3, 4):
            h.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.doubleClicked.connect(self.download_to_flash)
        return self.table

    def _build_detail(self) -> QWidget:
        outer = QScrollArea()
        outer.setWidgetResizable(True)
        outer.setFrameShape(QFrame.NoFrame)

        inner = QWidget()
        inner.setObjectName("detailPane")
        v = QVBoxLayout(inner)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(10)

        # Заглушка
        self.empty_label = QLabel("Выберите торрент из списка слева")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setObjectName("emptyHint")
        v.addWidget(self.empty_label)

        # Карточка с деталями
        self.detail_card = QWidget()
        self.detail_card.setVisible(False)
        card_v = QVBoxLayout(self.detail_card)
        card_v.setContentsMargins(0, 0, 0, 0)
        card_v.setSpacing(10)

        self.title_label = QLabel()
        self.title_label.setWordWrap(True)
        self.title_label.setObjectName("titleLabel")
        title_font = QFont()
        title_font.setPointSize(13)
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        card_v.addWidget(self.title_label)

        meta_box = QFrame()
        meta_box.setObjectName("metaBox")
        meta = QFormLayout(meta_box)
        meta.setLabelAlignment(Qt.AlignRight)
        meta.setContentsMargins(12, 12, 12, 12)
        meta.setHorizontalSpacing(14)
        meta.setVerticalSpacing(6)
        self.date_val = QLabel("")
        self.size_val = QLabel("")
        self.seeds_val = QLabel("")
        self.leech_val = QLabel("")
        self.hash_val = QLabel("")
        self.hash_val.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.hash_val.setStyleSheet("font-family: monospace; font-size: 11px;")
        for w in (self.date_val, self.size_val, self.seeds_val, self.leech_val):
            w.setStyleSheet("font-weight: 500;")
        meta.addRow("Дата:", self.date_val)
        meta.addRow("Размер:", self.size_val)
        meta.addRow("Сиды:", self.seeds_val)
        meta.addRow("Личеры:", self.leech_val)
        meta.addRow("Hash:", self.hash_val)
        card_v.addWidget(meta_box)

        # Постер + описание (подгружается асинхронно после выбора)
        self.poster_label = QLabel()
        self.poster_label.setAlignment(Qt.AlignCenter)
        self.poster_label.setMinimumHeight(0)
        self.poster_label.setVisible(False)
        card_v.addWidget(self.poster_label)
        self.description_view = QTextBrowser()
        self.description_view.setOpenExternalLinks(True)
        self.description_view.setMaximumHeight(200)
        self.description_view.setVisible(False)
        self.description_view.setStyleSheet(
            "QTextBrowser { background: rgba(127,127,127,0.05); border: none; padding: 8px; }"
        )
        card_v.addWidget(self.description_view)

        # Информация о флешке + помещается ли торрент
        self.flash_info = QLabel("")
        self.flash_info.setObjectName("flashInfo")
        self.flash_info.setWordWrap(True)
        card_v.addWidget(self.flash_info)

        # Действия
        actions = QHBoxLayout()
        actions.setSpacing(6)
        style = self.style()
        self.copy_btn = QPushButton("Magnet")
        self.copy_btn.setIcon(themed_icon("edit-copy", style, QStyle.SP_DialogSaveButton))
        self.copy_btn.setToolTip("Скопировать magnet-ссылку в буфер обмена")
        self.copy_btn.clicked.connect(self.copy_magnet)
        actions.addWidget(self.copy_btn)
        self.ktorrent_btn = QPushButton("KTorrent")
        self.ktorrent_btn.setIcon(themed_icon("ktorrent", style, QStyle.SP_MediaPlay))
        self.ktorrent_btn.clicked.connect(self.open_in_ktorrent)
        actions.addWidget(self.ktorrent_btn)
        self.page_btn = QPushButton("Страница")
        self.page_btn.setIcon(themed_icon("internet-web-browser", style, QStyle.SP_DirLinkIcon))
        self.page_btn.clicked.connect(self.open_page)
        actions.addWidget(self.page_btn)
        actions.addStretch()
        self.flash_btn = QPushButton("Скачать → на флешку")
        self.flash_btn.setObjectName("primaryBtn")
        self.flash_btn.setIcon(themed_icon("drive-removable-media-usb", style, QStyle.SP_DriveHDIcon))
        self.flash_btn.clicked.connect(self.download_to_flash)
        actions.addWidget(self.flash_btn)
        card_v.addLayout(actions)

        # Inline-баннер (ошибки/важные сообщения)
        self.banner = QLabel("")
        self.banner.setVisible(False)
        self.banner.setWordWrap(True)
        self.banner.setObjectName("banner")
        card_v.addWidget(self.banner)

        # Прогресс-секция
        self.progress_box = QFrame()
        self.progress_box.setObjectName("progressBox")
        self.progress_box.setVisible(False)
        pv = QVBoxLayout(self.progress_box)
        pv.setContentsMargins(12, 10, 12, 10)
        pv.setSpacing(6)
        self.progress_phase = QLabel("")
        self.progress_phase.setStyleSheet("font-weight: 600;")
        pv.addWidget(self.progress_phase)
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        pv.addWidget(self.progress_bar)
        self.progress_status = QLabel("")
        self.progress_status.setWordWrap(True)
        self.progress_status.setStyleSheet("color: #888;")
        pv.addWidget(self.progress_status)
        cancel_row = QHBoxLayout()
        cancel_row.addStretch()
        self.cancel_btn = QPushButton("Отмена")
        self.cancel_btn.setIcon(themed_icon("process-stop", style, QStyle.SP_DialogCancelButton))
        self.cancel_btn.clicked.connect(self._on_cancel)
        cancel_row.addWidget(self.cancel_btn)
        pv.addLayout(cancel_row)
        card_v.addWidget(self.progress_box)

        card_v.addStretch()
        v.addWidget(self.detail_card)
        v.addStretch()

        outer.setWidget(inner)
        return outer

    def _build_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            return
        # Для трея — отдельная упрощённая иконка (без анимаций и мелких деталей)
        base = Path(__file__).resolve().parent
        if not base.exists():
            base = Path(getattr(sys, "_MEIPASS", "."))
        tray_path = base / "torflash-tray.svg"
        icon = QIcon(str(tray_path)) if tray_path.exists() else self.windowIcon()
        # Добавим PNG-варианты разных размеров — KDE предпочитает их при выборе
        for size in (22, 32, 48):
            png = base / f"torflash-tray-{size}.png"
            if png.exists():
                icon.addFile(str(png))

        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip(APP_NAME)

        menu = QMenu(self)
        self.act_show = QAction("Показать", self)
        self.act_show.triggered.connect(self._tray_show)
        menu.addAction(self.act_show)
        menu.addSeparator()
        act_settings = QAction("Настройки…", self)
        act_settings.triggered.connect(self.open_settings)
        menu.addAction(act_settings)
        self.act_update = QAction(f"Проверить обновление… (v{APP_VERSION})", self)
        self.act_update.triggered.connect(self.check_for_updates)
        menu.addAction(self.act_update)
        menu.addSeparator()
        act_quit = QAction("Выход", self)
        act_quit.triggered.connect(self._tray_quit)
        menu.addAction(act_quit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:  # ЛКМ
            if self.isVisible():
                self.hide()
            else:
                self._tray_show()

    def _tray_show(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def open_settings(self):
        dlg = SettingsDialog(self.settings, self)
        dlg.applied.connect(self._apply_settings)
        dlg.exec_()

    def _apply_settings(self):
        down = self.settings.value("rate_limit_down", 0, type=int)
        up = self.settings.value("rate_limit_up", 0, type=int)
        self.seed.apply_rate_limits(down, up)
        try:
            from themes import apply_theme
            theme = self.settings.value("theme", "auto", type=str)
            apply_theme(QApplication.instance(), theme)
        except (ImportError, ModuleNotFoundError) as e:
            print(f"[main] theme module unavailable: {e}", flush=True)

    def _maybe_check_updates(self):
        if not self.settings.value("auto_check_updates", True, type=bool):
            return
        # Скрытая проверка: не показываем «уже свежая»
        self.check_for_updates(silent=True)

    def _tray_quit(self):
        if self.tray:
            self.tray.hide()
        QApplication.quit()

    def closeEvent(self, event):
        if (
            self.tray
            and self.tray.isVisible()
            and self.settings.value("minimize_on_close", True, type=bool)
        ):
            self.hide()
            event.ignore()
            if not self.settings.value("tray_hint_shown", False, type=bool):
                self.tray.showMessage(
                    APP_NAME,
                    "Свёрнуто в трей. Правый клик по иконке — настройки и выход.",
                    QSystemTrayIcon.Information,
                    4000,
                )
                self.settings.setValue("tray_hint_shown", True)
            return
        event.accept()

    def _apply_style(self):
        self.setStyleSheet("""
            QLabel#emptyHint {
                color: #888;
                font-size: 13px;
                padding: 40px;
            }
            QLabel#titleLabel {
                padding: 4px 0;
            }
            QFrame#metaBox, QFrame#progressBox {
                background: rgba(127, 127, 127, 0.08);
                border-radius: 6px;
            }
            QPushButton#primaryBtn {
                font-weight: 600;
                padding: 6px 14px;
            }
            QProgressBar {
                text-align: center;
                border: 1px solid rgba(127,127,127,0.3);
                border-radius: 4px;
                height: 20px;
            }
            QProgressBar::chunk {
                background-color: #2980b9;
                border-radius: 3px;
            }
            QProgressBar[phase="copy"]::chunk {
                background-color: #27ae60;
            }
            QLabel#banner {
                background: #c0392b;
                color: white;
                padding: 8px 12px;
                border-radius: 4px;
            }
            QLabel#banner[kind="info"] {
                background: #2980b9;
            }
            QLabel#flashInfo {
                padding: 6px 10px;
                border-radius: 4px;
                background: rgba(40, 167, 69, 0.12);
                color: #2d7a3f;
            }
            QLabel#flashInfo[state="warn"] {
                background: rgba(192, 57, 43, 0.15);
                color: #c0392b;
            }
            QLabel#flashInfo[state="off"] {
                background: rgba(127, 127, 127, 0.08);
                color: #888;
            }
        """)

    # ---------- helpers ----------

    def _show_banner(self, text: str, kind: str = "error"):
        self.banner.setText(text)
        self.banner.setProperty("kind", kind)
        self.banner.style().unpolish(self.banner)
        self.banner.style().polish(self.banner)
        self.banner.setVisible(True)

    def _hide_banner(self):
        self.banner.setVisible(False)

    def current_result(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self.results):
            return None
        title = self.table.item(row, 1).text()
        for r in self.results:
            if r["title"] == title:
                return r
        return None

    def _on_selection_changed(self):
        r = self.current_result()
        if not r:
            self.detail_card.setVisible(False)
            self.empty_label.setVisible(True)
            return
        self.empty_label.setVisible(False)
        self.detail_card.setVisible(True)
        self.title_label.setText(r["title"])
        self.date_val.setText(r["date"])
        self.size_val.setText(r["size"])
        self.seeds_val.setText(r["seeds"])
        self.leech_val.setText(r["leech"])
        m = MAGNET_HASH_RE.search(r["magnet"])
        self.hash_val.setText(m.group(1) if m else "—")
        self._refresh_flash_info()
        # Сбрасываем постер/описание, запускаем фоновое получение деталей
        self.poster_label.setVisible(False)
        self.poster_label.clear()
        self.description_view.setVisible(False)
        self.description_view.clear()
        self._current_meta_url = r["page"]
        if r["page"]:
            self._meta_fetcher = MetaFetcher(r["page"])
            self._meta_fetcher.fetched.connect(self._on_meta_fetched)
            self._meta_fetcher.start()

    def _on_meta_fetched(self, url: str, data: dict):
        # Игнорируем если пользователь уже выбрал другой торрент
        if getattr(self, "_current_meta_url", None) != url:
            return
        desc = data.get("description") or ""
        if desc:
            self.description_view.setPlainText(desc.strip())
            self.description_view.setVisible(True)
        poster_url = data.get("poster_url") or ""
        if poster_url:
            self._poster_fetcher = PosterFetcher(poster_url, referer=url)
            self._poster_fetcher.loaded.connect(self._on_poster_loaded)
            self._poster_fetcher.start()

    def _on_poster_loaded(self, url: str, data: bytes):
        if not data:
            return
        from PyQt5.QtGui import QPixmap
        pix = QPixmap()
        pix.loadFromData(data)
        if pix.isNull():
            return
        pix = pix.scaledToWidth(280, Qt.SmoothTransformation)
        self.poster_label.setPixmap(pix)
        self.poster_label.setVisible(True)
        # показываем прогресс, только если скачивается ИМЕННО этот элемент
        if self.dl_result and self.dl_result["magnet"] == r["magnet"]:
            self._refresh_progress_widget()
            self.progress_box.setVisible(True)
            self.flash_btn.setEnabled(False)
        else:
            self.progress_box.setVisible(False)
            self.flash_btn.setEnabled(self.dl_result is None)
        self._hide_banner()

    def _refresh_progress_widget(self):
        pct, status = self.dl_progress
        self.progress_bar.setValue(pct)
        self.progress_status.setText(status)
        if self.dl_phase == "dl":
            self.progress_phase.setText("Скачивание торрента")
        elif self.dl_phase == "copy":
            self.progress_phase.setText(f"Копирование → {self.dst_dir}")
        # тот же прогресс-бар, но зелёная заливка для фазы copy
        if self.progress_bar.property("phase") != self.dl_phase:
            self.progress_bar.setProperty("phase", self.dl_phase)
            self.progress_bar.style().unpolish(self.progress_bar)
            self.progress_bar.style().polish(self.progress_bar)

    # ---------- search ----------

    def start_search(self):
        query = self.input.text().strip()
        if not query:
            return
        if self.search_worker and self.search_worker.isRunning():
            return
        category = self.category_combo.currentData() or 0
        self.settings.setValue("last_category", int(category))
        self._push_history(query)
        self.search_btn.setEnabled(False)
        self.statusBar().showMessage("Поиск…")
        self._hide_banner()
        self.search_worker = SearchWorker(query, category=int(category))
        self.search_worker.done.connect(self._on_search_done)
        self.search_worker.failed.connect(self._on_search_failed)
        self.search_worker.start()

    def _push_history(self, query: str):
        q = query.strip()
        if not q:
            return
        if q in self._search_history:
            self._search_history.remove(q)
        self._search_history.insert(0, q)
        self._search_history = self._search_history[:SEARCH_HISTORY_MAX]
        self.settings.setValue("search_history", self._search_history)
        # Обновляем completer
        from PyQt5.QtCore import QStringListModel
        model = self.search_completer.model()
        if isinstance(model, QStringListModel):
            model.setStringList(self._search_history)
        else:
            self.search_completer = QCompleter(self._search_history)
            self.search_completer.setCaseSensitivity(Qt.CaseInsensitive)
            self.input.setCompleter(self.search_completer)

    def _on_search_done(self, results: list, mirror: str):
        self.search_btn.setEnabled(True)
        self.results = results
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(results))
        for i, r in enumerate(results):
            for j, key in enumerate(("date", "title", "size", "seeds", "leech")):
                item = QTableWidgetItem(r[key])
                if key in ("seeds", "leech"):
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(i, j, item)
        self.table.setSortingEnabled(True)
        self.statusBar().showMessage(f"Найдено: {len(results)} ({mirror})", 5000)
        if results:
            self.table.selectRow(0)

    def _on_search_failed(self, err: str):
        self.search_btn.setEnabled(True)
        self.statusBar().showMessage("Ошибка поиска", 5000)
        self._show_banner(f"Поиск не удался: {err}")

    # ---------- destination ----------

    def _on_flash_toggle(self, checked: bool):
        if checked:
            flash = detect_flash_mount()
            if not flash:
                self.statusBar().showMessage("Флешка не обнаружена — выключаю", 4000)
                self.flash_check.blockSignals(True)
                self.flash_check.setChecked(False)
                self.flash_check.blockSignals(False)
                self.dst_dir = str(Path.home() / "Storage")
            else:
                self.dst_dir = str(Path(flash) / "Movies")
        else:
            self.dst_dir = str(Path.home() / "Storage")
        self.dst_edit.setText(self.dst_dir)

    def choose_destination(self):
        d = QFileDialog.getExistingDirectory(self, "Куда копировать", self.dst_dir)
        if d:
            self.dst_dir = d
            self.dst_edit.setText(d)
            # Ручной выбор — снимаем галочку флешки
            self.flash_check.blockSignals(True)
            self.flash_check.setChecked(False)
            self.flash_check.blockSignals(False)
            self.statusBar().showMessage(f"Папка: {d}", 3000)

    def redetect_flash(self):
        flash = detect_flash_mount()
        if flash:
            if self.flash_check.isChecked():
                self.dst_dir = str(Path(flash) / "Movies")
                self.dst_edit.setText(self.dst_dir)
            self.statusBar().showMessage(f"Найдена флешка: {flash}", 4000)
        else:
            self.statusBar().showMessage("Флешка не обнаружена", 4000)

    def eject_flash(self):
        print("[eject] start", flush=True)
        if self.dl_result is not None:
            self._show_banner("Идёт загрузка — дождитесь завершения перед извлечением")
            print("[eject] skip: download in progress", flush=True)
            return
        if self.copy_worker and self.copy_worker.isRunning():
            self._show_banner("Идёт копирование на флешку — дождитесь завершения")
            print("[eject] skip: copy in progress", flush=True)
            return
        mount = detect_flash_mount()
        print(f"[eject] mount={mount}", flush=True)
        if not mount:
            self._show_banner("Флешка не смонтирована")
            return
        try:
            src = subprocess.run(
                ["findmnt", "-no", "SOURCE", mount],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            print(f"[eject] device={src}", flush=True)
            if not src:
                self._show_banner(f"Не удалось определить устройство для {mount}")
                return
            parent = re.sub(r"\d+$", "", src)
            print(f"[eject] parent={parent}", flush=True)

            # Сначала syncим
            subprocess.run(["sync"], check=False)

            unmount_res = subprocess.run(
                ["udisksctl", "unmount", "-b", src],
                capture_output=True, text=True,
            )
            print(
                f"[eject] unmount rc={unmount_res.returncode} "
                f"stdout={unmount_res.stdout.strip()!r} "
                f"stderr={unmount_res.stderr.strip()!r}",
                flush=True,
            )
            if unmount_res.returncode != 0:
                # Узнаём, кто держит
                busy = ""
                try:
                    lsof = subprocess.run(
                        ["lsof", "+D", mount],
                        capture_output=True, text=True, timeout=5,
                    )
                    print(f"[eject] lsof rc={lsof.returncode}", flush=True)
                    lines = [l for l in lsof.stdout.splitlines() if l and not l.startswith("COMMAND")]
                    print(f"[eject] lsof lines: {len(lines)}", flush=True)
                    if lines:
                        # Берём имя процесса и PID — colонки 1 и 2
                        procs = set()
                        for l in lines[:20]:
                            parts = l.split()
                            if len(parts) >= 2:
                                procs.add(f"{parts[0]}({parts[1]})")
                        busy = ", ".join(sorted(procs))
                        print(f"[eject] busy: {busy}", flush=True)
                except (FileNotFoundError, subprocess.SubprocessError) as e:
                    print(f"[eject] lsof not available: {e}", flush=True)
                err_msg = (unmount_res.stderr or unmount_res.stdout or "").strip()
                msg = f"Не удалось размонтировать: {err_msg}"
                if busy:
                    msg += f"\nДержат: {busy}"
                self._show_banner(msg)
                return

            poff = subprocess.run(
                ["udisksctl", "power-off", "-b", parent],
                capture_output=True, text=True,
            )
            print(
                f"[eject] power-off rc={poff.returncode} "
                f"stdout={poff.stdout.strip()!r} "
                f"stderr={poff.stderr.strip()!r}",
                flush=True,
            )
            if poff.returncode != 0:
                # unmount удался, но power-off нет — флешка размонтирована, можно вынимать
                self._show_banner(
                    f"Размонтировано, но power-off не сработал: "
                    f"{(poff.stderr or poff.stdout).strip()}. "
                    "Можно вынимать.",
                    kind="info",
                )
            else:
                self._show_banner(
                    f"Флешка извлечена ({src}) — можно вынимать", kind="info"
                )
            # Переходим в режим ~/Storage
            self.flash_check.blockSignals(True)
            self.flash_check.setChecked(False)
            self.flash_check.blockSignals(False)
            self.dst_dir = str(Path.home() / "Storage")
            self.dst_edit.setText(self.dst_dir)
            self.statusBar().showMessage("Флешка безопасно извлечена", 5000)
        except FileNotFoundError as e:
            print(f"[eject] tool missing: {e}", flush=True)
            self._show_banner(f"Утилита не найдена: {e}")
        except subprocess.SubprocessError as e:
            print(f"[eject] subprocess error: {e}", flush=True)
            self._show_banner(f"Ошибка: {e}")

    # ---------- actions ----------

    def copy_magnet(self):
        r = self.current_result()
        if not r:
            return
        QGuiApplication.clipboard().setText(r["magnet"])
        self.statusBar().showMessage("Magnet скопирован", 3000)

    def open_page(self):
        r = self.current_result()
        if not r or not r["page"]:
            return
        import webbrowser
        webbrowser.open(r["page"])

    def open_in_ktorrent(self):
        r = self.current_result()
        if not r:
            return
        exe = shutil.which("ktorrent")
        if not exe:
            self._show_banner("KTorrent не найден в PATH")
            return
        try:
            subprocess.Popen([exe, r["magnet"]])
            self.statusBar().showMessage("Открыто в KTorrent", 3000)
        except OSError as e:
            self._show_banner(f"Ошибка запуска KTorrent: {e}")

    def download_to_flash(self):
        r = self.current_result()
        if not r:
            return
        try:
            STORAGE_DEFAULT.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._show_banner(f"Не удалось создать {STORAGE_DEFAULT}: {e}")
            return
        # Если уже идёт загрузка — добавим в очередь
        if self.dl_result is not None:
            if not hasattr(self, "_dl_queue"):
                self._dl_queue = []
            entry = {
                "magnet": r["magnet"],
                "torrent_url": r.get("torrent_url", ""),
                "title": r["title"],
                "use_flash": self.flash_check.isChecked(),
            }
            # Избегаем дубликатов
            if not any(q["magnet"] == r["magnet"] for q in self._dl_queue):
                self._dl_queue.append(entry)
                self.statusBar().showMessage(
                    f"В очередь #{len(self._dl_queue)}: {r['title'][:60]}", 4000
                )
            else:
                self.statusBar().showMessage("Уже в очереди", 3000)
            return
        # Если флешка — проверяем доступ дополнительно при копировании.
        self._hide_banner()
        self.dl_result = r
        self.dl_phase = "dl"
        self.dl_progress = (0, "Запуск…")
        self._refresh_progress_widget()
        self.progress_box.setVisible(True)
        self.flash_btn.setEnabled(False)

        self.dl_worker = DownloadWorker(
            self.seed, r["magnet"], str(STORAGE_DEFAULT), r.get("torrent_url", ""),
            mark_pending_flash=self.flash_check.isChecked(),
        )
        self.dl_worker.progress.connect(self._on_dl_progress)
        self.dl_worker.done.connect(self._on_dl_done)
        self.dl_worker.failed.connect(self._on_dl_failed)
        self.dl_worker.start()

    def _on_cancel(self):
        if self.dl_worker and self.dl_worker.isRunning():
            self.dl_worker.cancel()
        if self.copy_worker and self.copy_worker.isRunning():
            self.copy_worker.cancel()

    def _on_dl_progress(self, pct: int, status: str):
        self.dl_progress = (pct, status)
        # обновляем UI только если просматриваем этот же элемент
        cur = self.current_result()
        if cur and self.dl_result and cur["magnet"] == self.dl_result["magnet"]:
            self._refresh_progress_widget()

    def _on_dl_failed(self, err: str):
        self._reset_dl_state()
        if err == "Отменено":
            self.statusBar().showMessage("Загрузка отменена", 3000)
            return
        self._show_banner(f"Ошибка загрузки: {err}")

    def _on_dl_done(self, save_dir: str, rel_paths: list, info_hash: str):
        # Файлы лежат в ~/Storage и торрент остаётся в seed session.
        # Если включена флешка — копируем туда дополнительно.
        if not self.flash_check.isChecked():
            self._reset_dl_state()
            self.statusBar().showMessage(
                f"Скачано в {save_dir}, продолжаю раздачу", 8000
            )
            self._show_banner(
                f"Готово: файлы в {save_dir}, раздаются. Управление — на вкладке «Моя раздача».",
                kind="info",
            )
            return
        # Копирование на флешку
        try:
            Path(self.dst_dir).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._reset_dl_state()
            self._show_banner(f"Не удалось создать {self.dst_dir}: {e}")
            return
        if not os.access(self.dst_dir, os.W_OK):
            self._reset_dl_state()
            self._show_banner(f"Нет прав на запись в {self.dst_dir}")
            return
        self.dl_phase = "copy"
        self.dl_progress = (0, "Подготовка…")
        cur = self.current_result()
        if cur and self.dl_result and cur["magnet"] == self.dl_result["magnet"]:
            self._refresh_progress_widget()
        self.copy_worker = CopyWorker(save_dir, rel_paths, self.dst_dir, FAT32_MAX_PART)
        self.copy_worker.progress.connect(self._on_copy_progress)
        self.copy_worker.done.connect(self._on_copy_done)
        self.copy_worker.failed.connect(self._on_copy_failed)
        self.copy_worker.start()

    def _on_copy_progress(self, pct: int, status: str):
        self.dl_progress = (pct, status)
        cur = self.current_result()
        if cur and self.dl_result and cur["magnet"] == self.dl_result["magnet"]:
            self._refresh_progress_widget()

    def _on_copy_done(self, report: list):
        summary = " · ".join(line for line in report)
        # Сбрасываем pending_flash_copy для активного торрента
        if self.dl_worker and self.dl_worker.info_hash in self.seed.library:
            self.seed.library[self.dl_worker.info_hash].pop("pending_flash_copy", None)
            self.seed._save_library()
        self._reset_dl_state()
        self.statusBar().showMessage(f"Готово: {summary}", 8000)
        self._show_banner(
            "Скопировано на флешку. Оригинал в ~/Storage, раздаётся.",
            kind="info",
        )

    def _on_copy_failed(self, err: str):
        self._reset_dl_state()
        if err == "Отменено":
            self.statusBar().showMessage("Копирование отменено", 3000)
            return
        self._show_banner(
            f"Ошибка копирования: {err}. Файлы скачаны в ~/Storage, раздача идёт."
        )

    def _reset_dl_state(self):
        self.dl_result = None
        self.dl_worker = None
        self.copy_worker = None
        self.dl_phase = ""
        self.dl_progress = (0, "")
        self.progress_box.setVisible(False)
        self.flash_btn.setEnabled(True)
        # Если есть очередь — стартуем следующий
        if getattr(self, "_dl_queue", None):
            next_item = self._dl_queue.pop(0)
            self.flash_check.blockSignals(True)
            self.flash_check.setChecked(next_item.get("use_flash", False))
            self.flash_check.blockSignals(False)
            fake = {
                "magnet": next_item["magnet"],
                "torrent_url": next_item.get("torrent_url", ""),
                "title": next_item.get("title", ""),
            }
            self._start_download_for(fake)

    def _start_download_for(self, r: dict):
        self._hide_banner()
        self.dl_result = r
        self.dl_phase = "dl"
        self.dl_progress = (0, "Запуск из очереди…")
        self._refresh_progress_widget()
        self.progress_box.setVisible(True)
        self.flash_btn.setEnabled(False)
        self.dl_worker = DownloadWorker(
            self.seed, r["magnet"], str(STORAGE_DEFAULT),
            r.get("torrent_url", ""),
            mark_pending_flash=self.flash_check.isChecked(),
        )
        self.dl_worker.progress.connect(self._on_dl_progress)
        self.dl_worker.done.connect(self._on_dl_done)
        self.dl_worker.failed.connect(self._on_dl_failed)
        self.dl_worker.start()
        self.statusBar().showMessage(f"Из очереди: {r.get('title','')[:60]}", 4000)

    # ---------- flash info ----------

    def _refresh_flash_info(self):
        if not hasattr(self, "flash_info"):
            return
        mount = detect_flash_mount()
        r = self.current_result()
        torrent_size = parse_size_text(r["size"]) if r else 0

        def set_state(state: str):
            self.flash_info.setProperty("state", state)
            self.flash_info.style().unpolish(self.flash_info)
            self.flash_info.style().polish(self.flash_info)

        if not mount:
            self.flash_info.setText("Флешка не подключена — копирование пропустим")
            set_state("off")
            return
        try:
            usage = shutil.disk_usage(mount)
        except OSError as e:
            self.flash_info.setText(f"Ошибка чтения {mount}: {e}")
            set_state("warn")
            return
        fs = ""
        try:
            res = subprocess.run(
                ["findmnt", "-no", "FSTYPE", mount],
                capture_output=True, text=True, check=True, timeout=2,
            )
            fs = res.stdout.strip()
        except (subprocess.SubprocessError, OSError, FileNotFoundError):
            pass
        label = Path(mount).name
        text = (
            f"Флешка <b>{label}</b> ({mount})"
            + (f" · {fs}" if fs else "")
            + f" · свободно <b>{human_bytes(usage.free)}</b> из {human_bytes(usage.total)}"
        )
        if torrent_size > 0:
            text += f"<br/>Размер торрента: <b>{human_bytes(torrent_size)}</b>"
            if torrent_size > usage.free:
                need = torrent_size - usage.free
                text += f" — <b>не помещается</b>, не хватает {human_bytes(need)}"
                set_state("warn")
            else:
                left = usage.free - torrent_size
                text += f" — после копирования останется {human_bytes(left)}"
                set_state("ok")
        else:
            set_state("ok")
        self.flash_info.setText(text)

    # ---------- library / seeding ----------

    def _refresh_library(self):
        rows = self.seed.all_statuses()
        rows.sort(key=lambda r: (r["progress"] >= 1.0, -r["upload_rate"]))
        self.lib_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self._set_lib_row(i, r)
        self._check_pending_flash_copies(rows)
        self._refresh_lib_detail()

    def _check_pending_flash_copies(self, rows: list):
        """Если торрент завершён и помечен pending_flash_copy — копируем на флешку.

        Работает и после перезапуска: флаг хранится в library.json."""
        if self.copy_worker and self.copy_worker.isRunning():
            return
        if self.dl_worker and self.dl_worker.isRunning():
            # Свой обработчик _on_dl_done сам разберётся
            return
        mount = detect_flash_mount()
        if not mount:
            return
        for r in rows:
            if not r["is_seeding"] and r["progress"] < 1.0:
                continue
            meta = self.seed.library.get(r["hash"], {})
            if not meta.get("pending_flash_copy"):
                continue
            handle = self.seed.handles.get(r["hash"])
            if not handle:
                continue
            info = handle.torrent_file()
            if not info:
                continue
            files = info.files()
            rel_paths = [files.file_path(i) for i in range(files.num_files())]
            target = str(Path(mount) / "Movies")
            try:
                Path(target).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                print(f"[flash] mkdir failed: {e}", flush=True)
                return
            print(f"[flash] auto-copy {r['title'][:60]} → {target}", flush=True)
            self._pending_copy_hash = r["hash"]
            self._pending_copy_title = r["title"]
            self.copy_worker = CopyWorker(meta["save_path"], rel_paths, target, FAT32_MAX_PART)
            self.copy_worker.progress.connect(self._on_pending_copy_progress)
            self.copy_worker.done.connect(self._on_pending_copy_done)
            self.copy_worker.failed.connect(self._on_pending_copy_failed)
            self.lib_copy_phase.setText(f"Копирую на флешку: {r['title'][:80]}")
            self.lib_copy_bar.setValue(0)
            self.lib_copy_status.setText("Подготовка…")
            self.lib_copy_box.setVisible(True)
            self.copy_worker.start()
            self.statusBar().showMessage(
                f"Копирую на флешку: {r['title'][:60]}", 5000
            )
            return  # одна копия за раз

    def _on_pending_copy_progress(self, pct: int, status: str):
        if hasattr(self, "lib_copy_bar"):
            self.lib_copy_bar.setValue(pct)
            self.lib_copy_status.setText(status)
        self.statusBar().showMessage(f"Флешка: {status}", 2500)

    def _on_pending_copy_done(self, report: list):
        hid = getattr(self, "_pending_copy_hash", None)
        if hid and hid in self.seed.library:
            self.seed.library[hid].pop("pending_flash_copy", None)
            self.seed._save_library()
        self._pending_copy_hash = None
        self.copy_worker = None
        if hasattr(self, "lib_copy_box"):
            self.lib_copy_box.setVisible(False)
        self.statusBar().showMessage("Скопировано на флешку", 5000)
        if self.tray:
            self.tray.showMessage(
                APP_NAME,
                f"Скопировано на флешку: {getattr(self, '_pending_copy_title', '')[:80]}",
                QSystemTrayIcon.Information,
                4000,
            )

    def _on_pending_copy_failed(self, err: str):
        self._pending_copy_hash = None
        self.copy_worker = None
        if hasattr(self, "lib_copy_box"):
            self.lib_copy_box.setVisible(False)
        if err != "Отменено":
            self.statusBar().showMessage(f"Не удалось скопировать на флешку: {err}", 5000)

    def _cancel_pending_copy(self):
        if self.copy_worker and self.copy_worker.isRunning():
            self.copy_worker.cancel()

    def _set_lib_row(self, i: int, r: dict):
        title_item = QTableWidgetItem(r["title"] or "(метаданные…)")
        title_item.setData(Qt.UserRole, r["hash"])
        size_item = QTableWidgetItem(human_bytes(r["size"]) if r["size"] else "?")
        pct = int(r["progress"] * 100)
        if r["is_seeding"] or pct == 100:
            prog_text = "раздача"
        elif r["has_metadata"]:
            prog_text = f"{r['state']} {pct}%"
        else:
            prog_text = "метаданные…"
        prog_item = QTableWidgetItem(prog_text)
        prog_item.setTextAlignment(Qt.AlignCenter)
        down_item = QTableWidgetItem(f"{human_bytes(r['download_rate'])}/s")
        up_item = QTableWidgetItem(f"{human_bytes(r['upload_rate'])}/s")
        if r["upload_rate"] > 0:
            up_item.setForeground(Qt.green)
        peers_item = QTableWidgetItem(f"{r['num_peers']} ({r['num_seeds']}↑)")
        peers_item.setTextAlignment(Qt.AlignCenter)
        for col, item in enumerate(
            (title_item, size_item, prog_item, down_item, up_item, peers_item)
        ):
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.lib_table.setItem(i, col, item)

    def _lib_context_menu(self, pos):
        item = self.lib_table.itemAt(pos)
        if not item:
            return
        row = item.row()
        title_item = self.lib_table.item(row, 0)
        info_hash = title_item.data(Qt.UserRole) if title_item else None
        if not info_hash:
            return
        menu = QMenu(self)
        act_open = menu.addAction("Открыть папку")
        act_open.triggered.connect(lambda: self._lib_open_folder(info_hash))
        menu.addSeparator()
        act_rm = menu.addAction("Убрать из раздачи (файлы оставить)")
        act_rm.triggered.connect(lambda: self._lib_remove(info_hash, False))
        act_del = menu.addAction("Удалить вместе с файлами")
        act_del.triggered.connect(lambda: self._lib_remove(info_hash, True))
        menu.exec_(self.lib_table.viewport().mapToGlobal(pos))

    def _lib_open_folder(self, info_hash: str):
        meta = self.seed.library.get(info_hash)
        if not meta:
            return
        save = Path(meta.get("save_path", STORAGE_DEFAULT))
        try:
            subprocess.Popen(["xdg-open", str(save)])
        except OSError as e:
            self._show_banner(f"Не открыть {save}: {e}")

    def _lib_remove(self, info_hash: str, delete_files: bool):
        self.seed.remove(info_hash, delete_files=delete_files)
        self._refresh_library()
        msg = "Удалено вместе с файлами" if delete_files else "Убрано из раздачи"
        self.statusBar().showMessage(msg, 3000)

    def _selected_lib_hash(self):
        row = self.lib_table.currentRow()
        if row < 0:
            return None
        item = self.lib_table.item(row, 0)
        return item.data(Qt.UserRole) if item else None

    def _on_lib_selection_changed(self):
        if not self._selected_lib_hash():
            self.lib_detail_card.setVisible(False)
            self.lib_empty.setVisible(True)
            return
        self._refresh_lib_detail()

    def _refresh_lib_detail(self):
        hid = self._selected_lib_hash()
        if not hid:
            return
        s = self.seed.get_status(hid)
        h = self.seed.handles.get(hid)
        if not s or not h:
            self.lib_detail_card.setVisible(False)
            self.lib_empty.setVisible(True)
            return
        self.lib_empty.setVisible(False)
        self.lib_detail_card.setVisible(True)

        st = h.status()
        paused = bool(getattr(st, "paused", False)) or bool(getattr(st, "auto_managed", True)) is False and bool(getattr(st, "paused", False))
        try:
            paused = bool(st.paused)
        except AttributeError:
            paused = False

        self.lib_title.setText(s["title"])
        if paused:
            status = "На паузе"
        elif s["is_seeding"]:
            status = "Раздаётся"
        elif s["has_metadata"]:
            status = f"{s['state']} {int(s['progress'] * 100)}%"
        else:
            status = "Получение метаданных…"
        self.lib_status_val.setText(status)
        self.lib_size_val.setText(human_bytes(s["size"]) if s["size"] else "?")
        if s["size"]:
            downloaded = s["progress"] * s["size"]
            self.lib_downloaded_val.setText(
                f"{human_bytes(downloaded)} ({int(s['progress'] * 100)}%)"
            )
        else:
            self.lib_downloaded_val.setText(f"{int(s['progress'] * 100)}%")
        self.lib_rates_val.setText(
            f"↓ {human_bytes(s['download_rate'])}/s · ↑ {human_bytes(s['upload_rate'])}/s"
        )
        self.lib_peers_val.setText(f"{s['num_peers']} (сидов: {s['num_seeds']})")
        self.lib_path_val.setText(s["save_path"])
        total_up = getattr(st, "total_payload_upload", 0) or 0
        total_done = max(1, int(s["progress"] * s["size"])) if s["size"] else 1
        ratio = total_up / total_done if total_done else 0
        self.lib_ratio_val.setText(f"{human_bytes(total_up)} (ratio {ratio:.2f})")
        pending = self.seed.library.get(hid, {}).get("pending_flash_copy", False)
        self.lib_pending_val.setText("✓ запланировано" if pending else "—")
        # Медиа-инфо для самого большого .mkv в папке торрента (один раз, кэшируем)
        self._update_media_info(hid, h, s)
        self.lib_progress_bar.setValue(int(s["progress"] * 100))
        self.lib_progress_bar.setProperty(
            "phase", "copy" if s["is_seeding"] else "dl"
        )
        self.lib_progress_bar.style().unpolish(self.lib_progress_bar)
        self.lib_progress_bar.style().polish(self.lib_progress_bar)

        style = self.style()
        if paused:
            self.lib_pause_btn.setText("Возобновить")
            self.lib_pause_btn.setIcon(themed_icon("media-playback-start", style, QStyle.SP_MediaPlay))
        else:
            self.lib_pause_btn.setText("Пауза")
            self.lib_pause_btn.setIcon(themed_icon("media-playback-pause", style, QStyle.SP_MediaPause))
        self.lib_flash_btn_panel.setEnabled(not pending)

    def _update_media_info(self, hid: str, handle, s: dict):
        if not s.get("has_metadata"):
            self.lib_media_val.setText("—")
            return
        if not hasattr(self, "_media_cache"):
            self._media_cache = {}
        if hid in self._media_cache:
            self.lib_media_val.setText(self._media_cache[hid])
            return
        # Ищем самый большой видеофайл
        try:
            info = handle.torrent_file()
            files = info.files()
            best = None
            best_size = 0
            for i in range(files.num_files()):
                path = files.file_path(i)
                if path.lower().endswith((".mkv", ".mp4", ".avi", ".m4v", ".mov")):
                    fs = files.file_size(i)
                    if fs > best_size:
                        best_size = fs
                        best = path
            if not best:
                self.lib_media_val.setText("—")
                self._media_cache[hid] = "—"
                return
            full = Path(s["save_path"]) / best
            if not full.exists():
                self.lib_media_val.setText("(файл недоступен)")
                return
        except Exception as e:
            self.lib_media_val.setText(f"ошибка: {e}")
            return
        # Запускаем фоновую проверку
        self._media_cache[hid] = "загружаю…"
        self.lib_media_val.setText("загружаю…")

        class _MediaWorker(QThread):
            done = pyqtSignal(str, str)

            def __init__(self, hid, path):
                super().__init__()
                self.hid = hid
                self.path = path

            def run(self):
                try:
                    from mediainfo import file_info
                    data = file_info(str(self.path))
                    summary = data.get("human_summary", "") or "—"
                except Exception as e:
                    summary = f"(ошибка: {e})"
                self.done.emit(self.hid, summary)

        w = _MediaWorker(hid, full)
        w.done.connect(self._on_media_done)
        w.start()
        # Сохраняем ссылку чтобы не GC'нулся
        self._media_worker = w

    def _on_media_done(self, hid: str, summary: str):
        self._media_cache[hid] = summary
        if self._selected_lib_hash() == hid:
            self.lib_media_val.setText(summary)

    def _lib_pause_toggle(self):
        hid = self._selected_lib_hash()
        if not hid:
            return
        h = self.seed.handles.get(hid)
        if not h:
            return
        try:
            if h.status().paused:
                h.resume()
            else:
                h.pause()
        except AttributeError:
            pass
        self._refresh_lib_detail()

    def _lib_force_recheck(self):
        hid = self._selected_lib_hash()
        if not hid:
            return
        h = self.seed.handles.get(hid)
        if h:
            try:
                h.force_recheck()
                self.statusBar().showMessage("Перепроверка пиров запущена", 3000)
            except AttributeError as e:
                self.statusBar().showMessage(f"recheck недоступен: {e}", 3000)

    def _lib_open_current_folder(self):
        hid = self._selected_lib_hash()
        if hid:
            self._lib_open_folder(hid)

    def _lib_queue_flash(self):
        hid = self._selected_lib_hash()
        if not hid:
            return
        meta = self.seed.library.get(hid)
        if not meta:
            return
        meta["pending_flash_copy"] = True
        self.seed._save_library()
        self.statusBar().showMessage(
            "Запланировано — скопируем при появлении флешки", 4000
        )
        self._check_pending_flash_copies(self.seed.all_statuses())
        self._refresh_lib_detail()

    def _lib_remove_current_keep(self):
        hid = self._selected_lib_hash()
        if hid:
            self._lib_remove(hid, delete_files=False)

    def _lib_remove_current_delete(self):
        hid = self._selected_lib_hash()
        if hid:
            self._lib_remove(hid, delete_files=True)

    # ---------- updater ----------

    def check_for_updates(self, silent: bool = False):
        if getattr(self, "update_checker", None) and self.update_checker.isRunning():
            return
        self._update_silent = silent
        if not silent:
            self.statusBar().showMessage("Проверяю обновление…", 3000)
        self.update_checker = UpdateChecker()
        self.update_checker.found.connect(self._on_update_found)
        self.update_checker.up_to_date.connect(self._on_up_to_date)
        self.update_checker.failed.connect(self._on_update_check_failed)
        self.update_checker.start()

    def _on_update_found(self, version: str, url: str, asset_name: str):
        self._pending_update = (version, url, asset_name)
        self._show_banner(
            f"Доступна версия v{version} (сейчас v{APP_VERSION}). "
            "Нажмите ⏏ для обновления → автозамена бинарника и перезапуск.",
            kind="info",
        )
        # Используем кнопку eject_btn временно? Лучше отдельную. Покажем уведомление трея.
        if self.tray:
            self.tray.showMessage(
                APP_NAME,
                f"Доступна версия v{version}. Кликните в меню «Установить обновление».",
                QSystemTrayIcon.Information,
                6000,
            )
        # Меняем пункт меню на «Установить обновление v…»
        self.act_update.setText(f"Установить обновление v{version}")
        try:
            self.act_update.triggered.disconnect()
        except TypeError:
            pass
        self.act_update.triggered.connect(self._install_pending_update)

    def _install_pending_update(self):
        if not getattr(self, "_pending_update", None):
            return
        if not getattr(sys, "frozen", False):
            self._show_banner(
                "Запущена python-версия — обновление возможно только для бинарника. "
                "Запустите через ярлык TorFlash и попробуйте снова."
            )
            return
        version, url, _ = self._pending_update
        binary_dir = str(Path(sys.executable).parent)
        self._update_dl = UpdateDownloader(url, binary_dir)
        self.dl_phase = "copy"   # переиспользуем зелёный стиль для прогресса
        self.dl_progress = (0, "Скачивание обновления…")
        self.progress_phase.setText(f"Обновление до v{version}")
        self.progress_bar.setProperty("phase", "copy")
        self.progress_bar.style().unpolish(self.progress_bar)
        self.progress_bar.style().polish(self.progress_bar)
        self.progress_box.setVisible(True)
        self._update_dl.progress.connect(self._on_update_dl_progress)
        self._update_dl.done.connect(self._on_update_dl_done)
        self._update_dl.failed.connect(self._on_update_dl_failed)
        self._update_dl.start()

    def _on_update_dl_progress(self, pct: int, status: str):
        self.progress_bar.setValue(pct)
        self.progress_status.setText(status)

    def _on_update_dl_done(self, new_path: str):
        self.progress_box.setVisible(False)
        current = Path(sys.executable)
        try:
            os.replace(new_path, current)
        except OSError as e:
            self._show_banner(f"Не удалось заменить бинарник: {e}")
            return
        self._show_banner(
            f"Обновление установлено. Перезапуск…",
            kind="info",
        )
        # exec на самого себя — на Linux замена ELF inode допустима для запущенного процесса
        if self.tray:
            self.tray.hide()
        os.execv(str(current), [str(current)] + sys.argv[1:])

    def _on_update_dl_failed(self, err: str):
        self.progress_box.setVisible(False)
        self._show_banner(f"Не удалось скачать обновление: {err}")

    def _on_up_to_date(self, version: str):
        if not getattr(self, "_update_silent", False):
            self.statusBar().showMessage(f"Установлена последняя версия (v{version})", 5000)

    def _on_update_check_failed(self, err: str):
        if not getattr(self, "_update_silent", False):
            self.statusBar().showMessage(f"Проверка обновлений не удалась: {err}", 5000)


def setup_icon_theme():
    """Гарантируем что QIcon.fromTheme(name) находит системные иконки.
    Под PyInstaller/некоторыми DE дефолтные пути не включают /usr/share/icons."""
    paths = list(QIcon.themeSearchPaths())
    for p in (
        "/usr/share/icons",
        str(Path.home() / ".local" / "share" / "icons"),
        str(Path.home() / ".icons"),
    ):
        if p not in paths and Path(p).exists():
            paths.append(p)
    QIcon.setThemeSearchPaths(paths)
    if not QIcon.themeName() or QIcon.themeName() == "hicolor":
        for theme in ("breeze", "Adwaita", "Papirus", "gnome", "oxygen"):
            for base in paths:
                if (Path(base) / theme / "index.theme").exists():
                    QIcon.setThemeName(theme)
                    return


def main():
    # При запуске бинарника без терминала перенаправляем логи в файл
    if getattr(sys, "frozen", False):
        try:
            LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
            log_path = LIBRARY_DIR / "torflash.log"
            f = open(log_path, "a", buffering=1)
            sys.stdout = f
            sys.stderr = f
            print(f"\n=== {APP_NAME} v{APP_VERSION} started at {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)
        except OSError:
            pass

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)  # окно скрывается в трей — не выходим
    setup_icon_theme()
    icon_path = Path(__file__).resolve().parent / "torflash.svg"
    if not icon_path.exists():
        mei = getattr(sys, "_MEIPASS", None)
        if mei:
            icon_path = Path(mei) / "torflash.svg"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    w = MainWindow()

    # Корректное закрытие seed-сессии и сохранение resume_data
    def _on_about_to_quit():
        try:
            w.seed.shutdown()
        except Exception as e:
            print(f"[main] seed shutdown error: {e}", flush=True)
    app.aboutToQuit.connect(_on_about_to_quit)

    start_hidden = (
        "--hidden" in sys.argv
        or w.settings.value("start_hidden", False, type=bool)
    )
    if not start_hidden:
        w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
