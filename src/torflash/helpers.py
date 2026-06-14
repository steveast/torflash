"""TorFlash: чистые помощники (форматирование, пути, парсинг) без Qt."""

import os
import re
from pathlib import Path


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


SIZE_RE = re.compile(r"([\d.,]+)\s*(KB|MB|GB|TB|B)?", re.I)


SIZE_FACTORS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}


def parse_size_text(s: str) -> int:
    if not s:
        return 0
    s = s.replace("\xa0", " ").strip()
    m = SIZE_RE.match(s)
    if not m:
        return 0
    try:
        val = float(m.group(1).replace(",", "."))
    except ValueError:
        return 0
    unit = (m.group(2) or "B").upper()
    return int(val * SIZE_FACTORS.get(unit, 1))


def fmt_time(seconds) -> str:
    if seconds is None:
        return "—"
    try:
        s = int(seconds)
    except (TypeError, ValueError, OverflowError):
        return "—"
    if s < 0 or s > 24 * 3600 * 99:
        return "—"
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def _result_id(r: dict) -> str:
    """Стабильный идентификатор результата поиска / элемента очереди загрузки.

    Magnet — самый надёжный (есть info_hash), но у NNM-провайдера magnet пустой
    до момента скачивания .torrent. Фолбэк — провайдер + страница торрента."""
    if not r:
        return ""
    if r.get("magnet"):
        return r["magnet"]
    return f'{r.get("provider","")}::{r.get("page","") or r.get("torrent_url","")}'


def _current_user() -> str:
    """Имя пользователя без os.getlogin() — тот падает с OSError без управляющего
    терминала (автозапуск/systemd)."""
    user = os.environ.get("USER") or os.environ.get("LOGNAME")
    if user:
        return user
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, ImportError, AttributeError):
        return ""


def _safe_join(root, rel) -> "Path | None":
    """Безопасно склеивает root/rel. Возвращает None, если результат (после
    разворачивания ../ и симлинков) выходит за пределы root. Защита от
    path traversal через имена файлов из недоверенных торрентов."""
    root_p = Path(root).resolve()
    dst = (root_p / rel).resolve()
    try:
        dst.relative_to(root_p)
    except ValueError:
        return None
    return dst


def detect_flash_mount(base: "str | None" = None) -> str | None:
    base_path = Path(base) if base is not None else Path(f"/run/media/{_current_user()}")
    if not base_path.exists():
        return None
    for child in base_path.iterdir():
        if child.is_dir() and os.access(child, os.W_OK):
            return str(child)
    return None


PART_SUFFIX_RE = re.compile(r"(-\d{3}|\.part\d{3}|\.\d{1,2})$")


def _strip_part_suffix(stem: str) -> str:
    return PART_SUFFIX_RE.sub("", stem)


def _split_copy_part_name(stem: str, ext: str, idx: int) -> str:
    """Имя части raw-сплита: name.part000.mkv (расширение в конце, чтобы части
    группировались обратно через _strip_part_suffix)."""
    return f"{stem}.part{idx:03d}{ext}"


def group_movie_parts(files: list[tuple[Path, int, float]]) -> list[dict]:
    """Группирует части фильма в один логический фильм.

    Вход: список (path, size, mtime). Выход: список словарей с title, size,
    count, mtime, paths. Группа из 1 файла = одиночный фильм (исходное имя)."""
    groups: dict[tuple[str, str], list[tuple[Path, int, float]]] = {}
    for path, size, mtime in files:
        key = (_strip_part_suffix(path.stem), path.suffix.lower())
        groups.setdefault(key, []).append((path, size, mtime))
    result = []
    for (base, ext), items in groups.items():
        items.sort(key=lambda x: x[0].name)
        # Группа из 1 файла без суффикса — оригинальное имя; иначе — base
        if len(items) == 1 and items[0][0].stem == base:
            title = items[0][0].name
        else:
            title = base + ext
        result.append({
            "title": title,
            "size": sum(s for _, s, _ in items),
            "count": len(items),
            "mtime": max(m for _, _, m in items),
            "paths": [p for p, _, _ in items],
        })
    result.sort(key=lambda g: g["title"].lower())
    return result


def _version_tuple(v: str) -> tuple:
    parts = []
    for x in v.lstrip("v").split("."):
        try:
            parts.append(int(x))
        except ValueError:
            break
    return tuple(parts)


def _sha256_from_sumfile(text: str) -> str:
    """Парсит файл формата `sha256sum`: «<hex>  <имя>». Возвращает hex в
    нижнем регистре либо «» если строка не похожа на sha256."""
    token = (text or "").strip().split()
    return token[0].lower() if token and len(token[0]) == 64 else ""


MAGNET_HASH_RE = re.compile(r"btih:([a-f0-9]+)", re.I)
