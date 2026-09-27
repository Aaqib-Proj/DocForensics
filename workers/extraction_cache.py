"""Background pre-extraction of loaded documents.

As soon as a document is loaded, its text extraction starts in worker
PROCESSES, so by the time the user clicks Compare the slow part
(pdfplumber layout analysis) is usually already done. PDF pages are
independent, so each document is split into page ranges that extract
in parallel on every spare CPU core (both documents share the pool) and
are joined back in page order -- pdfplumber is pure Python, so threads
would just take turns on the GIL and also stall the GUI thread.

Results are cached per file, keyed by (path, mtime, size): loading the
same unchanged file again reuses its result, and an edited file on disk
is re-extracted.

Per the workers/ package contract: no Qt widgets here, and completion
is reported to the GUI thread only through signals.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from collections import OrderedDict
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

from PySide6.QtCore import QObject, Signal

from core.document_extractor import (
    document_kind,
    extract_document_tokens,
    extract_pdf_page_range,
    pdf_page_count,
)
from core.office_convert import cached_pdf_path
from core.pdf_extractor import build_comparison_tokens

logger = logging.getLogger(__name__)

# Every core but one (left for the GUI and Word), at least two.
_MAX_WORKERS = max(2, min((os.cpu_count() or 2) - 1, 12))

# Finished results kept around for re-use. Token lists for large
# documents are sizeable, so only the most recently requested files are
# kept; in-flight extractions are never evicted.
_MAX_CACHED_FILES = 6


def _warm_up() -> None:
    """Run once in each worker process at startup so the heavy imports
    (pdfplumber, PyMuPDF, ...) are paid before the first real load."""
    import core.document_extractor  # noqa: F401


def _cache_key(path: str) -> tuple[str, int, int]:
    stat = os.stat(path)
    return (os.path.normcase(os.path.abspath(path)), stat.st_mtime_ns, stat.st_size)


class ExtractionCache(QObject):
    """Starts and remembers background extractions.

    Signals:
        ready: Emitted with the file path when its extraction succeeds.
        failed: Emitted with (file path, error message) when it fails.
    """

    ready = Signal(str)
    failed = Signal(str, str)
    # Internal: (path, cache key, future), emitted from an executor
    # thread and delivered queued, so all cache bookkeeping stays on
    # the GUI thread.
    _finished = Signal(str, object, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._executor = self._new_executor()
        self._futures: OrderedDict[tuple[str, int, int], Future] = OrderedDict()
        self._finished.connect(self._on_done)

    @staticmethod
    def _new_executor() -> ProcessPoolExecutor:
        executor = ProcessPoolExecutor(max_workers=_MAX_WORKERS)
        for _ in range(_MAX_WORKERS):
            executor.submit(_warm_up)
        return executor

    def extraction_for(self, path: str) -> Future:
        """Return the Future holding ``path``'s comparison tokens,
        starting its extraction first if it isn't cached or running.

        Must be called from the GUI thread.
        """
        try:
            key = _cache_key(path)
        except OSError as exc:
            future: Future = Future()
            future.set_exception(exc)
            return future

        future = self._futures.get(key)
        if future is not None:
            self._futures.move_to_end(key)
            return future

        try:
            future = self._submit(path)
        except BrokenProcessPool:
            logger.warning("Extraction process pool broke; restarting it")
            self._executor = self._new_executor()
            future = self._submit(path)

        self._futures[key] = future
        self._evict_finished()
        future.add_done_callback(lambda f, p=path, k=key: self._finished.emit(p, k, f))
        return future

    def _submit(self, path: str) -> Future:
        """Start extracting ``path``: page ranges in parallel for a PDF
        (or a Word document already laid out as one), else one job."""
        pdf = self._layout_pdf(path)
        if pdf is None:
            return self._executor.submit(extract_document_tokens, path)
        pages = pdf_page_count(pdf)
        per_chunk = max(1, math.ceil(pages / _MAX_WORKERS))
        name = os.path.basename(path)
        parts = [
            self._executor.submit(
                extract_pdf_page_range, pdf, first, min(first + per_chunk - 1, pages), name
            )
            for first in range(1, pages + 1, per_chunk)
        ]
        return _joined(parts)

    @staticmethod
    def _layout_pdf(path: str) -> str | None:
        kind = document_kind(path)
        if kind == "pdf":
            return path
        if kind == "docx":
            converted = cached_pdf_path(path)
            if os.path.exists(converted):
                return converted
        return None

    def _on_done(self, path: str, key: tuple, future: Future) -> None:
        if future.cancelled():
            return
        exc = future.exception()
        if exc is None:
            self.ready.emit(path)
            return
        if isinstance(exc, BrokenProcessPool) and self._futures.get(key) is future:
            # Not a property of the file -- let the next request retry.
            del self._futures[key]
        self.failed.emit(path, str(exc) or type(exc).__name__)

    def _evict_finished(self) -> None:
        for key in list(self._futures):
            if len(self._futures) <= _MAX_CACHED_FILES:
                break
            if self._futures[key].done():
                del self._futures[key]

    def shutdown(self) -> None:
        """Stop the worker processes without waiting for running
        extractions (called when the app closes)."""
        # ProcessPoolExecutor has no public way to stop a task that is
        # already running before Python 3.14, and interpreter exit
        # would otherwise block until it finishes. shutdown() clears
        # the process table, so take it first.
        processes = list((getattr(self._executor, "_processes", None) or {}).values())
        self._executor.shutdown(wait=False, cancel_futures=True)
        for process in processes:
            if process.is_alive():
                process.terminate()


def _joined(parts: list[Future]) -> Future:
    """A Future of the comparison tokens of all ``parts`` (word lists in
    page order); fails with the first part's error."""
    result: Future = Future()
    result.set_running_or_notify_cancel()
    lock = threading.Lock()
    remaining = [len(parts)]

    def on_part_done(_part: Future) -> None:
        with lock:
            remaining[0] -= 1
            if remaining[0] or result.done():
                failed = next(
                    (p for p in parts if p.done() and not p.cancelled() and p.exception() is not None),
                    None,
                )
                if failed is not None and not result.done():
                    result.set_exception(failed.exception())
                return
        try:
            words = [word for part in parts for word in part.result()]
            result.set_result(build_comparison_tokens(words))
        except BaseException as exc:  # noqa: BLE001 -- handed to the caller
            if not result.done():
                result.set_exception(exc)

    if not parts:
        result.set_result([])
    for part in parts:
        part.add_done_callback(on_part_done)
    return result
