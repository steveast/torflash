"""Базовый класс провайдера + общие константы."""

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


class Provider:
    """Интерфейс источника. Возвращает list[dict] результатов поиска.

    Каждый dict содержит ключи: provider, date, title, size, seeds, leech,
    magnet, torrent_url, page. Хотя бы один из magnet/torrent_url должен быть
    заполнен — иначе скачать торрент будет нечем."""

    name: str = ""
    display_name: str = ""
    # Логические категории для combobox: list[(native_id, label)]. 0 = все.
    categories: list[tuple[int, str]] = [(0, "Все")]

    def search(self, query: str, category: int = 0, timeout: float = 10) -> list[dict]:
        raise NotImplementedError
