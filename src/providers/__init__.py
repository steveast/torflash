"""Источники поиска торрентов. Каждый Provider парсит свой сайт и возвращает
результаты в едином формате — list[dict] с ключами провайдера, title, size,
seeds, leech, magnet, torrent_url, page, date."""

from .base import Provider, HEADERS
from .rutor import RutorProvider
from .nnm import NnmProvider

ALL_PROVIDERS: list[Provider] = [RutorProvider(), NnmProvider()]


def get_provider(name: str) -> Provider | None:
    for p in ALL_PROVIDERS:
        if p.name == name:
            return p
    return None
