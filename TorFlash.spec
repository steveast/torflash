# -*- mode: python ; coding: utf-8 -*-
import os
import sys

from PyInstaller.utils.hooks import collect_all

sys.path.insert(0, os.path.join(SPECPATH, 'src'))
from torflash.config import APP_VERSION

datas = [
    ('assets/torflash.svg', '.'),
    ('assets/torflash-tray.svg', '.'),
    ('assets/torflash-tray-22.png', '.'),
    ('assets/torflash-tray-32.png', '.'),
    ('assets/torflash-tray-48.png', '.'),
]
binaries = []
hiddenimports = [
    'requests',
    'torflash', 'torflash.config', 'torflash.i18n', 'torflash.helpers',
    'torflash.runtime', 'torflash.dl_slot', 'torflash.meta', 'torflash.mediainfo', 'torflash.themes',
    'torflash.session', 'torflash.session.seed_session', 'torflash.session.download_worker',
    'torflash.workers', 'torflash.workers.copy_worker', 'torflash.workers.search_worker',
    'torflash.workers.meta_fetcher', 'torflash.workers.poster_fetcher',
    'torflash.update', 'torflash.update.checker', 'torflash.update.downloader',
    'torflash.update.signature',
    'torflash.widgets', 'torflash.widgets.speed_graph', 'torflash.widgets.sortable_item',
    'torflash.ui', 'torflash.ui.main_window',
    'torflash.providers', 'torflash.providers.base', 'torflash.providers.rutor',
    'torflash.providers.nnm', 'torflash.providers.rutracker',
    # Платформенные бэкенды импортируются лениво в torflash/platform/__init__.py
    # (по sys.platform) — статический анализ PyInstaller их не находит.
    'torflash.platform', 'torflash.platform.base', 'torflash.platform.linux',
    'torflash.platform.windows', 'torflash.platform.macos',
]
tmp_ret = collect_all('requests')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# cryptography — для проверки minisign-подписи обновлений (ленивый импорт в
# torflash/update/signature.py, поэтому тянем явно).
tmp_ret = collect_all('cryptography')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['src/rutor_search.py'],
    pathex=['src'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)


# Иконка приложения, специфичная для платформы (если файл присутствует).
# Windows: .ico, macOS: .icns. На Linux иконку даёт .desktop/тема.
def _app_icon():
    if sys.platform == 'win32':
        ico = os.path.join('assets', 'torflash.ico')
        return ico if os.path.exists(ico) else None
    if sys.platform == 'darwin':
        icns = os.path.join('assets', 'torflash.icns')
        return icns if os.path.exists(icns) else None
    return None


app_icon = _app_icon()

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='TorFlash',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=app_icon,
)

# macOS: оборачиваем исполняемый файл в .app-бандл (нужно для нормального
# запуска GUI и последующей подписи/нотаризации).
if sys.platform == 'darwin':
    app = BUNDLE(
        exe,
        name='TorFlash.app',
        icon=app_icon,
        bundle_identifier='pro.torflash',
        info_plist={
            'CFBundleName': 'TorFlash',
            'CFBundleDisplayName': 'TorFlash',
            'CFBundleShortVersionString': APP_VERSION,
            'NSHighResolutionCapable': True,
            'LSMinimumSystemVersion': '11.0',
        },
    )
