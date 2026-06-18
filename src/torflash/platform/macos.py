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
from pathlib import Path

from .base import OpResult, PlatformBackend, StatusCallback


LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / "pro.torflash.autostart.plist"
LAUNCH_LABEL = "pro.torflash.autostart"


class MacOSBackend(PlatformBackend):
    name = "macos"

    # --- сменный носитель: чтение ---

    def _diskutil_info(self, target: str) -> dict:
        """diskutil info -plist <target> → dict (пусто при ошибке)."""
        try:
            out = subprocess.run(
                ["diskutil", "info", "-plist", target],
                capture_output=True, timeout=10,
            )
            if out.returncode != 0:
                return {}
            return plistlib.loads(out.stdout)
        except (subprocess.SubprocessError, OSError, plistlib.InvalidFileException):
            return {}

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
        # Работает для «голого» исполняемого файла. Для .app-бандла in-place
        # замена не годится — там обновление ставится из .dmg вручную.
        os.replace(new_path, current_exe)
        os.chmod(current_exe, 0o755)
        os.execv(current_exe, [current_exe] + list(argv))
