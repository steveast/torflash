"""NoNaMe-Club (nnmclub.to) — RU торрент-трекер. Поиск открыт без логина,
скачивание .torrent через download.php?id=N (302 на bulk.nnmclub.to).
Magnet-ссылок в листинге нет — отдаём только torrent_url, libtorrent качает."""

import re
import time
from html import unescape
from urllib.parse import quote

import requests

from .base import Provider, HEADERS

BASE = "https://nnmclub.to/forum"

# Логические категории — используем те же id, что и у Rutor (Rutor canonical),
# чтобы один combo в UI работал для всех провайдеров. NNM-форумы маппим внутри.
# Список форумов NNM огромный (300+), маппим только то, что совпадает с rutor-категориями.
# Если категория не замаплена — ищем без фильтра по форуму.
_RUTOR_TO_NNM_FORUM = {
    1: 1259,   # Зарубежные фильмы (HD/FHD/UHD)
    5: 227,    # Наше кино HD
    4: 1382,   # Зарубежные сериалы
    16: 211,   # Наши сериалы
    7: 1339,   # Зарубежные мультфильмы (HD) — rutor использует это для всех мультов
    17: 1339,  # Зарубежные мультфильмы
    9: 235,    # Аниме
    14: 664,   # Документальные
}

# Парсим строки таблицы трекера: <tr class="prow1|prow2">…</tr>
ROW_RE = re.compile(r'<tr class="prow[12]">(.*?)</tr>', re.S)
TITLE_RE = re.compile(
    r'<a class="genmed topictitle" href="(viewtopic\.php\?t=\d+)">'
    r'<b>(.*?)</b></a>',
    re.S,
)
DL_RE = re.compile(r'href="(download\.php\?id=\d+)"')
SIZE_RE = re.compile(r'<u>(\d+)</u>\s*([\d.,]+\s*[KMGT]?B)', re.I)
# seeds/leech: <td ... class="seedmed"><b>N</b></td>
SEED_RE = re.compile(r'class="seedmed"><b>(\d+)</b>')
LEECH_RE = re.compile(r'class="leechmed"><b>(\d+)</b>')
# Дата: <u>UNIX_TS</u> DD-MM-YYYY<br>HH:MM (берём unix-таймштамп — отсортируется
# нормально как строка YYYY-MM-DD).
DATE_RE = re.compile(r'title="Торрент-файл добавлен"[^>]*><u>(\d+)</u>')
TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(html: str) -> str:
    return unescape(TAG_RE.sub("", html).replace("\xa0", " ")).strip()


def _parse(html: str) -> list[dict]:
    results = []
    for row in ROW_RE.findall(html):
        title_m = TITLE_RE.search(row)
        dl_m = DL_RE.search(row)
        if not (title_m and dl_m):
            continue
        size_m = SIZE_RE.search(row)
        seed_m = SEED_RE.search(row)
        leech_m = LEECH_RE.search(row)
        date_m = DATE_RE.search(row)
        title = _strip_tags(title_m.group(2))
        page_url = f"{BASE}/{title_m.group(1)}"
        torrent_url = f"{BASE}/{dl_m.group(1)}"
        size_text = _strip_tags(size_m.group(2)) if size_m else ""
        if date_m:
            ts = int(date_m.group(1))
            date_text = time.strftime("%Y-%m-%d", time.localtime(ts))
        else:
            date_text = ""
        results.append({
            "provider": "nnm",
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


class NnmProvider(Provider):
    name = "nnm"
    display_name = "NoNaMe-Club"

    def search(self, query: str, category: int = 0, timeout: float = 10) -> list[dict]:
        params = {"nm": query}
        native = _RUTOR_TO_NNM_FORUM.get(category)
        if native:
            params["f"] = str(native)
        url = f"{BASE}/tracker.php"
        r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return _parse(r.text)
