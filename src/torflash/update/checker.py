"""TorFlash: проверка наличия обновления на GitHub (QThread)."""

import sys

import requests

from PyQt5.QtCore import QThread, pyqtSignal

from torflash.config import APP_NAME, APP_VERSION, GITHUB_REPO, _proxies
from torflash.i18n import _t
from torflash.helpers import _version_tuple
from torflash.update.assets import select_platform_asset


class UpdateChecker(QThread):
    found = pyqtSignal(str, str, str, str, str)  # version, asset_url, name, sha256_url, minisig_url
    up_to_date = pyqtSignal(str)        # current_version
    failed = pyqtSignal(str)

    def run(self):
        try:
            r = requests.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                headers={"Accept": "application/vnd.github+json", "User-Agent": APP_NAME},
                timeout=10, proxies=_proxies(),
            )
            r.raise_for_status()
            data = r.json()
            tag = data.get("tag_name", "").lstrip("v")
            if not tag:
                self.failed.emit(_t("Релиз без tag_name"))
                return
            if _version_tuple(tag) <= _version_tuple(APP_VERSION):
                self.up_to_date.emit(APP_VERSION)
                return
            assets = data.get("assets", [])
            url_by_name = {a.get("name", ""): a.get("browser_download_url", "") for a in assets}
            asset = select_platform_asset(assets, sys.platform)
            if asset is None:
                self.failed.emit(_t("Не найден бинарный asset в релизе"))
                return
            name = asset.get("name", "")
            sha_url = url_by_name.get(name + ".sha256", "")
            sig_url = url_by_name.get(name + ".minisig", "")
            self.found.emit(tag, asset["browser_download_url"], name, sha_url, sig_url)
        except requests.RequestException as e:
            self.failed.emit(_t("Сеть: {}").format(e))
        except (ValueError, KeyError) as e:
            self.failed.emit(_t("Ответ GitHub: {}").format(e))
