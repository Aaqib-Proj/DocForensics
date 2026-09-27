"""Faithful DOCX -> PDF conversion (core/office_convert.py).

A Word document has no fixed layout of its own: pagination, table
borders, columns, headers and text boxes only exist once a real layout
engine has set the page. Re-implementing that layout from python-docx
gives a rough approximation at best, so DOCX files are instead
converted to PDF by an actual engine, and that PDF is both what the
viewer shows and what gets extracted and compared -- the same pipeline
real PDFs use, so preview, highlights and comparison always agree.

Engines, in order of preference (Word runs as warm, reusable background
instances when the app has started them -- see core/word_pool.py):
  1. Microsoft Word (COM automation, Windows) -- exact Word layout.
  2. LibreOffice (``soffice --headless``) -- very close, cross-platform.

If neither is installed, ``ConverterUnavailableError`` is raised and
callers fall back to the approximate python-docx layout. Any other
failure (corrupt file, password-protected document, ...) raises
``ConversionError`` so it is reported rather than silently degraded.

Tracked changes are exported as the FINAL document (no markup), and
comments are not included: the comparison is about what the document
says, not its review history.

Converted PDFs are cached per source file (path, modification time,
size) under the user's local app-data folder, so the viewer and the
background extraction process -- which both need the PDF -- convert
only once, and re-opening an unchanged file is instant.

No Qt imports -- standalone-testable, and safe to call from worker
threads and worker processes.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import uuid

logger = logging.getLogger(__name__)

CONVERTIBLE_EXTENSIONS = {".docx"}

# Word's WdExportFormat / WdRevisionsMarkup / WdRevisionsView values.
_WD_EXPORT_FORMAT_PDF = 17
_WD_REVISIONS_MARKUP_NONE = 0
_WD_REVISIONS_VIEW_FINAL = 0

# Only what the viewer and the text extraction need. Skipping
# accessibility tags, bookmarks, document properties and print-quality
# images cut export time by roughly a quarter on a 46-page label, with
# identical extracted text and word positions. BitmapMissingFonts=False
# keeps text in a non-embeddable font as real text (Word's default
# turns it into a picture, which can't be extracted).
_WORD_EXPORT_OPTIONS = {
    "OpenAfterExport": False,
    "OptimizeFor": 1,  # wdExportOptimizeForOnScreen
    "Range": 0,  # wdExportAllDocument
    "Item": 0,  # wdExportDocumentContent (no comment markup)
    "IncludeDocProps": False,
    "KeepIRM": True,
    "CreateBookmarks": 0,  # wdExportCreateNoBookmarks
    "DocStructureTags": False,
    "BitmapMissingFonts": False,
    "UseISO19005_1": False,
}

# A layout taking longer than this is treated as hung.
WORD_TIMEOUT_S = 600

# Oldest converted PDFs beyond this many are deleted.
_MAX_CACHED_PDFS = 40

_LIBREOFFICE_TIMEOUT_S = 300

_key_locks: dict[str, threading.Lock] = {}
_key_locks_guard = threading.Lock()


class ConversionError(RuntimeError):
    """The document could not be converted."""


class ConverterUnavailableError(ConversionError):
    """Neither Microsoft Word nor LibreOffice is installed."""


def cache_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    path = os.path.join(base, "DocForensicsAnalyzer", "converted")
    os.makedirs(path, exist_ok=True)
    return path


def cached_pdf_path(path: str) -> str:
    """Where the converted PDF for ``path`` (in its current state) lives."""
    stat = os.stat(path)
    ident = f"{os.path.normcase(os.path.abspath(path))}|{stat.st_mtime_ns}|{stat.st_size}"
    digest = hashlib.sha1(ident.encode("utf-8")).hexdigest()[:20]
    stem = os.path.splitext(os.path.basename(path))[0][:40]
    return os.path.join(cache_dir(), f"{stem}-{digest}.pdf")


def convert_to_pdf(path: str) -> str:
    """Return the path of a PDF rendering of ``path``, converting it
    first unless an up-to-date conversion is already cached.

    Raises:
        ConverterUnavailableError: no conversion engine is installed.
        ConversionError: the engine failed to convert this file.
    """
    target = cached_pdf_path(path)
    if os.path.exists(target):
        return target

    with _key_locks_guard:
        lock = _key_locks.setdefault(target, threading.Lock())
    with lock:
        if os.path.exists(target):
            return target
        # The engine writes into a fresh folder under %TEMP% -- Word can
        # fail with "The directory name isn't valid" for folders under
        # AppData\Local that it didn't create itself. The result is then
        # moved next to `target` under a unique name and renamed, so a
        # concurrent conversion of the same file can never leave a
        # half-written PDF at `target`.
        tmp = f"{target[:-4]}.{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp.pdf"
        with tempfile.TemporaryDirectory(prefix="docforensics-") as work_dir:
            produced = os.path.join(work_dir, "converted.pdf")
            _convert(os.path.abspath(path), produced)
            try:
                shutil.move(produced, tmp)
                os.replace(tmp, target)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
    _prune_cache()
    return target


def available_engine() -> str | None:
    """"word", "libreoffice", or None."""
    if _word_available():
        return "word"
    if _soffice_path():
        return "libreoffice"
    return None


def _convert(source: str, output: str) -> None:
    if _word_available():
        _convert_with_word(source, output)
        return
    soffice = _soffice_path()
    if soffice:
        _convert_with_libreoffice(soffice, source, output)
        return
    raise ConverterUnavailableError(
        "Neither Microsoft Word nor LibreOffice is installed, so Word documents "
        "can't be laid out exactly."
    )


# --- Microsoft Word ---------------------------------------------------


def _word_available() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"Word.Application\CLSID"):
            pass
        import win32com.client  # noqa: F401
    except (ImportError, OSError):
        return False
    return True


def _convert_with_word(source: str, output: str) -> None:
    from core import word_pool

    pool = word_pool.active_pool()
    if pool is not None:
        # The app's warm background Word instances.
        pool.convert(source, output, timeout=WORD_TIMEOUT_S)
        return

    import pythoncom
    import pywintypes
    import win32com.client

    pythoncom.CoInitialize()
    word = None
    try:
        # DispatchEx: a private Word process, never the user's open
        # Word window or documents.
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0  # wdAlertsNone
        export_with_word(word, source, output)
    except pywintypes.com_error as exc:
        raise ConversionError(
            f"Microsoft Word could not convert '{os.path.basename(source)}': {_com_message(exc)}"
        ) from exc
    finally:
        if word is not None:
            try:
                word.Quit(False)
            except Exception:  # noqa: BLE001 -- already gone
                pass
        pythoncom.CoUninitialize()


def export_with_word(word, source: str, output: str) -> None:
    """Open ``source`` in the running Word ``word``, export it to the PDF
    ``output`` and close it again.

    Raises:
        ConversionError: Word rejected the document. A Word process that
            died mid-way raises its raw ``pywintypes.com_error`` instead,
            so the caller can tell the two apart (see core/word_pool.py).
    """
    import pywintypes

    doc = None
    try:
        doc = word.Documents.Open(
            source,
            ConfirmConversions=False,
            ReadOnly=True,
            AddToRecentFiles=False,
            # A wrong (empty) password makes a protected document fail
            # with an error instead of opening a password prompt.
            PasswordDocument="",
            PasswordTemplate="",
            Visible=False,
            NoEncodingDialog=True,
        )
        try:
            view = doc.ActiveWindow.View
            view.RevisionsFilter.Markup = _WD_REVISIONS_MARKUP_NONE
            view.RevisionsFilter.View = _WD_REVISIONS_VIEW_FINAL
        except pywintypes.com_error:
            logger.debug("Could not switch %s to the final (no markup) view", source)
        doc.ExportAsFixedFormat(output, _WD_EXPORT_FORMAT_PDF, **_WORD_EXPORT_OPTIONS)
    except pywintypes.com_error as exc:
        from core.word_pool import _is_dead_server

        if _is_dead_server(exc):
            raise
        raise ConversionError(
            f"Microsoft Word could not convert '{os.path.basename(source)}': {_com_message(exc)}"
        ) from exc
    finally:
        if doc is not None:
            try:
                doc.Close(False)  # wdDoNotSaveChanges
            except pywintypes.com_error:
                pass


def _com_message(exc) -> str:
    # com_error.excepinfo = (code, source, description, ...)
    excepinfo = exc.args[2] if len(exc.args) > 2 else None
    if excepinfo and len(excepinfo) > 2 and excepinfo[2]:
        return str(excepinfo[2]).strip()
    return str(exc)


# --- LibreOffice ------------------------------------------------------


def _soffice_path() -> str | None:
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            candidate = os.path.join(base, "LibreOffice", "program", "soffice.exe")
            if os.path.exists(candidate):
                return candidate
    return None


def _convert_with_libreoffice(soffice: str, source: str, output: str) -> None:
    with tempfile.TemporaryDirectory() as outdir:
        # A private profile, so a running LibreOffice instance can't
        # swallow the request.
        profile = "file:///" + os.path.join(outdir, "profile").replace("\\", "/")
        result = subprocess.run(
            [
                soffice,
                f"-env:UserInstallation={profile}",
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                outdir,
                source,
            ],
            capture_output=True,
            text=True,
            timeout=_LIBREOFFICE_TIMEOUT_S,
        )
        produced = os.path.join(outdir, os.path.splitext(os.path.basename(source))[0] + ".pdf")
        if result.returncode != 0 or not os.path.exists(produced):
            detail = (result.stderr or result.stdout).strip() or f"exit code {result.returncode}"
            raise ConversionError(
                f"LibreOffice could not convert '{os.path.basename(source)}': {detail}"
            )
        shutil.move(produced, output)


def _prune_cache() -> None:
    try:
        folder = cache_dir()
        pdfs = [
            os.path.join(folder, name)
            for name in os.listdir(folder)
            if name.endswith(".pdf") and not name.endswith(".tmp.pdf")
        ]
        pdfs.sort(key=os.path.getmtime, reverse=True)
        for stale in pdfs[_MAX_CACHED_PDFS:]:
            os.remove(stale)
    except OSError:
        pass  # a file in use by the viewer; try again next time
