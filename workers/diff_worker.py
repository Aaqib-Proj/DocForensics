"""QRunnable wrapper around core/document_extractor.py + core/diff_engine.py.

Extraction normally already started in the background when each
document was loaded (workers/extraction_cache.py); this worker then
only waits for whatever part of it is still running and diffs. Without
a pre-started extraction it extracts inline, as before.

Per the workers/ package contract: no Qt WIDGETS here, only Qt's
threading primitives, and results only ever cross back to the GUI
thread via signals -- never by touching a widget directly from the
worker thread.

Pure text-vs-text comparison: no OCR, no pixel/visual diff. Both sides
must be one of PDF, DOCX, PPTX, or XLSX with real extractable text
(core.document_extractor.extract_document_words raises a clear
ValueError otherwise, e.g. for a scanned/image-only PDF).
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import FIRST_EXCEPTION, Future, wait
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QRunnable, Signal

from core.coverage_check import coverage_is_serious, coverage_warnings
from core.diff_engine import DiffItem, diff_documents
from core.review_store import comparison_key
from core.document_extractor import extract_document_tokens
from core.token_map import TokenMap

logger = logging.getLogger(__name__)

# How often differences found so far are handed to the UI. Short enough
# that the list visibly fills, long enough that a document with
# thousands of them does not drown the event loop in signals.
_FOUND_INTERVAL_S = 0.12


@dataclass
class ComparisonResult:
    items: list[DiffItem]
    # Word-level correspondence between the two documents, for
    # click-to-navigate from one viewer to the other.
    token_map: TokenMap
    # What the comparison could NOT read (scanned pages, a near-empty
    # side, undecodable fonts) -- see core/coverage_check.py.
    warnings: list[str] = field(default_factory=list)
    # One side yielded a fraction of the other's words: needs acknowledging.
    serious_warning: bool = False
    # Identity of this exact pair of files, for keeping the review.
    review_key: str = ""


class DiffWorkerSignals(QObject):
    """Signals for DiffWorker (QRunnable can't emit signals itself)."""

    finished = Signal(object)  # ComparisonResult
    # Differences as they are found, in batches, so the list fills
    # while the comparison runs. Provisional: `finished` carries the
    # authoritative set (see core.diff_engine.diff_documents).
    found = Signal(list)  # list[DiffItem]
    failed = Signal(str)  # human-readable error message, on failure
    progress = Signal(str)  # human-readable stage label


class DiffWorker(QRunnable):
    """Runs extraction + diffing for a pair of documents on a background
    thread.

    Args:
        approved_path: Path to the reference/approved document (PDF,
            DOCX, PPTX, or XLSX).
        suspect_path: Path to the document being checked. Does NOT
            need to be the same format as ``approved_path`` -- e.g.
            comparing a DOCX draft against its final PDF export is a
            legitimate, common use case, and both sides normalize to
            the same Word shape before diffing.
        approved_extraction / suspect_extraction: Optional Futures
            already producing each side's comparison tokens (from
            ExtractionCache). A side without one is extracted inline.
    """

    def __init__(
        self,
        approved_path: str,
        suspect_path: str,
        approved_extraction: Future | None = None,
        suspect_extraction: Future | None = None,
        crop_data: dict[str, dict[int, tuple[float, float]]] | None = None,
    ) -> None:
        super().__init__()
        self.approved_path = approved_path
        self.suspect_path = suspect_path
        self.approved_extraction = approved_extraction
        self.suspect_extraction = suspect_extraction
        self.crop_data = crop_data or {}
        self.signals = DiffWorkerSignals()
        self._cancelled = False
        # Differences found but not yet handed to the UI.
        self._pending: list = []
        self._last_emit = 0.0

    def cancel(self) -> None:
        self._cancelled = True

    def _progress(self, message: str) -> None:
        self.signals.progress.emit(message)

    def run(self) -> None:
        try:
            self._await_extractions()
            if self._cancelled:
                return
            approved_tokens = self._tokens(self.approved_path, self.approved_extraction)
            if self._cancelled:
                return
            suspect_tokens = self._tokens(self.suspect_path, self.suspect_extraction)
            if self._cancelled:
                return

            # Apply header/footer crops if set
            if self.crop_data:
                self._progress("Applying header/footer crops...")
                before_a = len(approved_tokens)
                before_s = len(suspect_tokens)
                approved_tokens = self._apply_crop_to_tokens(approved_tokens, self.approved_path)
                suspect_tokens = self._apply_crop_to_tokens(suspect_tokens, self.suspect_path)
                logger.info(
                    "Crop filter: approved %d->%d tokens, suspect %d->%d tokens",
                    before_a, len(approved_tokens), before_s, len(suspect_tokens),
                )

            self._progress("Comparing text...")
            matches: list = []
            items = diff_documents(
                approved_tokens, suspect_tokens, matches, on_found=self._on_found
            )
            self._progress("Checking what could be compared...")
            warnings = coverage_warnings(
                self.approved_path, self.suspect_path, approved_tokens, suspect_tokens
            )
            try:
                review_key = comparison_key(self.approved_path, self.suspect_path)
            except OSError:
                review_key = ""
            result = ComparisonResult(
                items,
                TokenMap(approved_tokens, suspect_tokens, matches),
                warnings=warnings,
                serious_warning=coverage_is_serious(approved_tokens, suspect_tokens),
                review_key=review_key,
            )
        except Exception as exc:  # noqa: BLE001 -- surface any failure to the UI
            logger.exception("DiffWorker failed")
            self.signals.failed.emit(str(exc) or type(exc).__name__)
            return

        if self._cancelled:
            return
        if self._pending:
            self.signals.found.emit(self._pending)
            self._pending = []
        self.signals.finished.emit(result)

    def _on_found(self, found: list) -> None:
        """Hand a batch of differences to the UI as they are found.

        Batched on a short timer rather than emitted one at a time: a
        signal per difference would cost more in queued events than the
        comparison itself takes.
        """
        if self._cancelled:
            return
        self._pending.extend(found)
        now = time.monotonic()
        if now - self._last_emit >= _FOUND_INTERVAL_S:
            self._last_emit = now
            batch, self._pending = self._pending, []
            self.signals.found.emit(batch)

    def _await_extractions(self) -> None:
        """Wait for any still-running background extraction, raising a
        failure as soon as it happens rather than after the other side
        finishes."""
        pending = {
            future: os.path.basename(path)
            for path, future in (
                (self.approved_path, self.approved_extraction),
                (self.suspect_path, self.suspect_extraction),
            )
            if future is not None and not future.done()
        }
        if not pending:
            return
        self._progress(f"Finishing extraction of {', '.join(pending.values())}...")
        while pending and not self._cancelled:
            done, _ = wait(list(pending), timeout=0.2, return_when=FIRST_EXCEPTION)
            for future in done:
                exc = future.exception()
                if exc is not None:
                    raise exc
                del pending[future]

    def _apply_crop_to_tokens(self, tokens: list, path: str) -> list:
        """Filter out tokens whose source words fall inside cropped header/footer regions.

        A token is excluded if all of its source words (or its bounding box center)
        fall inside the cropped header or footer region for that page.
        """
        page_crops = self.crop_data.get(path, {})
        if not page_crops:
            return tokens
        filtered = []
        for t in tokens:
            page = getattr(t, "page", None)
            if page is None:
                filtered.append(t)
                continue
            crop = page_crops.get(page)
            if crop is None:
                filtered.append(t)
                continue
            header_bottom, footer_top = crop
            source_words = getattr(t, "source_words", None)
            if source_words:
                all_cropped = True
                for w in source_words:
                    y_center = (w.top + w.bottom) / 2.0
                    if header_bottom <= y_center <= footer_top:
                        all_cropped = False
                        break
                if all_cropped:
                    continue
            elif hasattr(t, "bbox") and t.bbox:
                x0, top, x1, bottom = t.bbox
                y_center = (top + bottom) / 2.0
                if y_center < header_bottom or y_center > footer_top:
                    continue
            filtered.append(t)
        return filtered

    def _tokens(self, path: str, extraction: Future | None) -> list:
        if extraction is None:
            self._progress(f"Extracting {os.path.basename(path)}...")
            return extract_document_tokens(path)
        return extraction.result()
