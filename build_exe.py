"""Build script for packaging DocForensics Analyzer into a standalone Windows executable and zip distribution."""

import os
import shutil
import subprocess
import sys
import time
import zipfile


def print_banner(msg: str) -> None:
    sep = "=" * 70
    print(f"\n{sep}\n  {msg}\n{sep}")


def check_prerequisites() -> None:
    print_banner("1. Checking prerequisites")
    try:
        import PySide6  # noqa: F401
        print("  [OK] PySide6 installed")
    except ImportError:
        sys.exit("Error: PySide6 is not installed. Run 'pip install -r requirements.txt'")

    try:
        import pymupdf  # noqa: F401
        print("  [OK] PyMuPDF installed")
    except ImportError:
        sys.exit("Error: PyMuPDF is not installed. Run 'pip install -r requirements.txt'")

    try:
        import pdfplumber  # noqa: F401
        print("  [OK] pdfplumber installed")
    except ImportError:
        sys.exit("Error: pdfplumber is not installed. Run 'pip install -r requirements.txt'")

    try:
        import PyInstaller  # noqa: F401
        print(f"  [OK] PyInstaller installed (v{PyInstaller.__version__})")
    except ImportError:
        sys.exit("Error: PyInstaller is not installed. Run 'pip install pyinstaller'")

    if not os.path.exists("assets/icon.ico"):
        sys.exit("Error: assets/icon.ico not found.")
    print("  [OK] Assets and application icon verified")


def clean_build_artifacts() -> None:
    print_banner("2. Cleaning previous build artifacts")
    for folder in ["build"]:
        if os.path.exists(folder):
            print(f"  Removing {folder}/...")
            shutil.rmtree(folder, ignore_errors=True)
    dist_app = os.path.join("dist", "DocForensicsAnalyzer")
    if os.path.exists(dist_app):
        print(f"  Removing {dist_app}...")
        shutil.rmtree(dist_app, ignore_errors=True)
    print("  [OK] Clean complete")


def run_pyinstaller() -> None:
    print_banner("3. Running PyInstaller with docforensics.spec")
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "docforensics.spec",
    ]
    start_time = time.time()
    res = subprocess.run(cmd)
    elapsed = time.time() - start_time
    if res.returncode != 0:
        sys.exit(f"\nPyInstaller build failed with exit code {res.returncode}")
    print(f"  [OK] PyInstaller compilation completed in {elapsed:.1f}s")


def verify_build() -> str:
    print_banner("4. Verifying built executable")
    exe_path = os.path.join("dist", "DocForensicsAnalyzer", "DocForensicsAnalyzer.exe")
    if not os.path.isfile(exe_path):
        sys.exit(f"Error: Executable not found at {exe_path}")

    size_mb = os.path.getsize(exe_path) / (1024 * 1024)
    print(f"  [OK] Found executable: {exe_path} ({size_mb:.2f} MB)")
    return exe_path


def create_zip_archive() -> str:
    print_banner("5. Creating portable ZIP package")
    dist_dir = os.path.join("dist", "DocForensicsAnalyzer")
    zip_filename = os.path.join("dist", "DocForensicsAnalyzer-windows-x64.zip")

    if os.path.exists(zip_filename):
        os.remove(zip_filename)

    print(f"  Archiving {dist_dir} into {zip_filename}...")
    with zipfile.ZipFile(zip_filename, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(dist_dir):
            for file in files:
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, "dist")
                zf.write(full_path, rel_path)

    zip_size_mb = os.path.getsize(zip_filename) / (1024 * 1024)
    print(f"  [OK] Distribution package created: {zip_filename} ({zip_size_mb:.2f} MB)")
    return zip_filename


def main() -> None:
    print_banner("DocForensics Analyzer Packaging Pipeline")
    check_prerequisites()
    clean_build_artifacts()
    run_pyinstaller()
    exe_path = verify_build()
    zip_path = create_zip_archive()
    print_banner("Packaging SUCCESSFUL!")
    print(f"  * Standalone Folder:  dist/DocForensicsAnalyzer/")
    print(f"  * Primary Executable: {exe_path}")
    print(f"  * Portable Archive:   {zip_path}")
    print("\nYou can distribute the ZIP file or run DocForensicsAnalyzer.exe directly.\n")


if __name__ == "__main__":
    main()
