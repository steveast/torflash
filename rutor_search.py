#!/usr/bin/env python3
"""TorFlash — поиск торрентов rutor.info и закачка на флешку с разбиением для FAT32."""

APP_NAME = "TorFlash"
APP_VERSION = "1.0.0"
GITHUB_REPO = "steveast/torflash"

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
from PyQt5.QtCore import Qt, QSettings, QThread, pyqtSignal
from PyQt5.QtGui import QFont, QGuiApplication, QIcon
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QCheckBox,
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
    QSplitter,
    QStatusBar,
    QStyle,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
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


class DownloadWorker(QThread):
    progress = pyqtSignal(int, str)  # percent, status_line
    done = pyqtSignal(str, list)     # save_dir, list of relative file paths
    failed = pyqtSignal(str)

    def __init__(self, magnet: str, save_dir: str, torrent_url: str = ""):
        super().__init__()
        self.magnet = magnet
        self.torrent_url = torrent_url
        self.save_dir = save_dir
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            import libtorrent as lt
        except ImportError as e:
            self.failed.emit(f"libtorrent недоступен: {e}")
            return

        try:
            print(f"[DL] start magnet={self.magnet[:80]}…", flush=True)
            print(f"[DL] save_dir={self.save_dir}", flush=True)
            # UDP в split-routing часто блокируется → DHT не работает.
            # Используем только TCP-трекеры и единственный сетевой интерфейс,
            # чтобы libtorrent не пытался announce-ить с каждого veth/docker.
            ses = lt.session({
                "listen_interfaces": "0.0.0.0:6881",
                "alert_mask": (
                    lt.alert.category_t.error_notification
                    | lt.alert.category_t.status_notification
                ),
                "enable_dht": True,
                "enable_lsd": False,
                "enable_upnp": False,
                "enable_natpmp": False,
                "announce_to_all_trackers": True,
                "announce_to_all_tiers": True,
                "enable_outgoing_utp": True,
                "enable_incoming_utp": True,
                "enable_outgoing_tcp": True,
                "enable_incoming_tcp": True,
                "dht_bootstrap_nodes": (
                    "router.bittorrent.com:6881,"
                    "router.utorrent.com:6881,"
                    "dht.transmissionbt.com:6881"
                ),
            })
            print(f"[DL] session listening on port {ses.listen_port()}", flush=True)
            # Только HTTP/HTTPS-трекеры — UDP режется sing-box VPN.
            extra_trackers = [
                "https://tracker.opentrackr.org:443/announce",
                "https://tracker.gbitt.info:443/announce",
                "https://tracker1.520.jp:443/announce",
                "http://tracker.openbittorrent.com:80/announce",
                "http://retracker.local/announce",
            ]
            params = None
            if self.torrent_url:
                print(f"[DL] downloading .torrent from {self.torrent_url}", flush=True)
                try:
                    r = requests.get(self.torrent_url, headers=HEADERS, timeout=20, allow_redirects=True)
                    r.raise_for_status()
                    print(f"[DL] .torrent {len(r.content)} bytes", flush=True)
                    info = lt.torrent_info(lt.bdecode(r.content))
                    params = lt.add_torrent_params()
                    params.ti = info
                except (requests.RequestException, RuntimeError) as e:
                    print(f"[DL] .torrent fetch failed: {e}; fallback to magnet", flush=True)
                    params = None
            if params is None:
                params = lt.parse_magnet_uri(self.magnet)
            params.save_path = self.save_dir
            existing = list(params.trackers or [])
            for t in extra_trackers:
                if t not in existing:
                    existing.append(t)
            params.trackers = existing
            handle = ses.add_torrent(params)
            handle.force_dht_announce()
            has_meta_already = params.ti is not None
            print(
                f"[DL] torrent added, trackers: {len(params.trackers)}, "
                f"metadata-from-file: {has_meta_already}",
                flush=True,
            )

            self.progress.emit(0, "Получение метаданных (DHT + трекеры)…")
            meta_deadline = time.monotonic() + 180  # 3 минуты на метаданные
            tick = 0
            seen_alerts: set[str] = set()
            while not handle.status().has_metadata:
                if self._cancel:
                    ses.remove_torrent(handle)
                    self.failed.emit("Отменено")
                    return
                if time.monotonic() > meta_deadline:
                    s = handle.status()
                    ses.remove_torrent(handle)
                    self.failed.emit(
                        f"Не удалось получить метаданные за 3 мин "
                        f"(DHT-нод: {getattr(s, 'dht_nodes', '?')}, "
                        f"пиров: {s.num_peers}). "
                        "Проверьте сеть/VPN или попробуйте другой торрент."
                    )
                    return
                s = handle.status()
                dht = getattr(s, "dht_nodes", None)
                for a in ses.pop_alerts():
                    msg = a.message()
                    low = msg.lower()
                    if "error" not in low and "fail" not in low:
                        continue
                    # Сворачиваем шум: уникализируем по типу + первой части без IP
                    key = f"{type(a).__name__}:{msg.split('[')[0]}"
                    if key in seen_alerts:
                        continue
                    seen_alerts.add(key)
                    print(f"[DL][alert] {type(a).__name__}: {msg}", flush=True)
                if tick % 5 == 0:
                    print(
                        f"[DL] meta wait t={tick}s peers={s.num_peers} dht_nodes={dht}",
                        flush=True,
                    )
                self.progress.emit(
                    0,
                    f"Получение метаданных… пиров: {s.num_peers}"
                    + (f" · DHT-нод: {dht}" if dht else ""),
                )
                time.sleep(1)
                tick += 1
            print(f"[DL] metadata received after {tick}s", flush=True)

            info = handle.torrent_file()
            total = info.total_size()
            file_storage = info.files()
            rel_paths = [file_storage.file_path(i) for i in range(file_storage.num_files())]

            dl_tick = 0
            dl_start = time.monotonic()
            while True:
                if self._cancel:
                    ses.remove_torrent(handle)
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
                if dl_tick % 5 == 0:
                    print(f"[DL] {line}", flush=True)
                for a in ses.pop_alerts():
                    msg = a.message()
                    if "error" in msg.lower() or "fail" in msg.lower():
                        print(f"[DL][alert] {type(a).__name__}: {msg}", flush=True)
                if s.is_seeding or s.progress >= 1.0:
                    break
                time.sleep(1)
                dl_tick += 1

            print(f"[DL] complete, removing torrent", flush=True)
            ses.remove_torrent(handle)
            self.done.emit(self.save_dir, rel_paths)
        except Exception as e:  # libtorrent может бросать boost-ошибки
            self.failed.emit(f"Ошибка libtorrent: {e}")


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
            for src in sources:
                rel = src.relative_to(self.src_dir)
                dst = Path(self.dst_dir) / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                size = src.stat().st_size
                if size <= self.chunk_size:
                    copied = self._stream_copy(src, dst, copied, total_bytes, f"копирую {rel.name}")
                    report.append(f"✓ {rel} ({human_bytes(size)})")
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

    def _split_copy(self, src: Path, dst: Path, copied: int, total: int) -> int:
        buf_size = 4 * 1024 * 1024
        part_idx = 0
        with open(src, "rb") as fin:
            while True:
                if self._cancel:
                    return part_idx
                part_name = f"{dst.name}.part{part_idx:03d}"
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

    def __init__(self, query: str):
        super().__init__()
        self.query = query

    def run(self):
        last_err = ""
        for base in MIRRORS:
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
        self._tmp_dir: str = ""
        self.settings = QSettings("TorFlash", "TorFlash")
        self._build_ui()
        self._apply_style()
        self._build_tray()

    # ---------- UI building ----------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 6)
        root.setSpacing(8)

        # Поисковая строка
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Запрос:"))
        self.input = QLineEdit()
        self.input.setPlaceholderText("Название фильма, игры, дистрибутива…")
        self.input.returnPressed.connect(self.start_search)
        search_row.addWidget(self.input, 1)
        self.search_btn = QPushButton("Искать")
        self.search_btn.setDefault(True)
        self.search_btn.clicked.connect(self.start_search)
        search_row.addWidget(self.search_btn)
        root.addLayout(search_row)

        # Папка назначения
        dst_row = QHBoxLayout()
        self.flash_check = QCheckBox("На флешку (Movies)")
        self.flash_check.setChecked(self._initial_use_flash)
        self.flash_check.setToolTip(
            "Включено: копировать на флешку в /Movies. "
            "Выключено: сохранять в ~/Storage."
        )
        self.flash_check.toggled.connect(self._on_flash_toggle)
        dst_row.addWidget(self.flash_check)
        self.dst_edit = QLineEdit(self.dst_dir)
        self.dst_edit.setReadOnly(True)
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
        root.addLayout(dst_row)

        # Сплиттер: список ← → детали
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_list())
        splitter.addWidget(self._build_detail())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([720, 520])
        root.addWidget(splitter, 1)

        self.setStatusBar(QStatusBar())

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

        # Действия
        actions = QHBoxLayout()
        actions.setSpacing(6)
        style = self.style()
        self.copy_btn = QPushButton("Magnet")
        self.copy_btn.setIcon(style.standardIcon(QStyle.SP_DialogSaveButton))
        self.copy_btn.setToolTip("Скопировать magnet-ссылку в буфер обмена")
        self.copy_btn.clicked.connect(self.copy_magnet)
        actions.addWidget(self.copy_btn)
        self.ktorrent_btn = QPushButton("KTorrent")
        self.ktorrent_btn.setIcon(style.standardIcon(QStyle.SP_MediaPlay))
        self.ktorrent_btn.clicked.connect(self.open_in_ktorrent)
        actions.addWidget(self.ktorrent_btn)
        self.page_btn = QPushButton("Страница")
        self.page_btn.setIcon(style.standardIcon(QStyle.SP_DirLinkIcon))
        self.page_btn.clicked.connect(self.open_page)
        actions.addWidget(self.page_btn)
        actions.addStretch()
        self.flash_btn = QPushButton("Скачать → на флешку")
        self.flash_btn.setObjectName("primaryBtn")
        self.flash_btn.setIcon(style.standardIcon(QStyle.SP_DriveHDIcon))
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
        self.cancel_btn.setIcon(style.standardIcon(QStyle.SP_DialogCancelButton))
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
        icon_path = Path(__file__).resolve().parent / "torflash.svg"
        if not icon_path.exists():
            # PyInstaller bundle: ресурс лежит в sys._MEIPASS
            mei = getattr(sys, "_MEIPASS", None)
            if mei:
                icon_path = Path(mei) / "torflash.svg"
        icon = QIcon(str(icon_path)) if icon_path.exists() else self.windowIcon()

        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip(APP_NAME)

        menu = QMenu(self)
        self.act_show = QAction("Показать", self)
        self.act_show.triggered.connect(self._tray_show)
        menu.addAction(self.act_show)
        menu.addSeparator()
        self.act_minimize = QAction("Сворачивать при закрытии", self, checkable=True)
        self.act_minimize.setChecked(
            self.settings.value("minimize_on_close", True, type=bool)
        )
        self.act_minimize.toggled.connect(self._on_minimize_toggle)
        menu.addAction(self.act_minimize)
        menu.addSeparator()
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

    def _on_minimize_toggle(self, checked: bool):
        self.settings.setValue("minimize_on_close", checked)

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
        self.search_btn.setEnabled(False)
        self.statusBar().showMessage("Поиск…")
        self._hide_banner()
        self.search_worker = SearchWorker(query)
        self.search_worker.done.connect(self._on_search_done)
        self.search_worker.failed.connect(self._on_search_failed)
        self.search_worker.start()

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
        if self.dl_result is not None:
            self._show_banner("Идёт загрузка — дождитесь завершения перед извлечением")
            return
        mount = detect_flash_mount()
        if not mount:
            self._show_banner("Флешка не смонтирована")
            return
        try:
            src = subprocess.run(
                ["findmnt", "-no", "SOURCE", mount],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            if not src:
                self._show_banner(f"Не удалось определить устройство для {mount}")
                return
            # /dev/sdb1 → /dev/sdb (parent disk)
            parent = re.sub(r"\d+$", "", src)
            subprocess.run(
                ["udisksctl", "unmount", "-b", src],
                check=True, capture_output=True, text=True,
            )
            subprocess.run(
                ["udisksctl", "power-off", "-b", parent],
                check=True, capture_output=True, text=True,
            )
            self._show_banner(f"Флешка извлечена ({src}) — можно вынимать", kind="info")
            # Переходим в режим ~/Storage
            self.flash_check.blockSignals(True)
            self.flash_check.setChecked(False)
            self.flash_check.blockSignals(False)
            self.dst_dir = str(Path.home() / "Storage")
            self.dst_edit.setText(self.dst_dir)
            self.statusBar().showMessage("Флешка безопасно извлечена", 5000)
        except subprocess.CalledProcessError as e:
            err = (e.stderr or "").strip() or str(e)
            self._show_banner(f"Не удалось извлечь: {err}")
        except FileNotFoundError as e:
            self._show_banner(f"Утилита не найдена: {e}")

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
        if self.dl_result is not None:
            self.statusBar().showMessage("Уже идёт другая загрузка", 3000)
            return
        try:
            Path(self.dst_dir).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._show_banner(f"Не удалось создать {self.dst_dir}: {e}")
            return
        if not os.access(self.dst_dir, os.W_OK):
            self._show_banner(f"Нет прав на запись в {self.dst_dir}")
            return

        self._hide_banner()
        self._tmp_dir = tempfile.mkdtemp(prefix="rutor-")
        self.dl_result = r
        self.dl_phase = "dl"
        self.dl_progress = (0, "Запуск…")
        self._refresh_progress_widget()
        self.progress_box.setVisible(True)
        self.flash_btn.setEnabled(False)

        self.dl_worker = DownloadWorker(r["magnet"], self._tmp_dir, r.get("torrent_url", ""))
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
        shutil.rmtree(self._tmp_dir, ignore_errors=True)
        self._reset_dl_state()
        if err == "Отменено":
            self.statusBar().showMessage("Загрузка отменена", 3000)
            return
        self._show_banner(f"Ошибка загрузки: {err}")

    def _on_dl_done(self, save_dir: str, rel_paths: list):
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
        shutil.rmtree(self._tmp_dir, ignore_errors=True)
        summary = " · ".join(line for line in report)
        self._reset_dl_state()
        self.statusBar().showMessage(f"Готово: {summary}", 8000)
        self._show_banner(
            "Готово! " + "; ".join(line.lstrip("✓✂ ").strip() for line in report),
            kind="info",
        )

    def _on_copy_failed(self, err: str):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)
        self._reset_dl_state()
        if err == "Отменено":
            self.statusBar().showMessage("Копирование отменено", 3000)
            return
        self._show_banner(f"Ошибка копирования: {err}")

    def _reset_dl_state(self):
        self.dl_result = None
        self.dl_worker = None
        self.copy_worker = None
        self.dl_phase = ""
        self.dl_progress = (0, "")
        self.progress_box.setVisible(False)
        self.flash_btn.setEnabled(True)

    # ---------- updater ----------

    def check_for_updates(self):
        if getattr(self, "update_checker", None) and self.update_checker.isRunning():
            return
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
        self.statusBar().showMessage(f"Установлена последняя версия (v{version})", 5000)

    def _on_update_check_failed(self, err: str):
        self.statusBar().showMessage(f"Проверка обновлений не удалась: {err}", 5000)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    icon_path = Path(__file__).parent / "torflash.svg"
    if icon_path.exists():
        from PyQt5.QtGui import QIcon
        app.setWindowIcon(QIcon(str(icon_path)))
    w = MainWindow()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
