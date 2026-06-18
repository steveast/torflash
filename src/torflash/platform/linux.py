"""Linux-бэкенд: udisks2 (udisksctl) + findmnt/lsblk/lsof, XDG-автозапуск.

Поведение перенесено из прежнего main_window/helpers без изменений: флешки
ищутся под /run/media/<user>, форматирование и извлечение идут через udisksctl,
автозапуск — через ~/.config/autostart/*.desktop."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from .. import config
from ..helpers import detect_flash_mount
from .base import OpResult, PlatformBackend, StatusCallback


class LinuxBackend(PlatformBackend):
    name = "linux"

    AUTOSTART_FILE = Path.home() / ".config" / "autostart" / "TorFlash.desktop"

    # --- сменный носитель: чтение ---

    def find_flash_mount(self) -> "str | None":
        return detect_flash_mount()

    def flash_device(self, mount: str) -> "str | None":
        """Узел устройства смонтированной папки через findmnt."""
        try:
            src = subprocess.run(
                ["findmnt", "-no", "SOURCE", mount],
                capture_output=True, text=True, check=True, timeout=5,
            ).stdout.strip()
            return src or None
        except (subprocess.SubprocessError, OSError):
            return None

    def volume_label(self, mount: str) -> str:
        # Под udisks2 каталог монтирования и есть метка тома.
        return Path(mount).name

    def flash_fstype(self, mount: str) -> str:
        try:
            return subprocess.run(
                ["findmnt", "-no", "FSTYPE", mount],
                capture_output=True, text=True, check=True, timeout=2,
            ).stdout.strip()
        except (subprocess.SubprocessError, OSError):
            return ""

    @staticmethod
    def _parent_device(src: str) -> str:
        """Родительский диск для раздела. lsblk PKNAME корректен для nvme/mmc
        (sdb1→sdb, но nvme0n1p1→nvme0n1), в отличие от обрезки цифр регэкспом."""
        try:
            out = subprocess.run(
                ["lsblk", "-no", "PKNAME", src],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip().splitlines()
            if out and out[0].strip():
                return f"/dev/{out[0].strip()}"
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            pass
        return re.sub(r"p?\d+$", "", src)

    @staticmethod
    def _busy_processes(mount: str) -> str:
        """Процессы, держащие mount (через lsof). '' если нет/недоступен."""
        try:
            lsof = subprocess.run(
                ["lsof", "+D", mount],
                capture_output=True, text=True, timeout=5,
            )
            lines = [l for l in lsof.stdout.splitlines()
                     if l and not l.startswith("COMMAND")]
            procs = set()
            for l in lines[:20]:
                parts = l.split()
                if len(parts) >= 2:
                    procs.add(f"{parts[0]}({parts[1]})")
            return ", ".join(sorted(procs))
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            return ""

    # --- сменный носитель: операции ---

    def format_fat32(self, mount: str, label: str,
                     on_status: StatusCallback = None) -> OpResult:
        device = self.flash_device(mount)
        if not device:
            return OpResult(ok=False, step="detect")
        try:
            subprocess.run(["sync"], check=False, timeout=10)
            if on_status:
                on_status("unmount")
            um = subprocess.run(
                ["udisksctl", "unmount", "-b", device],
                capture_output=True, text=True, timeout=15,
            )
            if um.returncode != 0:
                return OpResult(ok=False, step="unmount",
                                message=(um.stderr or um.stdout).strip(),
                                device=device)
            if on_status:
                on_status("format")
            fmt = subprocess.run(
                ["udisksctl", "format", "-b", device,
                 "--type", "vfat", "--label", label, "--no-user-interaction"],
                capture_output=True, text=True, timeout=120,
            )
            if fmt.returncode != 0:
                return OpResult(ok=False, step="format",
                                message=(fmt.stderr or fmt.stdout).strip(),
                                device=device)
            if on_status:
                on_status("remount")
            mnt = subprocess.run(
                ["udisksctl", "mount", "-b", device],
                capture_output=True, text=True, timeout=15,
            )
            if mnt.returncode != 0:
                return OpResult(ok=True, step="remount",
                                message=(mnt.stderr or mnt.stdout).strip(),
                                device=device)
            return OpResult(ok=True, device=device)
        except FileNotFoundError as e:
            return OpResult(ok=False, missing_tool=True, message=str(e))
        except subprocess.SubprocessError as e:
            return OpResult(ok=False, step="format", message=str(e))

    def eject(self, mount: str, on_status: StatusCallback = None) -> OpResult:
        device = self.flash_device(mount)
        if not device:
            return OpResult(ok=False, step="detect")
        parent = self._parent_device(device)
        try:
            if on_status:
                on_status("sync")
            subprocess.run(["sync"], check=False, timeout=15)

            if on_status:
                on_status("unmount")
            um = subprocess.run(
                ["udisksctl", "unmount", "-b", device],
                capture_output=True, text=True, timeout=20,
            )
            if um.returncode != 0:
                return OpResult(ok=False, step="unmount",
                                message=(um.stderr or um.stdout).strip(),
                                busy=self._busy_processes(mount), device=device)

            if on_status:
                on_status("poweroff")
            poff = subprocess.run(
                ["udisksctl", "power-off", "-b", parent],
                capture_output=True, text=True, timeout=20,
            )
            if poff.returncode != 0:
                # Размонтировано, но питание не сняли — вынимать всё равно безопасно.
                return OpResult(ok=True, step="poweroff",
                                message=(poff.stderr or poff.stdout).strip(),
                                device=device)
            return OpResult(ok=True, device=device)
        except FileNotFoundError as e:
            return OpResult(ok=False, missing_tool=True, message=str(e))
        except subprocess.SubprocessError as e:
            return OpResult(ok=False, step="error", message=str(e))

    # --- интеграция с ОС ---

    def open_path(self, path: str) -> bool:
        try:
            subprocess.Popen(["xdg-open", path])
            return True
        except OSError:
            return False

    def _entry_script(self) -> str:
        return str(Path(sys.argv[0]).resolve())

    def is_autostart_enabled(self) -> bool:
        return self.AUTOSTART_FILE.exists()

    def set_autostart(self, enabled: bool) -> None:
        if enabled:
            self.AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
            if getattr(sys, "frozen", False):
                exe = sys.executable
            else:
                exe = f"/usr/bin/python3 {self._entry_script()}"
            icon = config.ASSETS_DIR / "torflash.svg"
            content = (
                "[Desktop Entry]\n"
                "Type=Application\n"
                f"Name={config.APP_NAME}\n"
                f"Exec={exe} --hidden\n"
                f"Icon={icon}\n"
                "Terminal=false\n"
                "X-GNOME-Autostart-enabled=true\n"
                "X-KDE-autostart-after=panel\n"
            )
            self.AUTOSTART_FILE.write_text(content)
            self.AUTOSTART_FILE.chmod(0o755)
        else:
            self.AUTOSTART_FILE.unlink(missing_ok=True)

    # --- автообновление ---

    def update_artifact_name(self) -> str:
        return "TorFlash.new"

    def install_update(self, new_path: str, current_exe: str,
                       argv: "list[str]") -> None:
        # Замена inode запущенного ELF допустима — заменяем и перезапускаемся.
        os.replace(new_path, current_exe)
        os.chmod(current_exe, 0o755)
        os.execv(current_exe, [current_exe] + list(argv))
