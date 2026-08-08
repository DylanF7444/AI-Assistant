# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_submodules
from PyInstaller.utils.hooks.qt import pyside6_library_info

hiddenimports = collect_submodules("assistant")
stt_datas, stt_binaries, stt_hiddenimports = collect_all("faster_whisper")
ctranslate_datas, ctranslate_binaries, ctranslate_hiddenimports = collect_all(
    "ctranslate2"
)
ctranslate_hiddenimports = [
    module
    for module in ctranslate_hiddenimports
    if not module.startswith("ctranslate2.converters")
]
playwright_datas, playwright_binaries, playwright_hiddenimports = collect_all("playwright")
document_datas, document_binaries, document_hiddenimports = collect_all("pypdf")
image_datas, image_binaries, image_hiddenimports = collect_all("PIL")
ocr_datas, ocr_binaries, ocr_hiddenimports = collect_all("pytesseract")
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
        + document_binaries
        + image_binaries
        + ocr_binaries
        + qt_plugin_binaries
        + qt_extra_binaries
    ),
    datas=(
        [("../docs", "docs")]
        + stt_datas
        + ctranslate_datas
        + playwright_datas
        + document_datas
        + image_datas
        + ocr_datas
    ),
    hiddenimports=hiddenimports + document_hiddenimports + image_hiddenimports + ocr_hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Keep the release artifact independent from unrelated optional packages in
    # the build machine's global environment. These stacks are not used by the
    # Assistant runtime and are not part of the audited production dependency graph.
    excludes=[
        "ctranslate2.converters",
        "datasets",
        "langchain",
        "langchain_core",
        "langchain_openai",
        "langchain_text_splitters",
        "langsmith",
        "pandas",
        "pyarrow",
        "sentence_transformers",
        "streamlit",
        "torch",
        "transformers",
        "yt_dlp",
    ],
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
