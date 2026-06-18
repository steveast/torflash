"""macOS-бэкенд: diskutil + LaunchAgent.

ВНИМАНИЕ: написан по документации diskutil/launchd, но (в отличие от Linux)
ещё не прогонялся на реальном железе — Windows в приоритете. Проверять на маке.

- Поиск флешки: diskutil list внешних/съёмных томов, смонтированных в /Volumes.
- Форматирование: diskutil eraseVolume "MS-DOS FAT32".
- Извлечение: diskutil eject.
- Автозапуск: LaunchAgent plist в ~/Library/LaunchAgents."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

from .base import OpResult, PlatformBackend, StatusCallback


LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / "pro.torflash.autostart.plist"
LAUNCH_LABEL = "pro.torflash.autostart"


class MacOSBackend(PlatformBackend):
    name = "macos"

    # Короткий TTL-кэш diskutil: схлопывает повторные вызовы для одного тома
    # внутри одного тика обновления (find → fstype → device дёргают diskutil для
    # одного mount). Новый том на первом тике — всегда промах кэша, так что
    # задержки в обнаружении вставленной флешки нет.
    _INFO_TTL = 3.0

    def __init__(self):
        self._info_cache: "dict[str, tuple[float, dict]]" = {}

    # --- сменный носитель: чтение ---

    def _diskutil_info(self, target: str) -> dict:
        """diskutil info -plist <target> → dict (пусто при ошибке), с TTL-кэшем."""
        now = time.monotonic()
        hit = self._info_cache.get(target)
        if hit is not None and now - hit[0] < self._INFO_TTL:
            return hit[1]
        try:
            out = subprocess.run(
                ["diskutil", "info", "-plist", target],
                capture_output=True, timeout=10,
            )
            info = plistlib.loads(out.stdout) if out.returncode == 0 else {}
        except (subprocess.SubprocessError, OSError, plistlib.InvalidFileException):
            info = {}
        self._info_cache[target] = (now, info)
        return info

    def find_flash_mount(self) -> "str | None":
        volumes = Path("/Volumes")
        if not volumes.exists():
            return None
        for child in sorted(volumes.iterdir()):
            try:
                if not child.is_dir() or not os.access(child, os.W_OK):
                    continue
            except OSError:
                continue
            info = self._diskutil_info(str(child))
            # Берём только внешние/съёмные тома — не системный диск.
            if info.get("RemovableMedia") or info.get("External") or info.get("Ejectable"):
                return str(child)
        return None

    def flash_device(self, mount: str) -> "str | None":
        node = self._diskutil_info(mount).get("DeviceNode")
        return node or None

    def volume_label(self, mount: str) -> str:
        return Path(mount).name

    def flash_fstype(self, mount: str) -> str:
        info = self._diskutil_info(mount)
        return info.get("FilesystemName") or info.get("FilesystemType") or ""

    # --- сменный носитель: операции ---

    def format_fat32(self, mount: str, label: str,
                     on_status: StatusCallback = None) -> OpResult:
        device = self.flash_device(mount)
        if not device:
            return OpResult(ok=False, step="detect")
        # Метка MS-DOS: до 11 символов, верхний регистр.
        lbl = "".join(c for c in label if c.isalnum() or c in " _-").strip()[:11].upper() or "FLASH"
        if on_status:
            on_status("format")
        try:
            res = subprocess.run(
                ["diskutil", "eraseVolume", "MS-DOS FAT32", lbl, device],
                capture_output=True, text=True, timeout=300,
            )
        except FileNotFoundError as e:
            return OpResult(ok=False, missing_tool=True, message=str(e))
        except subprocess.SubprocessError as e:
            return OpResult(ok=False, step="format", message=str(e))
        if res.returncode != 0:
            return OpResult(ok=False, step="format",
                            message=(res.stderr or res.stdout).strip(), device=device)
        return OpResult(ok=True, device=device)

    def eject(self, mount: str, on_status: StatusCallback = None) -> OpResult:
        device = self.flash_device(mount) or mount
        if on_status:
            on_status("sync")
            on_status("unmount")
        try:
            res = subprocess.run(
                ["diskutil", "eject", device],
                capture_output=True, text=True, timeout=60,
            )
        except FileNotFoundError as e:
            return OpResult(ok=False, missing_tool=True, message=str(e))
        except subprocess.SubprocessError as e:
            return OpResult(ok=False, step="error", message=str(e))
        if res.returncode != 0:
            return OpResult(ok=False, step="unmount",
                            message=(res.stderr or res.stdout).strip(), device=device)
        return OpResult(ok=True, device=device)

    # --- интеграция с ОС ---

    def open_path(self, path: str) -> bool:
        try:
            subprocess.Popen(["open", path])
            return True
        except OSError:
            return False

    def is_autostart_enabled(self) -> bool:
        return LAUNCH_AGENT.exists()

    def set_autostart(self, enabled: bool) -> None:
        if enabled:
            LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
            if getattr(sys, "frozen", False):
                args = [sys.executable, "--hidden"]
            else:
                args = [sys.executable, str(Path(sys.argv[0]).resolve()), "--hidden"]
            plist = {
                "Label": LAUNCH_LABEL,
                "ProgramArguments": args,
                "RunAtLoad": True,
                "ProcessType": "Interactive",
            }
            with open(LAUNCH_AGENT, "wb") as f:
                plistlib.dump(plist, f)
            try:
                subprocess.run(["launchctl", "load", str(LAUNCH_AGENT)],
                               capture_output=True, timeout=10)
            except (subprocess.SubprocessError, OSError):
                pass
        else:
            try:
                subprocess.run(["launchctl", "unload", str(LAUNCH_AGENT)],
                               capture_output=True, timeout=10)
            except (subprocess.SubprocessError, OSError):
                pass
            LAUNCH_AGENT.unlink(missing_ok=True)

    # --- автообновление ---

    def update_artifact_name(self) -> str:
        return "TorFlash.new"

    def install_update(self, new_path: str, current_exe: str,
                       argv: "list[str]") -> None:
        # Релизный macOS-ассет — это .app.zip, а не «голый» бинарь, поэтому
        # in-place замена исполняемого файла невозможна (заменили бы mach-o на
        # zip и сломали .app). Честно отказываемся — обновление .app ставится
        # вручную. Подробности — docs/cross-platform.md.
        raise OSError("на macOS обновите TorFlash.app вручную из GitHub Releases")
