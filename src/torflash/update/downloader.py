"""TorFlash: скачивание и проверка обновления с верификацией SHA-256 (QThread)."""

import hashlib
import hmac
from pathlib import Path

import requests

from PyQt5.QtCore import QThread, pyqtSignal

from torflash.config import MINISIGN_PUBKEY, _proxies
from torflash.i18n import _t
from torflash.helpers import human_bytes, _sha256_from_sumfile
from torflash.update.signature import verify_minisign


class UpdateDownloader(QThread):
    progress = pyqtSignal(int, str)
    done = pyqtSignal(str)              # путь к новому бинарнику (.new)
    failed = pyqtSignal(str)

    def __init__(self, url: str, target_dir: str, sha256_url: str = "", minisig_url: str = ""):
        super().__init__()
        self.url = url
        self.target_dir = target_dir
        self.sha256_url = sha256_url
        self.minisig_url = minisig_url

    def run(self):
        try:
            # Целостность обязательна: качаем ожидаемый хэш ДО бинарника.
            # Без него ставить нельзя — иначе подменённый apt/CDN/прокси бинарник
            # будет запущен с правами пользователя (RCE).
            if not self.sha256_url:
                self.failed.emit(_t(
                    "В релизе нет контрольной суммы (.sha256) — установка "
                    "отменена. Обновитесь вручную с GitHub."
                ))
                return
            sr = requests.get(self.sha256_url, timeout=20, proxies=_proxies())
            sr.raise_for_status()
            expected = _sha256_from_sumfile(sr.text)
            if not expected:
                self.failed.emit(_t("Не удалось прочитать контрольную сумму релиза"))
                return

            target = Path(self.target_dir) / "TorFlash.new"
            r = requests.get(self.url, stream=True, timeout=60, proxies=_proxies())
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            written = 0
            digest = hashlib.sha256()
            with open(target, "wb") as f:
                for chunk in r.iter_content(chunk_size=128 * 1024):
                    if chunk:
                        f.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)
                        pct = int(written * 100 / total) if total else 0
                        self.progress.emit(
                            pct,
                            _t("Загрузка обновления: {}").format(
                                human_bytes(written)
                                + (f"/{human_bytes(total)}" if total else "")
                            ),
                        )
            if not hmac.compare_digest(digest.hexdigest(), expected):
                target.unlink(missing_ok=True)
                self.failed.emit(_t(
                    "Контрольная сумма не совпала — файл повреждён или подменён. "
                    "Установка отменена."
                ))
                return

            # Асимметричная подпись (если включён публичный ключ): защищает даже
            # от компрометации релиза — sha256 рядом с бинарником подменить можно,
            # подпись без приватного ключа — нет. Включается при заполнении
            # MINISIGN_PUBKEY; пусто = остаёмся на одной SHA-256.
            if MINISIGN_PUBKEY:
                if not self.minisig_url:
                    target.unlink(missing_ok=True)
                    self.failed.emit(_t("Нет подписи (.minisig) в релизе — установка отменена."))
                    return
                try:
                    msr = requests.get(self.minisig_url, timeout=20, proxies=_proxies())
                    msr.raise_for_status()
                except requests.RequestException as e:
                    target.unlink(missing_ok=True)
                    self.failed.emit(_t("Не удалось скачать подпись: {}").format(e))
                    return
                if not verify_minisign(target.read_bytes(), msr.text, MINISIGN_PUBKEY):
                    target.unlink(missing_ok=True)
                    self.failed.emit(_t(
                        "Подпись неверна — файл подменён или ключ не совпал. "
                        "Установка отменена."
                    ))
                    return

            target.chmod(0o755)
            self.done.emit(str(target))
        except requests.RequestException as e:
            self.failed.emit(_t("Сеть: {}").format(e))
        except OSError as e:
            self.failed.emit(_t("Запись на диск: {}").format(e))
