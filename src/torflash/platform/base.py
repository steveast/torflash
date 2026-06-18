"""Абстрактный платформенный слой TorFlash.

Вся работа со сменным носителем (поиск, метка, ФС, форматирование, безопасное
извлечение), открытие путей в системном проводнике и автозапуск спрятаны за
этим интерфейсом. Реализации — linux.py / windows.py / macos.py. UI вызывает
только методы PlatformBackend и не знает про udisksctl / DiskPart / diskutil.

Локализация остаётся в UI: бэкенд возвращает структурный OpResult (без готовых
фраз), а шаги прогресса сообщает через on_status(key) — UI сам переводит ключи.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional


# Колбэк прогресса: бэкенд зовёт on_status("sync"|"unmount"|"format"|...),
# UI отображает локализованную надпись в статус-баре.
StatusCallback = Optional[Callable[[str], None]]


@dataclass
class OpResult:
    """Итог платформенной операции (форматирование / извлечение).

    ok            — операция достигла цели (флешка отформатирована / её можно
                    вынимать). Для извлечения ok=True и при неудавшемся power-off:
                    том размонтирован, физически вынуть безопасно.
    step          — при ok=False: шаг, на котором упали ("detect"/"unmount"/
                    "format"). При ok=True: шаг с некритичным предупреждением
                    ("poweroff"/"remount"), иначе "".
    message       — сырой текст ошибки ОС (UI подставит в локализованный шаблон).
    device        — задействованный узел устройства (для сообщений/подтверждения).
    busy          — список держащих устройство процессов ("name(pid), …"),
                    заполняется когда step == "unmount".
    missing_tool  — требуемая системная утилита не найдена (FileNotFoundError).
    """

    ok: bool
    step: str = ""
    message: str = ""
    device: str = ""
    busy: str = ""
    missing_tool: bool = False


class PlatformBackend(ABC):
    """Контракт платформенного бэкенда. Один конкретный класс на ОС."""

    name: str = "generic"

    # --- сменный носитель: чтение ---

    @abstractmethod
    def find_flash_mount(self) -> "str | None":
        """Точка монтирования сменной флешки или None, если не подключена.

        Linux: каталог под /run/media/<user>. Windows: корень removable-диска
        ("E:\\"). macOS: каталог под /Volumes."""

    @abstractmethod
    def flash_device(self, mount: str) -> "str | None":
        """Идентификатор устройства/раздела для mount (непрозрачен для UI).

        Используется в подтверждении форматирования и как аргумент format/eject.
        None, если устройство определить не удалось."""

    @abstractmethod
    def volume_label(self, mount: str) -> str:
        """Метка тома (для отображения и сохранения при форматировании). Дёшево."""

    @abstractmethod
    def flash_fstype(self, mount: str) -> str:
        """Тип файловой системы тома ('' если определить не удалось)."""

    # --- сменный носитель: операции ---

    @abstractmethod
    def format_fat32(self, mount: str, label: str,
                     on_status: StatusCallback = None) -> OpResult:
        """Отформатировать носитель в FAT32 с меткой label.

        Полный цикл: размонтировать → форматировать → перемонтировать."""

    @abstractmethod
    def eject(self, mount: str, on_status: StatusCallback = None) -> OpResult:
        """Безопасно извлечь носитель (sync → размонтировать → отключить питание)."""

    # --- интеграция с ОС ---

    @abstractmethod
    def open_path(self, path: str) -> bool:
        """Открыть файл/папку в системном проводнике. True при успехе."""

    @abstractmethod
    def set_autostart(self, enabled: bool) -> None:
        """Включить/выключить запуск приложения при входе в систему."""

    @abstractmethod
    def is_autostart_enabled(self) -> bool:
        """Настроен ли автозапуск приложения."""

    # --- автообновление ---

    @abstractmethod
    def update_artifact_name(self) -> str:
        """Имя файла, в который скачивается обновление (рядом с бинарником).

        Linux/macOS: 'TorFlash.new'. Windows: 'TorFlash.exe.new'."""

    @abstractmethod
    def install_update(self, new_path: str, current_exe: str,
                       argv: "list[str]") -> None:
        """Установить скачанное обновление new_path вместо current_exe и
        перезапустить приложение.

        Linux/macOS: заменяет бинарник и делает execv — НЕ возвращается.
        Windows: запускает отложенный хелпер замены и ВОЗВРАЩАЕТСЯ; вызывающий
        обязан тут же завершить приложение, чтобы .exe можно было заменить.
        Бросает OSError при ошибке замены."""
