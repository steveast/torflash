"""Выбор релизного ассета под текущую платформу (без Qt — тестируется отдельно).

При мультиплатформенных релизах в одном GitHub-релизе лежат бинарники под все
ОС. Эти функции выбирают нужный по sys.platform. Linux-ассет исторически
называется просто 'TorFlash' (на это имя завязан AUR) — учитываем это."""

from __future__ import annotations


SIDECAR_SUFFIXES = (".asc", ".sig", ".sha256", ".minisig")


def asset_platform(name: str) -> str:
    """Классифицирует имя GitHub-ассета по платформе ('win32'|'darwin'|'linux')."""
    low = name.lower()
    if low.endswith(".exe") or "windows" in low or "win64" in low or "win32" in low:
        return "win32"
    if (low.endswith((".dmg", ".pkg")) or "macos" in low or "darwin" in low
            or low.endswith(".app.zip") or "-mac" in low or "osx" in low):
        return "darwin"
    return "linux"  # «голый» TorFlash, *.AppImage, *-linux-*


def select_platform_asset(assets: list, platform_name: str) -> "dict | None":
    """Выбирает бинарный ассет под платформу platform_name (значение sys.platform).

    Учитываются только ассеты, чьё имя начинается на 'TorFlash' и не является
    сайдкаром (.sha256/.minisig/…). Для Linux предпочитается «голый» TorFlash,
    затем прочее (например AppImage). Возвращает asset-dict или None."""
    plat = platform_name if platform_name in ("win32", "darwin") else "linux"
    candidates = [
        a for a in assets
        if a.get("name", "").startswith("TorFlash")
        and not a.get("name", "").endswith(SIDECAR_SUFFIXES)
        and asset_platform(a.get("name", "")) == plat
    ]
    if not candidates:
        return None
    if plat == "linux":
        for a in candidates:
            if a.get("name") == "TorFlash":
                return a
    return candidates[0]
