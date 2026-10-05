# Packaging DocForensics Analyzer

This guide details how to build and package **DocForensics Analyzer** into a standalone Windows executable and portable distribution archive.

---

## 1. Quick Packaging (One-Click)

On Windows, simply run:

```cmd
package.bat
```

or via Python / PowerShell:

```powershell
python build_exe.py
```

---

## 2. What the Packaging Pipeline Does

The build pipeline (`build_exe.py` + `docforensics.spec`):
1. **Validates Prerequisites**: Checks that `PySide6`, `PyMuPDF`, `pdfplumber`, `python-docx`, `python-pptx`, `openpyxl`, `pywin32`, and `PyInstaller` are present.
2. **Collects Critical Assets**:
   - Application icon (`assets/icon.ico` & `assets/icon.png`)
   - `python-docx` templates and schemas
   - `python-pptx` layout templates
   - `pdfminer` CJK and Unicode character maps / encodings
   - `PyMuPDF` rendering binaries and font libraries
   - PySide6 Qt GUI plugins (platform windows, styles, image formats)
3. **Applies Windows Metadata**: Embeds PE version information (`version_info.txt`), manufacturer ("DocForensics"), product name, and icon into `DocForensicsAnalyzer.exe`.
4. **Enables Frozen Multiprocessing**: Supports `multiprocessing.freeze_support()` so background multi-core extraction workers execute smoothly without recursive process spawning.
5. **Creates Output Artifacts**:
   - `dist/DocForensicsAnalyzer/`: Standalone application directory (ready to run without Python installed).
   - `dist/DocForensicsAnalyzer-windows-x64.zip`: Compressed archive for easy distribution.

---

## 3. Manual PyInstaller Build

If you wish to invoke PyInstaller directly:

```powershell
pyinstaller docforensics.spec --noconfirm
```

Output directory will be created at:
```
dist/DocForensicsAnalyzer/DocForensicsAnalyzer.exe
```

---

## 4. Standard Python Package (Wheel / Pip)

The repository also includes `pyproject.toml` for standard Python installations:

```powershell
# Editable installation for development
pip install -e .

# Build standard wheel and sdist
python -m build
```
Once installed via pip, the app can be launched directly from any terminal using:
```powershell
docforensics
```
