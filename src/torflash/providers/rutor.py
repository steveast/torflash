"""Rutor.info — RU торрент-трекер. Несколько зеркал, magnet прямо в листинге."""

import re
from html import unescape
from urllib.parse import quote

import requests

from .base import Provider, HEADERS

MIRRORS = [
    "https://rutor.info",
    "https://rutor.is",
    "http://rutor.org",
]

# Категории rutor.info: id → название. URL: /search/0/<cat>/000/0/<query>
CATEGORIES = [
    (0, "Все"),
    (1, "Зарубежные фильмы"),
    (5, "Наше кино"),
    (4, "Зарубежные сериалы"),
    (16, "Наши сериалы"),
    (7, "Мультфильмы"),
    (8, "Игры"),
    (9, "Аниме"),
    (10, "Музыка"),
    (11, "Книги"),
    (12, "Спорт и здоровье"),
    (13, "Юмор"),
    (14, "Документальные"),
    (15, "Софт"),
    (17, "Зарубежные мультфильмы"),
]

ROW_RE = re.compile(r"<tr[^>]*class=['\"]?(?:gai|tum)['\"]?[^>]*>(.*?)</tr>", re.S)
CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
MAGNET_RE = re.compile(r'href="(magnet:\?[^"]+)"')
PAGE_RE = re.compile(r'href="(/torrent/\d+[^"]*)"')
TITLE_RE = re.compile(r'<a href="/torrent/\d+[^"]*">(.*?)</a>', re.S)
SEED_RE = re.compile(r'<span class="green">.*?(\d+)\s*</span>', re.S)
LEECH_RE = re.compile(r'<span class="red">[^<\d]*(\d+)\s*</span>', re.S)
DOWNLOAD_RE = re.compile(
    r'href="(?P<u>(?://|https?://)d\.rutor\.[^"]+/download/\d+[^"]*)"'
)
TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(html: str) -> str:
    return unescape(TAG_RE.sub("", html).replace("\xa0", " ")).strip()


def _parse(html: str, base: str) -> list[dict]:
    results = []
    for row in ROW_RE.findall(html):
        cells = CELL_RE.findall(row)
        if len(cells) < 3:
            continue
        magnet_m = MAGNET_RE.search(row)
        title_m = TITLE_RE.search(row)
        page_m = PAGE_RE.search(row)
        if not (magnet_m and title_m):
            continue
        seed_m = SEED_RE.search(cells[-1])
        leech_m = LEECH_RE.search(cells[-1])
        dl_m = DOWNLOAD_RE.search(row)
        dl_url = ""
        if dl_m:
            dl_url = dl_m.group("u")
            if dl_url.startswith("//"):
                dl_url = "https:" + dl_url
        results.append({
            "provider": "rutor",
            "date": _strip_tags(cells[0]),
            "title": _strip_tags(title_m.group(1)),
            "size": _strip_tags(cells[-2]),
            "seeds": seed_m.group(1) if seed_m else "0",
            "leech": leech_m.group(1) if leech_m else "0",
            "magnet": magnet_m.group(1),
            "torrent_url": dl_url,
            "page": base + page_m.group(1) if page_m else "",
        })
    return results


class RutorProvider(Provider):
    name = "rutor"
    display_name = "Rutor"
    categories = CATEGORIES

    def search(self, query: str, category: int = 0, timeout: float = 10, proxy: str = "") -> list[dict]:
        last_err = None
        proxies = {"http": proxy, "https": proxy} if proxy else {}
        for base in MIRRORS:
            if category:
                url = f"{base}/search/0/{category}/000/0/{quote(query)}"
            else:
                url = f"{base}/search/{quote(query)}"
            try:
                r = requests.get(url, headers=HEADERS, timeout=timeout, proxies=proxies)
                r.raise_for_status()
                return _parse(r.text, base)
            except requests.RequestException as e:
                last_err = e
        if last_err:
            raise last_err
        return []
