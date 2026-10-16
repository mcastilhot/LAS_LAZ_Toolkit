# PySide6 + optional PyVista 3D viewer runtime. Exclude unrelated Poppler ICU.
from PyInstaller.utils.hooks import collect_all, get_module_file_attribute
from pathlib import Path
site = Path(get_module_file_attribute('PySide6')).parent
shib_site = Path(get_module_file_attribute('shiboken6')).parent
qt_bins = []
for dll in ['Qt6Core.dll', 'Qt6Gui.dll', 'Qt6Widgets.dll']:
    qt_bins.append((str(site / dll), 'PySide6'))
for module in ['QtCore', 'QtGui', 'QtWidgets']:
    qt_bins.append((str(site / f'{module}.pyd'), 'PySide6'))
qt_bins.append((str(shib_site / 'shiboken6.abi3.dll'), 'shiboken6'))
qt_bins.append((str(shib_site / 'Shiboken.pyd'), 'shiboken6'))
for dll in shib_site.glob('*.dll'):
    qt_bins.append((str(dll), 'shiboken6'))
plugin = site / 'plugins/platforms/qwindows.dll'
if plugin.exists():
    qt_bins.append((str(plugin), 'PySide6/plugins/platforms'))
all_datas, all_bins, all_hidden = [], [], []
for package in ['pyvista', 'vtkmodules', 'matplotlib']:
    package_datas, package_bins, package_hidden = collect_all(package)
    all_datas += package_datas
    all_bins += package_bins
    all_hidden += package_hidden
a = Analysis(
    ['las_laz_toolkit.py'], pathex=[], binaries=qt_bins + all_bins, datas=all_datas,
    hiddenimports=['lazrs', 'PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets'] + all_hidden,
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['PySide6.scripts', 'PySide6.support'], noarchive=False, optimize=0,
)
a.binaries = [entry for entry in a.binaries
              if Path(entry[0]).name.lower() not in {'icuuc.dll', 'icuin.dll'}
              and not Path(entry[0]).name.lower().startswith('icudt')]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name='LAS_LAZ_Toolkit', debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False, disable_windowed_traceback=False,
    argv_emulation=False, target_arch=None, codesign_identity=None, entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='LAS_LAZ_Toolkit')
