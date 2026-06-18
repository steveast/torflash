# Кроссплатформенность: Windows и macOS

TorFlash исторически — Linux-приложение (PyQt5 + libtorrent + udisks2). Поддержка
Windows и macOS добавлена через платформенный слой и матрицу сборки в CI. Этот
документ описывает архитектуру, что работает на каждой ОС и что требует ручных
шагов (в первую очередь — подпись/нотаризация macOS).

## Платформенный слой

Вся ОС-зависимая логика спрятана за интерфейсом `PlatformBackend`
(`src/torflash/platform/base.py`). Реализации — по одному классу на файл:

| Файл | ОС | Технологии |
|---|---|---|
| `platform/linux.py` | Linux | `udisksctl`, `findmnt`, `lsblk`, `lsof`, `xdg-open`, XDG autostart |
| `platform/windows.py` | Windows | Win32 через `ctypes`, PowerShell `Format-Volume`, реестр `HKCU\…\Run` |
| `platform/macos.py` | macOS | `diskutil`, `open`, LaunchAgent |

Готовый синглтон выбирается по `sys.platform`:

```python
from torflash.platform import backend
mount = backend.find_flash_mount()
```

UI (`ui/main_window.py`) не содержит ни одного `udisksctl`/`xdg-open`/реестрового
вызова — только обращения к `backend`. Локализация остаётся в UI: бэкенд
возвращает структурный `OpResult` и шлёт ключи прогресса в колбэк `on_status`.

### Что делает каждый бэкенд

| Возможность | Linux | Windows | macOS |
|---|---|---|---|
| Поиск флешки | `/run/media/<user>/*` | removable-диски (DRIVE_REMOVABLE) | `/Volumes/*` (External/Removable) |
| Форматирование FAT32 | `udisksctl format vfat` | `Format-Volume -FileSystem FAT32` | `diskutil eraseVolume MS-DOS FAT32` |
| Безопасное извлечение | unmount + power-off | FSCTL_LOCK/DISMOUNT + IOCTL_STORAGE_EJECT_MEDIA | `diskutil eject` |
| Открыть в проводнике | `xdg-open` | `os.startfile` | `open` |
| Автозапуск | `~/.config/autostart/*.desktop` | реестр `HKCU\…\Run` | LaunchAgent `~/Library/LaunchAgents` |
| Самообновление | `os.replace` + `execv` | отложенный `.bat` (ждёт PID → move → restart) | `os.replace` + `execv` (только bare-binary) |
| Каталог данных | `$XDG_DATA_HOME/TorFlash` | `%LOCALAPPDATA%\TorFlash` | `~/Library/Application Support/TorFlash` |

### Граничные особенности (graceful degrade)

- **mkvmerge / mediainfo** вызываются через `shutil.which()`. Если их нет (как по
  умолчанию на Windows/macOS), MKV режется сырым сплитом, а медиаинфо просто
  недоступно — без падений. При желании их можно добавить в установщик.
- **«Открыть в KTorrent»** — Linux-only удобство; на других ОС `shutil.which`
  вернёт None и кнопка сообщит «KTorrent не найден».
- **Уведомления**: на Linux — `notify-send`, иначе — системный трей.

## Сборка локально

```bash
# Windows / macOS — самодостаточная pip-сборка:
pip install -r requirements/build.txt -r requirements/native.txt
# Linux — PyQt5/libtorrent из системных пакетов, ставим только сборочные:
#   pip install -r requirements/build.txt
pyinstaller --clean --noconfirm TorFlash.spec
```

- **Windows** → `dist\TorFlash.exe` (иконка `assets/torflash.ico`).
- **macOS** → `dist/TorFlash.app` (+ bare `dist/TorFlash`).
- **Linux** → `dist/TorFlash` (см. также `scripts/build-appimage.sh`).

`libtorrent` и `PyQt5` ставятся pip-колёсами на Windows/macOS; на Linux в CI они
берутся из apt (`python3-libtorrent`, `python3-pyqt5`). Если для конкретной версии
Python нет колеса libtorrent — закрепите версию (например `libtorrent==2.0.11`)
или поднимите/опустите `python-version`.

## CI

`.github/workflows/build.yml` собирает три платформы:

- `build` (ubuntu-22.04): `TorFlash` + `TorFlash-x86_64.AppImage`.
- `build-windows` (windows-latest): `TorFlash.exe`.
- `build-macos` (macos-latest, `continue-on-error`): `TorFlash-macos.app.zip`.

Каждый артефакт получает `.sha256` и (при заданном secret `MINISIGN_KEY`)
`.minisig`. Джоб `release` собирает все артефакты в один GitHub-релиз. Имя
Linux-бинарника осталось `TorFlash` — на него завязан AUR-пакет (не переименовывать).

Автообновление в приложении выбирает ассет под текущую ОС
(`torflash/update/assets.py::select_platform_asset`, покрыто тестами).

## macOS: подпись и нотаризация (требуется вручную)

Без подписи Gatekeeper на чужой машине покажет «TorFlash is damaged / cannot be
opened». Чтобы `.app` запускался у пользователей, нужен **Apple Developer ID**
(платная программа, ~$99/год) и нотаризация. Шаги (выполняются на macOS-раннере
или локально на маке):

1. **Импорт сертификата Developer ID Application** в keychain (в CI — из secret
   с base64 `.p12` через `security import`).
2. **Подпись** бандла с hardened runtime:
   ```bash
   codesign --deep --force --options runtime --timestamp \
     --sign "Developer ID Application: <Имя> (<TEAMID>)" dist/TorFlash.app
   ```
3. **Нотаризация** через `notarytool` (нужны Apple ID / app-specific password или
   API-ключ App Store Connect):
   ```bash
   ditto -c -k --keepParent dist/TorFlash.app TorFlash.zip
   xcrun notarytool submit TorFlash.zip \
     --apple-id "<apple-id>" --team-id "<TEAMID>" --password "<app-pw>" --wait
   ```
4. **Staple** тикета, чтобы работало офлайн:
   ```bash
   xcrun stapler staple dist/TorFlash.app
   ```
5. Заново упаковать (`ditto`) и выложить как `TorFlash-macos.app.zip`.

В `TorFlash.spec` есть `codesign_identity`/`entitlements_file` (сейчас `None`) —
их можно задать, чтобы PyInstaller подписывал на этапе сборки. Нотаризация всё
равно остаётся отдельным шагом.

> **Ограничение самообновления на macOS.** Встроенное обновление заменяет один
> исполняемый файл (модель Linux). Для `.app`-бандла это не годится: на macOS
> обновляйтесь, скачав свежий `.app.zip` вручную. Полноценный self-update под mac
> (через Sparkle или замену всего бандла) — отдельная задача.

## Windows: подпись (опционально)

`.exe` запускается и без подписи, но SmartScreen может предупреждать. Для тихого
запуска нужен сертификат Authenticode (EV/OV) и `signtool sign`. Не блокирует
работу — это polish.
