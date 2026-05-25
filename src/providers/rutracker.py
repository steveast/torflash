"""RuTracker.org — крупнейший русскоязычный трекер. Требует авторизации.
Магнет-ссылки доступны на странице раздачи, в листинге поиска — только .torrent."""

import re
import time
from html import unescape

import requests

from .base import Provider, HEADERS

MIRRORS = [
    "https://rutracker.net/forum",
    "https://rutracker.org/forum",
]

# Маппинг rutor canonical id → rutracker forum id.
# Руторовские id — это «общий язык» для всех провайдеров в TorFlash.
_RUTOR_TO_RT_FORUM = {
    1: 2198,   # Зарубежные фильмы (HD Video)
    5: 22,     # Наше кино
    4: 2366,   # Зарубежные сериалы (HD)
    16: 9,     # Наши сериалы
    7: 2343,   # Мультфильмы (HD)
    17: 2343,  # Зарубежные мультфильмы
    8: 5,      # Игры для Windows
    9: 33,     # Аниме
    10: 409,   # Музыка (lossless)
    11: 2157,  # Книги
    14: 670,   # Документальные (HD)
    15: 1014,  # Софт (Linux, Windows)
}

ROW_RE = re.compile(
    r'<tr\s+class="tCenter\s+hl-tr"[^>]*>(.*?)</tr>', re.S
)
TITLE_RE = re.compile(
    r'<a[^>]+data-topic_id="(\d+)"[^>]*class="[^"]*tt-text[^"]*"[^>]*>(.*?)</a>',
    re.S,
)
DL_RE = re.compile(r'<a[^>]+href="dl\.php\?t=(\d+)"')
SIZE_RE = re.compile(
    r'<a[^>]+href="dl\.php\?t=\d+"[^>]*>(\d[\d.,]*)\s*(TB|GB|MB|KB|B)</a>',
    re.I,
)
SEED_RE = re.compile(r'<b class="seedmed[^"]*">(\d+)</b>')
LEECH_RE = re.compile(r'<b class="leechmed[^"]*">(\d+)</b>')
DATE_RE = re.compile(r'data-ts_text="(\d+)"')
TAG_RE = re.compile(r"<[^>]+>")
MAGNET_RE = re.compile(r'href="(magnet:\?[^"]+)"')


def _strip_tags(html: str) -> str:
    return unescape(TAG_RE.sub("", html).replace("\xa0", " ")).strip()


def _parse(html: str, base: str) -> list[dict]:
    results = []
    for row in ROW_RE.findall(html):
        title_m = TITLE_RE.search(row)
        if not title_m:
            continue
        topic_id = title_m.group(1)
        title = _strip_tags(title_m.group(2))
        dl_m = DL_RE.search(row)
        torrent_url = f"{base}/dl.php?t={topic_id}" if dl_m else ""
        size_m = SIZE_RE.search(row)
        size_text = f"{size_m.group(1)} {size_m.group(2)}" if size_m else ""
        seed_m = SEED_RE.search(row)
        leech_m = LEECH_RE.search(row)
        date_m = DATE_RE.search(row)
        if date_m:
            ts = int(date_m.group(1))
            date_text = time.strftime("%Y-%m-%d", time.localtime(ts))
        else:
            date_text = ""
        page_url = f"{base}/viewtopic.php?t={topic_id}"
        results.append({
            "provider": "rutracker",
            "date": date_text,
            "title": title,
            "size": size_text,
            "seeds": seed_m.group(1) if seed_m else "0",
            "leech": leech_m.group(1) if leech_m else "0",
            "magnet": "",
            "torrent_url": torrent_url,
            "page": page_url,
        })
    return results


class RuTrackerProvider(Provider):
    name = "rutracker"
    display_name = "RuTracker"

    def __init__(self):
        self._session: requests.Session | None = None
        self._base: str = MIRRORS[0]
        self._username: str = ""
        self._password: str = ""
        self._proxy: str = ""

    def set_credentials(self, username: str, password: str, proxy: str = ""):
        """Обновить логин/пароль/прокси. Сбрасываем сессию при изменении."""
        if username != self._username or password != self._password or proxy != self._proxy:
            self._session = None
        self._username = username
        self._password = password
        self._proxy = proxy

    @property
    def configured(self) -> bool:
        return bool(self._username and self._password)

    def _login(self, timeout: float) -> requests.Session:
        if self._session is not None:
            return self._session
        s = requests.Session()
        s.headers.update(HEADERS)
        if self._proxy:
            s.proxies = {"http": self._proxy, "https": self._proxy}
        data = {
            "login_username": self._username,
            "login_password": self._password,
            "login": "Вход",
        }
        last_err = None
        for base in MIRRORS:
            try:
                r = s.post(f"{base}/login.php", data=data, timeout=timeout)
                r.raise_for_status()
                if any(c.name.startswith("bb_") for c in s.cookies):
                    self._session = s
                    self._base = base
                    return s
            except requests.RequestException as e:
                last_err = e
        if last_err:
            raise RuntimeError(f"RuTracker login failed: {last_err}")
        raise RuntimeError("Не удалось войти в RuTracker — проверьте логин/пароль")

    def search(self, query: str, category: int = 0, timeout: float = 10) -> list[dict]:
        if not self.configured:
            raise RuntimeError("RuTracker: не заданы логин/пароль (Настройки)")
        s = self._login(timeout)
        params: dict = {"nm": query}
        native = _RUTOR_TO_RT_FORUM.get(category)
        if native:
            params["f"] = str(native)
        r = s.post(f"{self._base}/tracker.php", data=params, timeout=timeout)
        r.raise_for_status()
        return _parse(r.text, self._base)

    def fetch_magnet(self, page_url: str, timeout: float = 10) -> str:
        """Получить magnet-ссылку со страницы раздачи."""
        if not self._session:
            self._login(timeout)
        r = self._session.get(page_url, timeout=timeout)
        r.raise_for_status()
        m = MAGNET_RE.search(r.text)
        return m.group(1) if m else ""
