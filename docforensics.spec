# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification file for DocForensics Analyzer.

Builds a standalone Windows application directory bundle with:
- PySide6 GUI runtime & Qt plugins
- PyMuPDF rendering engine
- pdfplumber & pdfminer layout parser with cmaps and encodings
- Office document support (.docx, .pptx, .xlsx) with template data
- Windows COM / pywin32 integration
- Custom application icon and Windows PE version metadata
"""

import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# Collect package data files for templates, cmaps, schemas
docx_datas = collect_data_files("docx")
pptx_datas = collect_data_files("pptx")
pdfminer_datas = collect_data_files("pdfminer")
pdfplumber_datas = collect_data_files("pdfplumber")
pymupdf_datas = collect_data_files("pymupdf")

datas = [
    ("assets", "assets"),
] + docx_datas + pptx_datas + pdfminer_datas + pdfplumber_datas + pymupdf_datas

hiddenimports = [
    "core",
    "core.char_compare",
    "core.coverage_check",
    "core.diff_engine",
    "core.document_extractor",
    "core.fast_tables",
    "core.inline_diff",
    "core.mupdf_page",
    "core.office_convert",
    "core.pdf_extractor",
    "core.pdf_merge",
    "core.rematch",
    "core.report",
    "core.review_store",
    "core.script_text",
    "core.section_diff",
    "core.token_map",
    "core.word_pool",
    "ui",
    "ui.comparator_panel",
    "ui.crop_dialog",
    "ui.document_panel",
    "ui.flow_layout",
    "ui.merge_dialog",
    "ui.results_panel",
    "ui.theme",
    "workers",
    "workers.convert_worker",
    "workers.diff_worker",
    "workers.extraction_cache",
    "fitz",
    "pymupdf",
    "pdfplumber",
    "pdfminer",
    "pdfminer.high_level",
    "pdfminer.layout",
    "pdfminer.six",
    "diff_match_patch",
    "docx",
    "pptx",
    "openpyxl",
    "numpy",
    "win32api",
    "win32con",
    "win32com",
    "win32com.client",
    "pythoncom",
] + collect_submodules("diff_match_patch") + collect_submodules("pymupdf")

# Exclude unrelated heavyweight machine learning & web packages present in local environment
excludes = [
    "tkinter",
    "matplotlib",
    "scipy",
    "torch",
    "torchvision",
    "transformers",
    "paddleocr",
    "paddlepaddle",
    "paddlex",
    "redis",
    "psycopg",
    "sqlalchemy",
    "streamlit",
    "selenium",
    "pytest",
    "IPython",
    "jupyter",
]

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DocForensicsAnalyzer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/icon.ico",
    version="version_info.txt",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="DocForensicsAnalyzer",
)
