"""Платформенный слой: выбор бэкенда по текущей ОС.

Импортируй готовый синглтон:

    from torflash.platform import backend
    mount = backend.find_flash_mount()

Конкретные модули бэкендов импортируются лениво, поэтому ОС-специфичные
зависимости (winreg и т.п.) не трогаются на чужой платформе."""

import sys

from .base import OpResult, PlatformBackend, StatusCallback


def _make_backend() -> PlatformBackend:
    if sys.platform == "win32":
        from .windows import WindowsBackend
        return WindowsBackend()
    if sys.platform == "darwin":
        from .macos import MacOSBackend
        return MacOSBackend()
    from .linux import LinuxBackend
    return LinuxBackend()


backend: PlatformBackend = _make_backend()


__all__ = ["backend", "PlatformBackend", "OpResult", "StatusCallback"]
