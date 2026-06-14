"""Fetch and parse torrent detail pages from rutor.info.

Pure stdlib + requests. No Qt, no libtorrent.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urlparse, urlunparse

import requests

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru,en;q=0.8",
}
MIRRORS = ["rutor.info", "rutor.is", "rutor.org"]


def _abs_url(src: str, base_host: str) -> str:
    """Make an absolute http(s) URL from possibly protocol-relative or relative src."""
    if not src:
        return ""
    src = src.strip()
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("http://") or src.startswith("https://"):
        return src
    if src.startswith("/"):
        return f"https://{base_host}{src}"
    return f"https://{base_host}/{src}"


def _strip_tags_keep_breaks(snippet: str) -> str:
    """Convert <br>/<p>/</p>/</div>/</li>/</tr> to newlines, then drop other tags."""
    s = snippet
    # Drop <script>/<style> blocks entirely.
    s = re.sub(r"(?is)<(script|style|textarea)[^>]*>.*?</\1>", "", s)
    # Convert breaks/block-enders to newlines.
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|li|tr|h[1-6])\s*>", "\n", s)
    # Drop remaining tags.
    s = re.sub(r"(?s)<[^>]+>", "", s)
    # Decode entities and squash whitespace.
    s = html.unescape(s)
    # Normalize trailing spaces per line and collapse 3+ newlines.
    lines = [ln.rstrip() for ln in s.splitlines()]
    s = "\n".join(lines)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _extract_poster(details_html: str, host: str) -> str:
    """Look for the first content image inside the details table."""
    # Prefer a div.tor-image wrapper if present.
    m = re.search(
        r'(?is)<div[^>]*class="[^"]*tor-image[^"]*"[^>]*>(.*?)</div>',
        details_html,
    )
    if m:
        inner = m.group(1)
        im = re.search(r'(?is)<img[^>]+src="([^"]+)"', inner)
        if im:
            return _abs_url(im.group(1), host)
    # Otherwise pick the first <img> that is not a layout/cdn UI asset.
    for im in re.finditer(r'(?is)<img[^>]+src="([^"]+)"', details_html):
        src = im.group(1)
        if "cdnbunny.org" in src or src.endswith(".gif"):
            continue
        if "/i/" in src and ("rutor" in src or src.startswith("//")):
            continue
        return _abs_url(src, host)
    return ""


def _extract_screenshots(details_html: str, host: str, poster_url: str) -> list[str]:
    """Extract screenshot/preview URLs from the details table (excluding poster and UI assets)."""
    urls: list[str] = []
    seen: set[str] = set()
    if poster_url:
        seen.add(poster_url)
    for im in re.finditer(r'(?is)<img[^>]+src="([^"]+)"', details_html):
        src = im.group(1)
        if "cdnbunny.org" in src or src.endswith(".gif") or src.endswith(".png"):
            continue
        if "/i/" in src and ("rutor" in src or src.startswith("//")):
            continue
        url = _abs_url(src, host)
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _extract_description(details_html: str) -> str:
    """Pull the main description blob from the first big <td> inside #details."""
    # Find the first row's second cell which holds the body of the description.
    m = re.search(
        r"(?is)<tr>\s*<td[^>]*>\s*</td>\s*<td[^>]*>(.*?)</td>\s*</tr>",
        details_html,
    )
    block = m.group(1) if m else details_html
    text = _strip_tags_keep_breaks(block)
    # Trim to a reasonable size.
    if len(text) > 6000:
        text = text[:6000].rstrip() + "..."
    return text


def _extract_year(title: str, description: str) -> str:
    for source in (title, description):
        if not source:
            continue
        m = re.search(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", source)
        if m:
            return m.group(1)
    return ""


def _extract_title(page_html: str) -> str:
    m = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", page_html)
    if m:
        return _strip_tags_keep_breaks(m.group(1))
    m = re.search(r"(?is)<title>(.*?)</title>", page_html)
    if m:
        t = _strip_tags_keep_breaks(m.group(1))
        return re.sub(r"^rutor\.[a-z]+\s*::\s*", "", t, flags=re.I)
    return ""


def _extract_category(details_html: str) -> str:
    m = re.search(
        r'(?is)<td[^>]*class="header"[^>]*>\s*Категория\s*</td>\s*<td[^>]*>(.*?)</td>',
        details_html,
    )
    if not m:
        return ""
    return _strip_tags_keep_breaks(m.group(1))


def _extract_magnet_and_hash(page_html: str) -> tuple[str, str]:
    m = re.search(r'href="(magnet:\?[^"]+)"', page_html)
    if not m:
        return "", ""
    magnet = html.unescape(m.group(1))
    h = re.search(r"urn:btih:([A-Fa-f0-9]{40}|[A-Za-z2-7]{32})", magnet)
    info_hash = h.group(1).lower() if h else ""
    return magnet, info_hash


def _extract_torrent_id(url: str, page_html: str) -> str:
    m = re.search(r"/torrent/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/descriptions/(\d+)\.files", page_html)
    return m.group(1) if m else ""


def _parse_files_html(files_html: str) -> list[dict]:
    """Parse the small HTML fragment returned by /descriptions/<id>.files."""
    out: list[dict] = []
    for row in re.finditer(r"(?is)<tr>(.*?)</tr>", files_html):
        cells = re.findall(r"(?is)<td[^>]*>(.*?)</td>", row.group(1))
        if len(cells) < 2:
            continue
        name = _strip_tags_keep_breaks(cells[0])
        size = _strip_tags_keep_breaks(cells[1])
        if not name or name.lower() == "название":
            continue
        out.append({"name": name, "size": size})
    return out


def _fetch_files(session: requests.Session, host: str, tid: str, timeout: int) -> list[dict]:
    if not tid:
        return []
    try:
        r = session.get(
            f"https://{host}/descriptions/{tid}.files",
            headers=HEADERS,
            timeout=timeout,
        )
        if r.status_code == 200 and r.text.strip():
            r.encoding = r.encoding or "utf-8"
            return _parse_files_html(r.text)
    except requests.RequestException:
        pass
    return []


def _try_get(url: str, timeout: int, proxy: str = "") -> tuple[str, str] | None:
    """Try mirrors in order. Returns (host, html) or None."""
    parsed = urlparse(url if "://" in url else "https://" + url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    candidates = []
    if parsed.netloc:
        candidates.append(parsed.netloc)
    for m in MIRRORS:
        if m not in candidates:
            candidates.append(m)

    session = requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    for host in candidates:
        target = urlunparse(("https", host, parsed.path or "/", "", parsed.query, ""))
        try:
            r = session.get(target, headers=HEADERS, timeout=timeout)
        except requests.RequestException:
            continue
        if r.status_code == 200 and r.text:
            r.encoding = r.encoding or "utf-8"
            return host, r.text, session  # type: ignore[return-value]
    return None


def fetch_torrent_details(url: str, timeout: int = 10, proxy: str = "") -> dict:
    """Fetch a rutor torrent detail page and return parsed metadata.

    Always returns a dict with all expected keys, even on failure.
    """
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
        got = _try_get(url, timeout, proxy)
    except Exception:
        got = None
    if not got:
        return result
    host, page, session = got  # type: ignore[misc]

    try:
        # Isolate the details table to scope poster/description searches.
        details_m = re.search(
            r'(?is)<table\s+id="details".*?</table>', page
        )
        details_html = details_m.group(0) if details_m else page

        title = _extract_title(page)
        result["poster_url"] = _extract_poster(details_html, host)
        result["screenshots"] = _extract_screenshots(details_html, host, result["poster_url"])
        result["description"] = _extract_description(details_html)
        result["year"] = _extract_year(title, result["description"])
        result["category"] = _extract_category(details_html)
        magnet, info_hash = _extract_magnet_and_hash(page)
        result["magnet"] = magnet
        result["info_hash"] = info_hash

        tid = _extract_torrent_id(url, page)
        result["files"] = _fetch_files(session, host, tid, timeout)
    except Exception:
        # Parser must be tolerant — return whatever we have so far.
        pass
    return result


if __name__ == "__main__":
    import json

    test_url = "https://rutor.info/torrent/1052665"
    data = fetch_torrent_details(test_url)
    # Truncate description for readability in the test print.
    preview = dict(data)
    if len(preview["description"]) > 600:
        preview["description"] = preview["description"][:600] + "..."
    print(json.dumps(preview, ensure_ascii=False, indent=2))
