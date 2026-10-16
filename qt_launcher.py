import ctypes
import os
import sys
import traceback
from pathlib import Path

base = Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent))
log_path = Path(sys.executable).parent / 'qt_startup_error.txt'
handles = []
load_steps = []

if getattr(sys, 'frozen', False):
    dll_dirs = [base, base / 'PySide6', base / 'shiboken6']
    for directory in dll_dirs:
        if directory.is_dir():
            try:
                handles.append(os.add_dll_directory(str(directory)))
            except OSError as exc:
                load_steps.append(f'AddDllDirectory {directory}: {exc}')
    system_root = Path(os.environ.get('SystemRoot', r'C:\Windows'))
    safe_dirs = [base, base / 'PySide6', base / 'shiboken6', system_root / 'System32', system_root]
    os.environ['PATH'] = os.pathsep.join(str(p) for p in safe_dirs if p.is_dir())

try:
    # Preload Qt and binding DLLs by absolute path. If one has a missing export,
    # the log identifies the exact binary before Python imports QtCore.pyd.
    for rel in [
        'PySide6/Qt6Core.dll',
        'PySide6/Qt6Gui.dll',
        'PySide6/Qt6Widgets.dll',
        'shiboken6/shiboken6.abi3.dll',
        'PySide6/pyside6.abi3.dll',
    ]:
        dll = base / rel
        if dll.exists():
            try:
                handles.append(ctypes.WinDLL(str(dll), winmode=0x00001100))
                load_steps.append(f'OK {dll}')
            except OSError as exc:
                load_steps.append(f'FAIL {dll}: {exc}')
                raise
        else:
            load_steps.append(f'MISSING {dll}')
    from PySide6.QtCore import Qt, QObject, QThread, Signal
    from PySide6.QtGui import QColor, QFontDatabase, QTextCharFormat, QTextCursor
    from PySide6.QtWidgets import QApplication
except Exception:
    win = sys.getwindowsversion()
    log_path.write_text(
        f'Executable: {sys.executable}\nBase: {base}\nWindows: {win.major}.{win.minor}.{win.build}\n'
        f'PATH: {os.environ.get("PATH", "")}\n\nDLL preload results:\n'
        + '\n'.join(load_steps) + '\n\n' + traceback.format_exc(), encoding='utf-8'
    )
    raise

import runpy
script = base / 'las_laz_toolkit.py'
runpy.run_path(str(script), run_name='__main__')
