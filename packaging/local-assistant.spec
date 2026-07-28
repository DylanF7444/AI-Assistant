# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_submodules
from PyInstaller.utils.hooks.qt import pyside6_library_info

hiddenimports = collect_submodules("assistant")
stt_datas, stt_binaries, stt_hiddenimports = collect_all("faster_whisper")
ctranslate_datas, ctranslate_binaries, ctranslate_hiddenimports = collect_all(
    "ctranslate2"
)
playwright_datas, playwright_binaries, playwright_hiddenimports = collect_all("playwright")
qt_plugin_binaries = []
for plugin_type in ("imageformats", "multimedia", "platforms", "styles", "tls"):
    qt_plugin_binaries += pyside6_library_info.collect_plugins(plugin_type)
qt_extra_binaries = pyside6_library_info.collect_extra_binaries()
hiddenimports += (
    stt_hiddenimports
    + ctranslate_hiddenimports
    + playwright_hiddenimports
    + [
        "PySide6.QtGui",
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
        "PySide6.QtWidgets",
    ]
)

a = Analysis(
    ["../src/assistant/app.py"],
    pathex=["../src"],
    binaries=(
        stt_binaries
        + ctranslate_binaries
        + playwright_binaries
        + qt_plugin_binaries
        + qt_extra_binaries
    ),
    datas=[("../docs", "docs")] + stt_datas + ctranslate_datas + playwright_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="LocalAssistant",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
