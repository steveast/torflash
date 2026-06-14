"""TorFlash: фоновая загрузка картинки постера (QThread)."""

import time

import requests

from PyQt5.QtCore import QThread, pyqtSignal

from torflash.config import HEADERS, _proxies


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
                r = requests.get(self.url, headers=headers, timeout=10, proxies=_proxies())
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
