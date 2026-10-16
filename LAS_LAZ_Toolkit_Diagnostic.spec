from PyInstaller.utils.hooks import get_module_file_attribute
from pathlib import Path
site = Path(get_module_file_attribute('PySide6')).parent
shib_site = Path(get_module_file_attribute('shiboken6')).parent
binaries = []
for dll in ['Qt6Core.dll', 'Qt6Gui.dll', 'Qt6Widgets.dll']:
    binaries.append((str(site / dll), 'PySide6'))
for module in ['QtCore', 'QtGui', 'QtWidgets']:
    binaries.append((str(site / f'{module}.pyd'), 'PySide6'))
binaries.append((str(shib_site / 'shiboken6.abi3.dll'), 'shiboken6'))
binaries.append((str(shib_site / 'Shiboken.pyd'), 'shiboken6'))
for dll in shib_site.glob('*.dll'):
    binaries.append((str(dll), 'shiboken6'))
for plugin in ['platforms/qwindows.dll']:
    p = site / 'plugins' / plugin
    if p.exists():
        binaries.append((str(p), str(Path('PySide6/plugins') / Path(plugin).parent)))
datas = [('las_laz_toolkit.py', '.')]
a = Analysis(
    ['qt_launcher.py'], pathex=[], binaries=binaries, datas=datas,
    hiddenimports=['lazrs', 'PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets'],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['PySide6.scripts', 'PySide6.support'], noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name='LAS_LAZ_Toolkit_DIAGNOSTIC', debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False, disable_windowed_traceback=False,
    argv_emulation=False, target_arch=None, codesign_identity=None, entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='LAS_LAZ_Toolkit_DIAGNOSTIC')
