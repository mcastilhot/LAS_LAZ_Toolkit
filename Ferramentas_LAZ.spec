# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

all_datas = []
all_binaries = []
all_hiddenimports = [
    'unittest',
    'unittest.mock',
    'numpy.testing',
    'lazrs',
    'laszip',
    'laspy',
    'scipy',
    'scipy.spatial',
    'scipy.spatial.transform',
    'scipy.special',
    'scipy.linalg',
    'rasterio',
    'rasterio.io',
    'rasterio.windows',
    'rasterio.transform',
    'rasterio.merge',
    'shapely',
    'shapely.geometry',
    'shapely.strtree',
    'pyproj',
    'PySide6.QtCore',
    'PySide6.QtGui',
    'PySide6.QtWidgets',
]

# Pacotes essenciais coletados integralmente
pacotes = [
    'scipy',
    'rasterio',
    'pyproj',
    'shapely',
    'laspy',
    'lazrs',
    'laszip',
]

for pkg in pacotes:
    try:
        d, b, h = collect_all(pkg)
        all_datas += d
        all_binaries += b
        all_hiddenimports += h
    except Exception as exc:
        print(f"[Aviso] Erro ao coletar '{pkg}': {exc}")

a = Analysis(
    ['Ferramentas_LAZ.py'],
    pathex=[],
    binaries=all_binaries,
    datas=all_datas,
    hiddenimports=list(dict.fromkeys(all_hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

a.binaries = [
    entry for entry in a.binaries
    if Path(entry[0]).name.lower() not in {'icuuc.dll', 'icuin.dll'}
    and not Path(entry[0]).name.lower().startswith('icudt')
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Ferramentas_LAZ',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Ferramentas_LAZ',
)
