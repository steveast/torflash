#!/usr/bin/env python3
"""TorFlash — точка входа: single-instance guard, логирование, запуск окна."""

import sys
import traceback

from PyQt5.QtCore import QSharedMemory
from PyQt5.QtGui import QFont, QIcon
from PyQt5.QtWidgets import QApplication

from torflash.config import APP_NAME, ASSETS_DIR
from torflash.runtime import setup_icon_theme, _install_logging
from torflash.ui.main_window import MainWindow


def main():
    _install_logging()

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)

    # Запрет запуска более одного экземпляра
    shared_mem = QSharedMemory("TorFlash_SingleInstance", app)
    if not shared_mem.create(1):
        # На Linux сегмент может остаться после краша — пробуем очистить
        shared_mem.attach()
        shared_mem.detach()
        if not shared_mem.create(1):
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.warning(None, APP_NAME, "TorFlash уже запущен.")
            sys.exit(0)

    # KDE Plasma 6 сохраняет шрифты в формате Qt6 — Qt5 не может их прочитать.
    # Задаём шрифт явно, чтобы избежать QFont::fromString warnings и уродливых шрифтов.
    app.setFont(QFont("Noto Sans", 10))

    app.setQuitOnLastWindowClosed(False)  # окно скрывается в трей — не выходим
    setup_icon_theme()
    icon_path = ASSETS_DIR / "torflash.svg"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    w = MainWindow()

    # Корректное закрытие seed-сессии и сохранение resume_data
    def _on_about_to_quit():
        try:
            w.stop_all_workers()
        except Exception:
            print(f"[main] stop workers error:\n{traceback.format_exc()}", flush=True)
        try:
            w.seed.shutdown()
        except Exception:
            print(f"[main] seed shutdown error:\n{traceback.format_exc()}", flush=True)
    app.aboutToQuit.connect(_on_about_to_quit)

    start_hidden = (
        "--hidden" in sys.argv
        or w.settings.value("start_hidden", False, type=bool)
    )
    if not start_hidden:
        w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
