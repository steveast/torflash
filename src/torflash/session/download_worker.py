"""TorFlash: воркер скачивания одного торрента (QThread)."""

import time
import traceback

from PyQt5.QtCore import QThread, pyqtSignal

from torflash.session.seed_session import SeedSession
from torflash.i18n import _t, DL_STATES
from torflash.helpers import human_bytes, fmt_time


class DownloadWorker(QThread):
    progress = pyqtSignal(int, str)
    done = pyqtSignal(str, list, str)   # save_dir, rel_paths, info_hash (остаётся в seed)
    failed = pyqtSignal(str)

    def __init__(self, seed: SeedSession, magnet: str, save_dir: str,
                 torrent_url: str = "", mark_pending_flash: bool = False,
                 cookies=None):
        super().__init__()
        self.seed = seed
        self.magnet = magnet
        self.torrent_url = torrent_url
        self.save_dir = save_dir
        self.mark_pending_flash = mark_pending_flash
        self.cookies = cookies
        self._cancel = False
        self._stop = False
        self.info_hash = ""

    def cancel(self):
        """Отмена пользователем — удаляет частичную закачку."""
        self._cancel = True

    def stop(self):
        """Мягкая остановка при выходе из приложения — НЕ удаляет файлы,
        торрент остаётся в сессии и доскачается при следующем запуске."""
        self._stop = True

    def run(self):
        try:
            print(f"[DL] start save_dir={self.save_dir}", flush=True)
            self.info_hash = self.seed.add(self.magnet, self.torrent_url, self.save_dir,
                                           cookies=self.cookies)
            handle = self.seed.get_handle(self.info_hash)
            if handle is None:
                self.failed.emit(_t("Ошибка: торрент не добавлен"))
                return
            if self.mark_pending_flash:
                self.seed.set_pending_flash(self.info_hash, True)
            print(f"[DL] hash={self.info_hash[:8]} pending_flash={self.mark_pending_flash}", flush=True)

            self.progress.emit(0, _t("Получение метаданных…"))
            meta_deadline = time.monotonic() + 180
            while not handle.status().has_metadata:
                if self._stop:
                    print("[DL] stop requested — leaving torrent in session", flush=True)
                    return
                if self._cancel:
                    self.seed.remove(self.info_hash, delete_files=True)
                    self.failed.emit(_t("Отменено"))
                    return
                if time.monotonic() > meta_deadline:
                    s = handle.status()
                    self.seed.remove(self.info_hash, delete_files=True)
                    self.failed.emit(
                        _t("Метаданные не получены за 3 мин (пиров: {})").format(s.num_peers)
                    )
                    return
                s = handle.status()
                self.progress.emit(0, _t("Получение метаданных… пиров: {}").format(s.num_peers))
                time.sleep(1)
            self.seed.update_metadata(self.info_hash)

            info = handle.torrent_file()
            total = info.total_size()
            files = info.files()
            rel_paths = [files.file_path(i) for i in range(files.num_files())]

            dl_start = time.monotonic()
            tick = 0
            while True:
                if self._stop:
                    print("[DL] stop requested — leaving torrent in session", flush=True)
                    return
                if self._cancel:
                    self.seed.remove(self.info_hash, delete_files=True)
                    self.failed.emit(_t("Отменено"))
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
                    f"· ↓ {human_bytes(s.download_rate)}/s · {_t('пиров:')} {s.num_peers} "
                    f"· ETA {fmt_time(eta_s)} · {_t('прошло')} {fmt_time(elapsed)}"
                )
                self.progress.emit(pct, line)
                if tick % 5 == 0:
                    print(f"[DL] {line}", flush=True)
                if s.is_seeding or s.progress >= 1.0:
                    break
                time.sleep(1)
                tick += 1

            print("[DL] complete, kept in seed session", flush=True)
            self.seed.mark_completed(self.info_hash)
            self.done.emit(self.save_dir, rel_paths, self.info_hash)
        except Exception as e:
            print(f"[DL] FAILED save_dir={self.save_dir} hash={self.info_hash[:8] if self.info_hash else '-'}\n{traceback.format_exc()}", flush=True)
            self.failed.emit(_t("Ошибка: {}").format(e))
