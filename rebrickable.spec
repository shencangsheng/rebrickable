# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — run: pyinstaller rebrickable.spec

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None
root = Path(SPECPATH)

# Selenium 4 lazily imports driver submodules via __getattr__; static analysis misses them.
selenium_datas, selenium_binaries, selenium_hidden = collect_all("selenium")
wdm_datas, wdm_binaries, wdm_hidden = collect_all("webdriver_manager")

a = Analysis(
    [str(root / "app.py")],
    pathex=[str(root)],
    binaries=selenium_binaries + wdm_binaries,
    datas=[(str(root / "templates"), "templates")] + selenium_datas + wdm_datas,
    hiddenimports=[
        "werkzeug",
        "jinja2",
        "openpyxl",
        "bs4",
        "meta",
        *selenium_hidden,
        *collect_submodules("selenium"),
        *wdm_hidden,
        *collect_submodules("webdriver_manager"),
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
    upx_exclude=["selenium-manager", "selenium-manager.exe"],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
