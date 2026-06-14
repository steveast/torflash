"""TorFlash: фоновое получение детальной инфы о торренте (QThread)."""

from PyQt5.QtCore import QThread, pyqtSignal

from torflash.config import current_proxy


class MetaFetcher(QThread):
    """Фоновое получение детальной инфы о торренте с rutor.info."""

    fetched = pyqtSignal(str, dict)  # url, details

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        try:
            from torflash.meta import fetch_torrent_details
        except ImportError as e:
            print(f"[meta] rutor_meta module missing: {e}", flush=True)
            return
        try:
            data = fetch_torrent_details(self.url, proxy=current_proxy())
            self.fetched.emit(self.url, data)
        except Exception as e:
            print(f"[meta] fetch failed: {e}", flush=True)
