"""TorFlash: воркер поиска по одному провайдеру (QThread)."""

import requests

from PyQt5.QtCore import QThread, pyqtSignal

from torflash.config import current_proxy


class SearchWorker(QThread):
    """Один воркер на один провайдер. MainWindow запускает по одному на каждый
    включённый источник и мерджит результаты по мере прихода."""

    done = pyqtSignal(str, list)      # provider_name, results
    failed = pyqtSignal(str, str)     # provider_name, error_message

    def __init__(self, provider, query: str, category: int = 0):
        super().__init__()
        self.provider = provider
        self.query = query
        self.category = category

    def run(self):
        try:
            results = self.provider.search(self.query, self.category, proxy=current_proxy())
            self.done.emit(self.provider.name, results)
        except requests.RequestException as e:
            self.failed.emit(self.provider.name, str(e))
        except Exception as e:
            self.failed.emit(self.provider.name, f"{type(e).__name__}: {e}")
