# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['nv_compressor.py'],
    pathex=[],
    binaries=[('ffmpeg.exe', '.')],
    datas=[('app.ico', '.')],
    hiddenimports=['tkinterdnd2'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PIL', 'numpy', 'pandas', 'matplotlib', 'scipy', 'PyQt5', 'PyQt6',
              'PySide2', 'PySide6', 'IPython', 'pytest', 'unittest', 'pydoc',
              'distutils', 'setuptools', 'pip', 'sqlite3', 'lib2to3', 'test'],
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
    name='TheCompressor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['app.ico'],
)
