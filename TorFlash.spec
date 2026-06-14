# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

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
)
