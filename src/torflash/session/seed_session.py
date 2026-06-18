"""TorFlash: постоянная libtorrent-сессия (хранит библиотеку, сидирует)."""

import json
import os
import shutil
import socket
import threading
import time
from pathlib import Path

import requests

from torflash.config import (
    HEADERS, EXTRA_TRACKERS, LIBRARY_DIR, TORRENTS_CACHE_DIR, RESUME_DIR,
    LIBRARY_FILE, STORAGE_DEFAULT, STATS_FILE, _proxies,
    DEFAULT_LISTEN_PORT, LISTEN_PORT_SPAN,
)
from torflash.i18n import _t, DL_STATES
from torflash.helpers import _safe_join


def _port_available(port: int) -> bool:
    """True, если на 0.0.0.0:port свободны и TCP, и UDP. На Windows занятый
    другим клиентом порт даёт WSAEACCES/WSAEADDRINUSE — ловим как OSError.
    SO_EXCLUSIVEADDRUSE гарантирует, что не сочтём свободным порт, который
    кто-то уже держит эксклюзивно (как делает uTorrent)."""
    for sock_type in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
        s = socket.socket(socket.AF_INET, sock_type)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind(("0.0.0.0", port))
        except OSError:
            return False
        finally:
            s.close()
    return True


def _pick_listen_port() -> int:
    """Первый свободный порт начиная с DEFAULT_LISTEN_PORT; если все заняты —
    0, чтобы ОС выбрала эфемерный (закачка/DHT работают и так)."""
    for port in range(DEFAULT_LISTEN_PORT, DEFAULT_LISTEN_PORT + LISTEN_PORT_SPAN):
        if _port_available(port):
            return port
    return 0


class SeedSession:
    """Постоянная libtorrent-сессия. Хранит библиотеку, переустанавливает торренты на старте."""

    def __init__(self):
        import libtorrent as lt
        self.lt = lt
        # Реентрантный лок: handles/library/stats читаются UI-таймерами и
        # одновременно мутируются из DownloadWorker-потока.
        self._lock = threading.RLock()
        LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
        TORRENTS_CACHE_DIR.mkdir(exist_ok=True)
        RESUME_DIR.mkdir(exist_ok=True)
        STORAGE_DEFAULT.mkdir(parents=True, exist_ok=True)
        self.stats = self._load_stats()
        self._prev_session_dl = 0
        self._prev_session_ul = 0
        # Предупреждение для UI, если не удалось занять желаемый порт.
        self.listen_warning: str = ""
        port = _pick_listen_port()
        if port == 0:
            self.listen_warning = _t(
                "Порты {}–{} заняты (другой торрент-клиент?). "
                "Слушаю случайный порт — входящие соединения могут не работать."
            ).format(DEFAULT_LISTEN_PORT, DEFAULT_LISTEN_PORT + LISTEN_PORT_SPAN - 1)
            print(f"[seed] WARN: {self.listen_warning}", flush=True)
        elif port != DEFAULT_LISTEN_PORT:
            self.listen_warning = _t(
                "Порт {} занят (другой торрент-клиент?) — слушаю {}."
            ).format(DEFAULT_LISTEN_PORT, port)
            print(f"[seed] WARN: {self.listen_warning}", flush=True)
        self.ses = lt.session({
            "listen_interfaces": f"0.0.0.0:{port}",
            # libtorrent 2.0 по умолчанию мапит в память каждый файл крупнее
            # mmap_file_size_cutoff*16КиБ (по умолчанию 40 = 640 КиБ). При
            # сидировании это раздувает RSS на полный размер всех раздач (у нас
            # доходило до 5+ ГБ). Поднимаем порог выше любого файла, чтобы диск
            # шёл через обычные pread/pwrite + внутренний кэш (cache_size).
            "mmap_file_size_cutoff": 2147483647,
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

    @staticmethod
    def _atomic_write(path: Path, text: str):
        """Запись через временный файл + os.replace — иначе крах/полный диск
        посреди write_text оставляет обрезанный JSON и теряет всю библиотеку."""
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def _save_library(self):
        try:
            with self._lock:
                data = json.dumps(self.library, ensure_ascii=False, indent=2)
            self._atomic_write(LIBRARY_FILE, data)
        except OSError as e:
            print(f"[seed] failed to save library: {e}", flush=True)

    def set_pending_flash(self, info_hash: str, value: bool = True):
        with self._lock:
            changed = info_hash in self.library
            if changed:
                self.library[info_hash]["pending_flash_copy"] = value
        if changed:
            self._save_library()

    def clear_pending_flash(self, info_hash: str):
        with self._lock:
            changed = info_hash in self.library and "pending_flash_copy" in self.library[info_hash]
            if changed:
                self.library[info_hash].pop("pending_flash_copy", None)
        if changed:
            self._save_library()

    def mark_completed(self, info_hash: str):
        with self._lock:
            changed = info_hash in self.library
            if changed:
                self.library[info_hash]["completed_at"] = time.time()
        if changed:
            self._save_library()

    def get_handle(self, info_hash: str):
        with self._lock:
            return self.handles.get(info_hash)

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

    def add(self, magnet: str, torrent_url: str, save_path: str, cookies=None):
        params = None
        torrent_bytes = None
        if torrent_url:
            try:
                r = requests.get(
                    torrent_url, headers=HEADERS, timeout=20,
                    allow_redirects=True, cookies=cookies, proxies=_proxies(),
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
        with self._lock:
            self.handles[info_hash] = handle
            is_new = info_hash not in self.library
            if is_new:
                self.library[info_hash] = {
                    "hash": info_hash,
                    "title": params.ti.name() if params.ti else _t("(получение метаданных…)"),
                    "size": params.ti.total_size() if params.ti else 0,
                    "magnet": magnet,
                    "torrent_url": torrent_url,
                    "save_path": save_path,
                    "added_at": time.time(),
                    "completed_at": None,
                }
        if is_new:
            if torrent_bytes:
                try:
                    (TORRENTS_CACHE_DIR / f"{info_hash}.torrent").write_bytes(torrent_bytes)
                except OSError:
                    pass
            self._save_library()
        return info_hash

    def update_metadata(self, info_hash: str):
        h = self.get_handle(info_hash)
        if not h or not h.status().has_metadata:
            return
        info = h.torrent_file()
        with self._lock:
            present = info_hash in self.library
            if present:
                self.library[info_hash]["title"] = info.name()
                self.library[info_hash]["size"] = info.total_size()
        if present:
            tfile = TORRENTS_CACHE_DIR / f"{info_hash}.torrent"
            if not tfile.exists():
                try:
                    ct = self.lt.create_torrent(info)
                    tfile.write_bytes(self.lt.bencode(ct.generate()))
                except (RuntimeError, OSError) as e:
                    print(f"[seed] dump .torrent failed: {e}", flush=True)
            self._save_library()

    def remove(self, info_hash: str, delete_files: bool = False):
        with self._lock:
            h = self.handles.pop(info_hash, None)
            meta = self.library.pop(info_hash, None)
        if h:
            try:
                self.ses.remove_torrent(h, 1 if delete_files else 0)
            except RuntimeError:
                pass
        if delete_files and meta:
            # libtorrent's option=1 удалит payload. На всякий — подчистим пустую папку.
            save_path = meta.get("save_path", str(STORAGE_DEFAULT))
            tfile = TORRENTS_CACHE_DIR / f"{info_hash}.torrent"
            if tfile.exists():
                try:
                    ti = self.lt.torrent_info(self.lt.bdecode(tfile.read_bytes()))
                    # ti.name() из недоверенного .torrent — проверяем containment,
                    # чтобы вредоносное имя ("../../..") не увело rmtree из save_path.
                    target = _safe_join(save_path, ti.name())
                    if target and target.exists():
                        if target.is_dir():
                            shutil.rmtree(target, ignore_errors=True)
                        else:
                            target.unlink(missing_ok=True)
                    elif target is None:
                        print(f"[seed] refusing unsafe delete for {info_hash[:8]}", flush=True)
                except (RuntimeError, OSError, ValueError) as e:
                    print(f"[seed] cleanup failed for {info_hash[:8]}: {e}", flush=True)
        (TORRENTS_CACHE_DIR / f"{info_hash}.torrent").unlink(missing_ok=True)
        (RESUME_DIR / f"{info_hash}.dat").unlink(missing_ok=True)
        self._save_library()

    def get_status(self, info_hash: str):
        with self._lock:
            h = self.handles.get(info_hash)
            meta = dict(self.library.get(info_hash, {}))
        if not h:
            return None
        s = h.status()
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
            "added_at": meta.get("added_at", 0),
            "completed_at": meta.get("completed_at", 0),
            "active_time": getattr(s, "active_time", 0),
            "seeding_time": getattr(s, "seeding_time", 0),
        }

    def all_statuses(self) -> list:
        with self._lock:
            hids = list(self.handles)
        return [s for s in (self.get_status(h) for h in hids) if s]

    def drain_alerts(self):
        for a in self.ses.pop_alerts():
            if isinstance(a, self.lt.save_resume_data_alert):
                try:
                    hid = self._hash_str(a.handle)
                    buf = self.lt.write_resume_data_buf(a.params)
                    (RESUME_DIR / f"{hid}.dat").write_bytes(buf)
                except (RuntimeError, OSError) as e:
                    print(f"[seed] write resume failed: {e}", flush=True)
            elif isinstance(a, self.lt.udp_error_alert):
                # Шум: пиры за NAT отвечают ICMP "port unreachable" на uTP-пакеты.
                # На закачку не влияет, но забивает лог сотнями строк — пропускаем.
                continue
            else:
                msg = a.message()
                low = msg.lower()
                if "error" in low or "fail" in low:
                    print(f"[seed][alert] {type(a).__name__}: {msg}", flush=True)

    def request_save_resume_all(self):
        with self._lock:
            handles = list(self.handles.values())
        for h in handles:
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

    def _load_stats(self) -> dict:
        default = {"total_downloaded": 0, "total_uploaded": 0, "daily": {}}
        if STATS_FILE.exists():
            try:
                data = json.loads(STATS_FILE.read_text())
                if "daily" not in data:
                    data["daily"] = {}
                return data
            except (json.JSONDecodeError, OSError):
                return default
        return default

    def _save_stats(self):
        try:
            with self._lock:
                data = json.dumps(self.stats, indent=2)
            self._atomic_write(STATS_FILE, data)
        except OSError:
            pass

    def update_stats(self):
        """Call periodically to accumulate session stats."""
        with self._lock:
            handles = list(self.handles.values())
        dl = 0
        ul = 0
        for h in handles:
            if h.is_valid():
                s = h.status()
                dl += s.total_payload_download
                ul += s.total_payload_upload
        with self._lock:
            delta_dl = max(0, dl - self._prev_session_dl)
            delta_ul = max(0, ul - self._prev_session_ul)
            self._prev_session_dl = dl
            self._prev_session_ul = ul
            if delta_dl <= 0 and delta_ul <= 0:
                return
            self.stats["total_downloaded"] += delta_dl
            self.stats["total_uploaded"] += delta_ul
            # Дневная статистика
            today = time.strftime("%Y-%m-%d")
            daily = self.stats.setdefault("daily", {})
            day = daily.setdefault(today, {"dl": 0, "ul": 0})
            day["dl"] += delta_dl
            day["ul"] += delta_ul
            # Храним только последние 90 дней
            if len(daily) > 90:
                for old_key in sorted(daily.keys())[:-90]:
                    del daily[old_key]
        self._save_stats()

    def shutdown(self):
        self.update_stats()
        self.request_save_resume_all()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.drain_alerts()
            time.sleep(0.2)
