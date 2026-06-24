"""Fetch and parse phpBB-based tracker detail pages (NoNaMe-Club, RuTracker).

Unlike rutor, these forums lazy-load content images via
``<var class="postImg" title="URL">`` tags (JS swaps them to ``<img>`` on the
client). The real poster and screenshots live there; the only ``<img>`` tags in
the raw HTML are site chrome (RSS button, smilies, seasonal banners, avatars).
Applying the rutor ``<img>`` scraper here scoops up that junk instead — hence a
dedicated parser. Pure stdlib + requests, no Qt.
"""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, urlparse

import requests

from torflash.meta import (
    HEADERS,
    _extract_magnet_and_hash,
    _extract_year,
    _strip_tags_keep_breaks,
)

# Картинки, которые не являются постером/скриншотами, даже если лежат в postImg:
# рейтинговые бейджи КиноПоиска, логотипы релиз-групп и прочий chrome форума.
_JUNK_IMG_MARKERS = (
    "kinopoisk.ru/rating",
    "/images/rg/",                 # логотипы релиз-групп на nnmstatic
    "nnmstatic.win/forum/images/",  # прочий chrome форума
    "static.rutracker",
)


def _unwrap_proxy(url: str) -> str:
    """NNM проксирует внешние картинки как ``.../image.php?link=<real-url>``.

    Возвращаем прямой URL (отдаётся напрямую с Referer и позволяет открыть
    полный размер); для прямых ссылок (RuTracker) — без изменений."""
    if "image.php" in url and "link=" in url:
        link = parse_qs(urlparse(url).query).get("link")
        if link:
            return link[0]
    return url


def _is_junk_image(url: str) -> bool:
    if not url.startswith("http"):
        return True
    low = url.lower()
    if low.endswith(".gif"):  # смайлы, бейджи, анимированные логотипы
        return True
    return any(marker in low for marker in _JUNK_IMG_MARKERS)


def _first_post_html(page: str) -> str:
    """Изолировать первый пост (раздачу), отбросив ответы в теме.

    phpBB оборачивает каждый пост в ``<table|tbody id="post_NNN">``."""
    m = re.search(r'(?is)<(?:table|tbody)\s+id="post_\d+"', page)
    if not m:
        return page
    rest = re.search(r'(?is)<(?:table|tbody)\s+id="post_\d+"', page[m.end():])
    end = m.end() + rest.start() if rest else len(page)
    return page[m.start():end]


def _postbody_html(post_html: str) -> str:
    """Вырезать тело поста (без колонки автора со «Стаж»/аватаром/подписью).

    Балансируем вложенные ``<div>``, т.к. внутри постбоди есть спойлеры/цитаты."""
    m = re.search(r'(?is)<div[^>]*class="[^"]*post_?body[^"]*"[^>]*>', post_html)
    if not m:
        return post_html
    body = post_html[m.end():]
    depth = 1
    for t in re.finditer(r"(?is)<(/?)div\b", body):
        depth += -1 if t.group(1) else 1
        if depth == 0:
            return body[:t.start()]
    return body


def _content_images(scope_html: str) -> list[tuple[bool, str]]:
    """Достать (is_poster, url) из ``<var class="postImg" title="URL">``-тегов."""
    out: list[tuple[bool, str]] = []
    for m in re.finditer(r"(?is)<var\b[^>]*>", scope_html):
        tag = m.group(0)
        if "postImg" not in tag:  # пропускаем posterAvatar и т.п.
            continue
        tm = re.search(r'title="([^"]*)"', tag)
        if not tm:
            continue
        url = _unwrap_proxy(html.unescape(tm.group(1)))
        if _is_junk_image(url):
            continue
        is_poster = "img-right" in tag or "Aligned" in tag
        out.append((is_poster, url))
    return out


def _extract_images(page: str) -> tuple[str, list[str]]:
    """Вернуть (poster_url, screenshots) из первого поста."""
    post = _first_post_html(page)
    imgs = _content_images(post)
    if not imgs:
        return "", []
    poster = next((u for is_poster, u in imgs if is_poster), imgs[0][1])
    screenshots: list[str] = []
    seen = {poster}
    for _, u in imgs:
        if u not in seen:
            seen.add(u)
            screenshots.append(u)
    return poster, screenshots


def _extract_description(page: str) -> str:
    text = _strip_tags_keep_breaks(_postbody_html(_first_post_html(page)))
    if len(text) > 6000:
        text = text[:6000].rstrip() + "..."
    return text


def fetch_details(url: str, timeout: int = 10, proxy: str = "") -> dict:
    """Скачать страницу раздачи phpBB-трекера и вернуть метаданные.

    Структура результата совпадает с :func:`torflash.meta.fetch_torrent_details`.
    Всегда возвращает dict со всеми ключами, даже при ошибке."""
    result = {
        "poster_url": "",
        "screenshots": [],
        "description": "",
        "year": "",
        "files": [],
        "category": "",
        "magnet": "",
        "info_hash": "",
    }
    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        r = requests.get(url, headers=HEADERS, timeout=timeout, proxies=proxies)
        r.raise_for_status()
        r.encoding = r.encoding or "utf-8"
        page = r.text
    except requests.RequestException:
        return result

    try:
        result["poster_url"], result["screenshots"] = _extract_images(page)
        result["description"] = _extract_description(page)
        result["year"] = _extract_year("", result["description"])
        magnet, info_hash = _extract_magnet_and_hash(page)
        result["magnet"] = magnet
        result["info_hash"] = info_hash
    except Exception:
        # Парсер обязан быть терпимым — отдаём что успели собрать.
        pass
    return result
