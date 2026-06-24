"""TorFlash: фоновое получение детальной инфы о торренте (QThread)."""

from PyQt5.QtCore import QThread, pyqtSignal

from torflash.config import current_proxy


# phpBB-форумы (NNM/RuTracker) лениво грузят постер/скриншоты через <var>-теги,
# поэтому требуют отдельного парсера — rutor-скрапер цепляет на них chrome форума.
_PHPBB_PROVIDERS = {"nnm", "rutracker"}


class MetaFetcher(QThread):
    """Фоновое получение детальной инфы о торренте со страницы раздачи.

    Парсер выбирается по провайдеру: rutor.info vs phpBB-форумы (NNM/RuTracker)."""

    fetched = pyqtSignal(str, dict)  # url, details

    def __init__(self, url: str, provider: str = ""):
        super().__init__()
        self.url = url
        self.provider = provider

    def run(self):
        try:
            if self.provider in _PHPBB_PROVIDERS:
                from torflash.meta_phpbb import fetch_details
            else:
                from torflash.meta import fetch_torrent_details as fetch_details
        except ImportError as e:
            print(f"[meta] parser module missing: {e}", flush=True)
            return
        try:
            data = fetch_details(self.url, proxy=current_proxy())
            self.fetched.emit(self.url, data)
        except Exception as e:
            print(f"[meta] fetch failed: {e}", flush=True)
