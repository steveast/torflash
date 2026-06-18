"""Windows-бэкенд: Win32 через ctypes (без сторонних зависимостей).

- Поиск флешки: перебор логических дисков, тип DRIVE_REMOVABLE.
- Форматирование: PowerShell Format-Volume в FAT32 (лимит ОС — тома ≤ 32 ГБ).
- Безопасное извлечение: FSCTL_LOCK/DISMOUNT_VOLUME + IOCTL_STORAGE_EJECT_MEDIA.
- Автозапуск: ключ реестра HKCU\\…\\Run.

ОС-специфичные импорты (ctypes.windll, winreg) — лениво внутри методов, чтобы
модуль импортировался и на других платформах (для тестов/анализа PyInstaller)."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from .base import OpResult, PlatformBackend, StatusCallback


DRIVE_REMOVABLE = 2

# Win32 IOCTL/FSCTL коды и флаги CreateFile.
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_DISMOUNT_VOLUME = 0x00090020
IOCTL_STORAGE_MEDIA_REMOVAL = 0x002D4804
IOCTL_STORAGE_EJECT_MEDIA = 0x002D4808
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "TorFlash"


class WindowsBackend(PlatformBackend):
    name = "windows"

    # --- сменный носитель: чтение ---

    def find_flash_mount(self) -> "str | None":
        import ctypes

        k32 = ctypes.windll.kernel32
        bitmask = k32.GetLogicalDrives()
        for i in range(26):
            if not (bitmask >> i) & 1:
                continue
            root = f"{chr(ord('A') + i)}:\\"
            if k32.GetDriveTypeW(ctypes.c_wchar_p(root)) != DRIVE_REMOVABLE:
                continue
            # Носитель вставлен и доступен на запись?
            if os.path.isdir(root) and os.access(root, os.W_OK):
                return root
        return None

    def flash_device(self, mount: str) -> "str | None":
        # На Windows «устройство» для операций — это сама буква диска ("E:\\").
        return mount if mount else None

    def _volume_info(self, mount: str) -> "tuple[str, str]":
        """(метка тома, имя ФС) через GetVolumeInformationW."""
        import ctypes

        root = mount if mount.endswith("\\") else mount + "\\"
        label = ctypes.create_unicode_buffer(261)
        fsname = ctypes.create_unicode_buffer(261)
        try:
            ok = ctypes.windll.kernel32.GetVolumeInformationW(
                ctypes.c_wchar_p(root), label, len(label),
                None, None, None, fsname, len(fsname),
            )
        except OSError:
            return "", ""
        if not ok:
            return "", ""
        return label.value, fsname.value

    def volume_label(self, mount: str) -> str:
        return self._volume_info(mount)[0]

    def flash_fstype(self, mount: str) -> str:
        return self._volume_info(mount)[1]

    # --- сменный носитель: операции ---

    @staticmethod
    def _sanitize_label(label: str) -> str:
        # Метка FAT32: до 11 символов, без спецсимволов.
        return re.sub(r"[^A-Za-z0-9 _-]", "", label).strip()[:11] or "FLASH"

    def format_fat32(self, mount: str, label: str,
                     on_status: StatusCallback = None) -> OpResult:
        import subprocess

        letter = mount[0]
        lbl = self._sanitize_label(label).replace("'", "''")
        if on_status:
            on_status("format")
        ps = (
            "$ErrorActionPreference='Stop';"
            "try {"
            f"Format-Volume -DriveLetter {letter} -FileSystem FAT32 "
            f"-NewFileSystemLabel '{lbl}' -Force -Confirm:$false | Out-Null;"
            " exit 0 } catch { Write-Error $_; exit 1 }"
        )
        try:
            res = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=300,
                creationflags=CREATE_NO_WINDOW,
            )
        except FileNotFoundError as e:
            return OpResult(ok=False, missing_tool=True, message=str(e))
        except subprocess.SubprocessError as e:
            return OpResult(ok=False, step="format", message=str(e))
        if res.returncode != 0:
            return OpResult(ok=False, step="format",
                            message=(res.stderr or res.stdout).strip(),
                            device=mount)
        return OpResult(ok=True, device=mount)

    def eject(self, mount: str, on_status: StatusCallback = None) -> OpResult:
        import ctypes
        import time
        from ctypes import wintypes

        letter = mount[0]
        k32 = ctypes.windll.kernel32
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        k32.DeviceIoControl.restype = wintypes.BOOL
        k32.DeviceIoControl.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        invalid = wintypes.HANDLE(-1).value

        if on_status:
            on_status("sync")
        handle = k32.CreateFileW(
            f"\\\\.\\{letter}:", GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None,
        )
        if not handle or handle == invalid:
            return OpResult(ok=False, step="error",
                            message=f"CreateFile failed (err {k32.GetLastError()})",
                            device=mount)

        returned = wintypes.DWORD(0)

        def ioctl(code, inbuf=None, insize=0) -> bool:
            return bool(k32.DeviceIoControl(
                handle, code, inbuf, insize, None, 0,
                ctypes.byref(returned), None,
            ))

        try:
            if on_status:
                on_status("unmount")
            # Lock может не пройти сразу, если том занят — пробуем несколько
            # раз с паузой, давая держателю дескрипторов время освободить том.
            # (any() с range(20) без паузы бил все попытки мгновенно подряд,
            # то есть фактически делал одну.)
            locked = False
            for _ in range(20):
                if ioctl(FSCTL_LOCK_VOLUME):
                    locked = True
                    break
                time.sleep(0.1)
            if not locked:
                return OpResult(ok=False, step="unmount",
                                message="volume is in use", device=mount)
            # Если размонтировать не удалось при захваченной блокировке —
            # не извлекаем (иначе рискуем выдернуть смонтированный том).
            if not ioctl(FSCTL_DISMOUNT_VOLUME):
                return OpResult(ok=False, step="unmount",
                                message="dismount failed", device=mount)
            # PREVENT_MEDIA_REMOVAL: один байт BOOLEAN = 0 (разрешить извлечение).
            allow = ctypes.create_string_buffer(1)
            ioctl(IOCTL_STORAGE_MEDIA_REMOVAL, allow, 1)

            if on_status:
                on_status("poweroff")
            if not ioctl(IOCTL_STORAGE_EJECT_MEDIA):
                return OpResult(ok=True, step="poweroff",
                                message=f"eject ioctl failed (err {k32.GetLastError()})",
                                device=mount)
            return OpResult(ok=True, device=mount)
        finally:
            k32.CloseHandle(handle)

    # --- интеграция с ОС ---

    def open_path(self, path: str) -> bool:
        try:
            os.startfile(path)  # noqa: возможно только на Windows
            return True
        except OSError:
            return False

    def _command(self) -> str:
        if getattr(sys, "frozen", False):
            return f'"{sys.executable}" --hidden'
        pyw = Path(sys.executable).with_name("pythonw.exe")
        exe = str(pyw) if pyw.exists() else sys.executable
        script = Path(sys.argv[0]).resolve()
        return f'"{exe}" "{script}" --hidden'

    def is_autostart_enabled(self) -> bool:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, RUN_VALUE)
            return True
        except OSError:
            return False

    def set_autostart(self, enabled: bool) -> None:
        import winreg

        if enabled:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, self._command())
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                    winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, RUN_VALUE)
            except OSError:
                pass

    # --- автообновление ---

    def update_artifact_name(self) -> str:
        return "TorFlash.exe.new"

    def install_update(self, new_path: str, current_exe: str,
                       argv: "list[str]") -> None:
        import subprocess

        # Запущенный .exe заблокирован — заменить его на лету нельзя. Пишем
        # .bat-хелпер: он ждёт выхода нашего PID, делает move и перезапуск.
        # Все ожидания ограничены по времени, а провал move фиксируется в
        # <new>.log (а не глотается >nul), чтобы обновление не «терялось молча».
        pid = os.getpid()
        extra = " ".join(f'"{a}"' for a in argv)
        log_path = f"{new_path}.log"
        bat = (
            "@echo off\r\n"
            "setlocal enableextensions\r\n"
            # ждём выхода нашего PID, но не дольше ~60 с (60 × ~1 с).
            "set WAIT=0\r\n"
            ":wait\r\n"
            f'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul || goto gone\r\n'
            "set /a WAIT+=1\r\n"
            f'if %WAIT% GEQ 60 (echo update aborted: pid {pid} still running > "{log_path}" '
            '& del "%~f0" & exit /b)\r\n'
            "ping -n 2 127.0.0.1 >nul & goto wait\r\n"
            ":gone\r\n"
            # move с ретраями: exe мог ещё не успеть отпустить файл.
            "set MOVE=0\r\n"
            ":domove\r\n"
            f'move /Y "{new_path}" "{current_exe}" >nul 2>&1 && goto launch\r\n'
            "set /a MOVE+=1\r\n"
            f'if %MOVE% GEQ 10 (echo update failed: cannot replace "{current_exe}" > "{log_path}" '
            f'& start "" "{current_exe}" {extra} & del "%~f0" & exit /b)\r\n'
            "ping -n 2 127.0.0.1 >nul & goto domove\r\n"
            ":launch\r\n"
            f'start "" "{current_exe}" {extra}\r\n'
            'del "%~f0"\r\n'
        )
        bat_path = Path(new_path).with_suffix(".bat")
        bat_path.write_text(bat, encoding="ascii")
        subprocess.Popen(
            ["cmd", "/c", str(bat_path)],
            creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
            close_fds=True,
        )
        # Возвращаемся: вызывающий обязан немедленно завершить приложение.
