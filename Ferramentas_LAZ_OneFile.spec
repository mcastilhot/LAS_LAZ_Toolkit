# -*- mode: python ; coding: utf-8 -*-
# Gerador de executável em ARQUIVO ÚNICO (--onefile)
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
    'scipy.ndimage',
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
    'geopandas',
    'pyproj',
    'pyogrio',
    'skimage',
    'skimage.measure',
    'PySide6.QtCore',
    'PySide6.QtGui',
    'PySide6.QtWidgets',
]

pacotes = [
    'scipy',
    'rasterio',
    'pyproj',
    'pyogrio',
    'shapely',
    'skimage',
    'laspy',
    'lazrs',
    'laszip',
    'geopandas',
]

for opcional in ['pyvista', 'vtkmodules', 'matplotlib']:
    try:
        __import__(opcional)
        pacotes.append(opcional)
    except ImportError:
        pass

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
    a.binaries,
    a.datas,
    [],
    name='Ferramentas_LAZ_OneFile',
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

