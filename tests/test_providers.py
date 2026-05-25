"""Тесты парсеров провайдеров на сохранённых HTML-фикстурах.

Запуск: python -m pytest tests/test_providers.py -v
"""

import sys
from pathlib import Path

# Добавляем src в путь
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class TestRutorParser:
    def _parse(self):
        from providers.rutor import _parse
        html = (FIXTURES / "rutor_search.html").read_text()
        return _parse(html, "https://rutor.info")

    def test_finds_results(self):
        results = self._parse()
        assert len(results) > 0, "Должны найтись результаты"

    def test_result_keys(self):
        r = self._parse()[0]
        for key in ("provider", "date", "title", "size", "seeds", "leech", "magnet", "torrent_url", "page"):
            assert key in r, f"Отсутствует ключ {key}"

    def test_provider_name(self):
        for r in self._parse():
            assert r["provider"] == "rutor"

    def test_magnet_present(self):
        results = self._parse()
        with_magnet = [r for r in results if r["magnet"]]
        assert len(with_magnet) > 0, "Хотя бы один результат должен иметь magnet"

    def test_magnet_format(self):
        for r in self._parse():
            if r["magnet"]:
                assert r["magnet"].startswith("magnet:?"), f"Некорректный magnet: {r['magnet'][:50]}"

    def test_seeds_numeric(self):
        for r in self._parse():
            assert r["seeds"].isdigit(), f"seeds не число: {r['seeds']}"

    def test_page_url(self):
        for r in self._parse():
            if r["page"]:
                assert "/torrent/" in r["page"], f"Некорректный page URL: {r['page']}"

    def test_title_not_empty(self):
        for r in self._parse():
            assert r["title"].strip(), "Пустой title"

    def test_size_has_unit(self):
        for r in self._parse():
            if r["size"]:
                assert any(u in r["size"] for u in ("MB", "GB", "TB", "KB")), \
                    f"Размер без единицы: {r['size']}"


class TestNnmParser:
    def _parse(self):
        from providers.nnm import _parse
        html = (FIXTURES / "nnm_search.html").read_text()
        return _parse(html)

    def test_finds_results(self):
        results = self._parse()
        assert len(results) > 0, "Должны найтись результаты"

    def test_result_keys(self):
        r = self._parse()[0]
        for key in ("provider", "date", "title", "size", "seeds", "leech", "torrent_url", "page"):
            assert key in r, f"Отсутствует ключ {key}"

    def test_provider_name(self):
        for r in self._parse():
            assert r["provider"] == "nnm"

    def test_torrent_url_present(self):
        results = self._parse()
        with_url = [r for r in results if r["torrent_url"]]
        assert len(with_url) > 0, "Хотя бы один результат должен иметь torrent_url"

    def test_date_format(self):
        for r in self._parse():
            if r["date"]:
                assert len(r["date"]) == 10, f"Дата не в формате YYYY-MM-DD: {r['date']}"
                assert r["date"][4] == "-" and r["date"][7] == "-"

    def test_seeds_numeric(self):
        for r in self._parse():
            assert r["seeds"].isdigit(), f"seeds не число: {r['seeds']}"

    def test_title_not_empty(self):
        for r in self._parse():
            assert r["title"].strip(), "Пустой title"


class TestRutorMeta:
    def _fetch(self):
        import re
        from rutor_meta import (
            _extract_poster, _extract_description, _extract_screenshots,
            _extract_year, _extract_magnet_and_hash, _extract_title,
        )
        html = (FIXTURES / "rutor_detail.html").read_text()
        details_m = re.search(r'(?is)<table\s+id="details".*?</table>', html)
        details = details_m.group(0) if details_m else html
        host = "rutor.info"
        return {
            "html": html,
            "details": details,
            "host": host,
            "poster": _extract_poster(details, host),
            "screenshots": _extract_screenshots(details, host, _extract_poster(details, host)),
            "description": _extract_description(details),
            "title": _extract_title(html),
            "year": _extract_year(_extract_title(html), _extract_description(details)),
            "magnet_hash": _extract_magnet_and_hash(html),
        }

    def test_poster_url(self):
        data = self._fetch()
        assert data["poster"], "Постер не найден"
        assert data["poster"].startswith("https://"), f"Постер не HTTPS: {data['poster']}"

    def test_screenshots_found(self):
        data = self._fetch()
        assert len(data["screenshots"]) > 0, "Скриншоты не найдены"

    def test_screenshots_not_contain_poster(self):
        data = self._fetch()
        assert data["poster"] not in data["screenshots"], "Постер не должен быть в скриншотах"

    def test_screenshots_no_cdn_junk(self):
        data = self._fetch()
        for url in data["screenshots"]:
            assert "cdnbunny" not in url, f"CDN-мусор в скриншотах: {url}"
            assert not url.endswith(".gif"), f"GIF в скриншотах: {url}"

    def test_description_not_empty(self):
        data = self._fetch()
        assert len(data["description"]) > 50, "Описание слишком короткое"

    def test_title_not_empty(self):
        data = self._fetch()
        assert data["title"].strip(), "Пустой заголовок"

    def test_magnet_and_hash(self):
        magnet, info_hash = self._fetch()["magnet_hash"]
        assert magnet.startswith("magnet:?"), f"Некорректный magnet: {magnet[:50]}"
        assert len(info_hash) == 40, f"Hash не 40 символов: {info_hash}"

    def test_year_extracted(self):
        data = self._fetch()
        assert data["year"], "Год не извлечён"
        assert data["year"].isdigit() and len(data["year"]) == 4


class TestSizeParser:
    def test_parse_gb(self):
        from rutor_search import parse_size_text
        assert parse_size_text("1.5 GB") == int(1.5 * 1024**3)

    def test_parse_mb(self):
        from rutor_search import parse_size_text
        assert parse_size_text("700 MB") == 700 * 1024**2

    def test_parse_empty(self):
        from rutor_search import parse_size_text
        assert parse_size_text("") == 0

    def test_parse_comma(self):
        from rutor_search import parse_size_text
        assert parse_size_text("1,5 GB") == int(1.5 * 1024**3)
