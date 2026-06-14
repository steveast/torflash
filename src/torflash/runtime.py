"""TorFlash: иконки темы, логирование (файл+терминал), хуки исключений."""

import faulthandler
import os
import sys
import threading
import time
import traceback
from pathlib import Path

from PyQt5.QtGui import QIcon

from torflash.config import APP_NAME, APP_VERSION, LIBRARY_DIR


def themed_icon(name: str, style=None, fallback=None) -> QIcon:
    """Сначала пробуем иконку из системной темы (Breeze, Adwaita, …),
    затем — стандартную из Qt-стиля. Возвращаем пустую QIcon в крайнем случае."""
    icon = QIcon.fromTheme(name)
    if not icon.isNull() and icon.availableSizes():
        return icon
    if style is not None and fallback is not None:
        return style.standardIcon(fallback)
    return QIcon()


def setup_icon_theme():
    """Гарантируем что QIcon.fromTheme(name) находит системные иконки.
    Под PyInstaller/некоторыми DE дефолтные пути не включают /usr/share/icons."""
    paths = list(QIcon.themeSearchPaths())
    for p in (
        "/usr/share/icons",
        str(Path.home() / ".local" / "share" / "icons"),
        str(Path.home() / ".icons"),
    ):
        if p not in paths and Path(p).exists():
            paths.append(p)
    QIcon.setThemeSearchPaths(paths)
    if not QIcon.themeName() or QIcon.themeName() == "hicolor":
        for theme in ("breeze", "Adwaita", "Papirus", "gnome", "oxygen"):
            for base in paths:
                if (Path(base) / theme / "index.theme").exists():
                    QIcon.setThemeName(theme)
                    return


class _Tee:
    """stdout/stderr -> файл + исходный поток (если есть)."""
    def __init__(self, fp, mirror):
        self._fp = fp
        self._mirror = mirror
    def write(self, data):
        try:
            self._fp.write(data)
            self._fp.flush()
        except Exception:
            pass
        if self._mirror is not None:
            try:
                self._mirror.write(data)
                self._mirror.flush()
            except Exception:
                pass
        return len(data) if isinstance(data, str) else 0
    def flush(self):
        try: self._fp.flush()
        except Exception: pass
        if self._mirror is not None:
            try: self._mirror.flush()
            except Exception: pass
    def isatty(self):
        return False
    def fileno(self):
        return self._fp.fileno()


def _install_logging():
    """Полное логирование: файл + терминал, traceback необработанных исключений,
    faulthandler для нативных крашей (libtorrent)."""
    try:
        LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LIBRARY_DIR / "torflash.log"
        log_fp = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
    except OSError:
        return

    # Если запущены без терминала (frozen или из .desktop) — mirror None.
    mirror_out = sys.__stdout__ if (sys.__stdout__ and sys.__stdout__.isatty()) else None
    mirror_err = sys.__stderr__ if (sys.__stderr__ and sys.__stderr__.isatty()) else None
    sys.stdout = _Tee(log_fp, mirror_out)
    sys.stderr = _Tee(log_fp, mirror_err)

    # faulthandler пишет нативные сегфолты (libtorrent и т.п.) в этот же файл.
    try:
        faulthandler.enable(file=log_fp, all_threads=True)
    except Exception as e:
        print(f"[log] faulthandler enable failed: {e}", flush=True)

    def _excepthook(exc_type, exc, tb):
        print("[uncaught] " + "".join(traceback.format_exception(exc_type, exc, tb)), flush=True)
        # Цепляем дефолтный, чтобы Qt тоже увидел.
        sys.__excepthook__(exc_type, exc, tb)
    sys.excepthook = _excepthook

    def _thread_excepthook(args):
        print(
            f"[uncaught-thread] in {args.thread.name if args.thread else '?'}:\n"
            + "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
            flush=True,
        )
    threading.excepthook = _thread_excepthook

    if hasattr(sys, "unraisablehook"):
        def _unraisable(unr):
            print(
                f"[unraisable] {unr.err_msg or ''} obj={unr.object!r}\n"
                + "".join(traceback.format_exception(unr.exc_type, unr.exc_value, unr.exc_traceback)),
                flush=True,
            )
        sys.unraisablehook = _unraisable

    print(
        f"\n=== {APP_NAME} v{APP_VERSION} started at {time.strftime('%Y-%m-%d %H:%M:%S')} "
        f"(pid={os.getpid()}, frozen={getattr(sys, 'frozen', False)}) ===",
        flush=True,
    )
    try:
        import libtorrent as _lt
        print(f"[env] libtorrent {_lt.__version__} python {sys.version.split()[0]}", flush=True)
    except Exception:
        pass
