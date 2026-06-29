"""TorFlash: фоновое скачивание .torrent-файла на диск (QThread)."""

import requests
from PyQt5.QtCore import QThread, pyqtSignal

from torflash.config import HEADERS, _proxies
from torflash.i18n import _t


class TorrentFileFetcher(QThread):
    """Скачивает .torrent-файл по torrent_url и сохраняет в указанный путь.

    Для авторизованных трекеров (RuTracker) cookies сессии передаём явно —
    иначе dl.php вернёт HTML-страницу логина вместо .torrent."""

    done = pyqtSignal(str)     # сохранённый путь
    failed = pyqtSignal(str)   # текст ошибки

    def __init__(self, torrent_url: str, dest_path: str, cookies=None):
        super().__init__()
        self.torrent_url = torrent_url
        self.dest_path = dest_path
        self.cookies = cookies

    def run(self):
        try:
            r = requests.get(
                self.torrent_url, headers=HEADERS, timeout=20,
                allow_redirects=True, cookies=self.cookies, proxies=_proxies(),
            )
            r.raise_for_status()
            data = r.content
            # Валидный .torrent — bencoded-словарь, начинается с b"d". Если трекер
            # отдал HTML (нужен логин/каптча), не сохраняем мусор под видом торрента.
            if not data[:1] == b"d":
                self.failed.emit(_t("Сервер вернул не .torrent (нужна авторизация?)"))
                return
            with open(self.dest_path, "wb") as f:
                f.write(data)
            self.done.emit(self.dest_path)
        except (requests.RequestException, OSError) as e:
            self.failed.emit(str(e))
