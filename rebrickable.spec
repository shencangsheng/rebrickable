# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — run: pyinstaller rebrickable.spec

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

block_cipher = None
root = Path(SPECPATH)

pillow_datas, pillow_binaries, pillow_hidden = collect_all("PIL")

a = Analysis(
    [str(root / "app.py")],
    pathex=[str(root)],
    binaries=pillow_binaries,
    datas=[(str(root / "templates"), "templates")] + pillow_datas,
    hiddenimports=[
        "werkzeug",
        "jinja2",
        "openpyxl",
        "PIL",
        "PIL.Image",
        "bs4",
        "meta",
        "logging_config",
        *pillow_hidden,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="rebrickable-backend",
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
