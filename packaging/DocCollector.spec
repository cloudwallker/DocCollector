# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for a directory-based (onedir) portable DocCollector build.

An unpacked directory makes the portable package easier to troubleshoot: inspect
the bundled DLLs / Qt plugins and see real startup errors instead of a temp-dir
extraction failure.

Build (from the repository root, with PyInstaller installed):

    pyinstaller packaging/DocCollector.spec --noconfirm

Output lands in  dist/DocCollector/  — copy that whole folder; run
DocCollector.exe inside it. No Python install is required on the target machine.
"""
import os

block_cipher = None
ROOT = os.path.abspath(os.path.dirname(SPEC))
REPO = os.path.abspath(os.path.join(ROOT, os.pardir))

a = Analysis(
    [os.path.join(REPO, "main.py")],
    pathex=[REPO],
    binaries=[],
    datas=[],
    hiddenimports=[
        # Extractors are imported lazily by extension; make sure PyInstaller
        # keeps them and their optional encoders reachable.
        "doccollector.extractors.txt",
        "doccollector.extractors.markdown",
        "doccollector.extractors.json_ext",
        "doccollector.extractors.csv" if os.path.exists(os.path.join(REPO, "doccollector", "extractors", "csv.py")) else "doccollector.extractors.txt",
        "doccollector.extractors.pdf",
        "doccollector.extractors.docx",
        "charset_normalizer",
        "chardet",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6"],
    win_no_prefer_redirects=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DocCollector",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # windowed GUI app; flip to True to see tracebacks
    disable_windowed_traceback=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="DocCollector",
)
