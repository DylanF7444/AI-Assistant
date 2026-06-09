# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_submodules

hiddenimports = collect_submodules("assistant")
stt_datas, stt_binaries, stt_hiddenimports = collect_all("faster_whisper")
ctranslate_datas, ctranslate_binaries, ctranslate_hiddenimports = collect_all(
    "ctranslate2"
)
hiddenimports += stt_hiddenimports + ctranslate_hiddenimports

a = Analysis(
    ["../src/assistant/app.py"],
    pathex=["../src"],
    binaries=stt_binaries + ctranslate_binaries,
    datas=[("../docs", "docs")] + stt_datas + ctranslate_datas,
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
