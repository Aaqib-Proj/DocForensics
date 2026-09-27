"""QRunnable wrapper around core/office_convert.py.

Converting a Word document through Microsoft Word takes a few seconds,
so it runs off the GUI thread. Results cross back only via signals.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QRunnable, Signal

from core.office_convert import ConverterUnavailableError, convert_to_pdf

logger = logging.getLogger(__name__)


class ConvertWorkerSignals(QObject):
    converted = Signal(str, str)  # (source path, PDF path)
    unavailable = Signal(str)  # source path: no layout engine installed
    failed = Signal(str, str)  # (source path, error message)


class ConvertWorker(QRunnable):
    def __init__(self, source_path: str) -> None:
        super().__init__()
        self.source_path = source_path
        self.signals = ConvertWorkerSignals()

    def run(self) -> None:
        try:
            pdf_path = convert_to_pdf(self.source_path)
        except ConverterUnavailableError:
            self.signals.unavailable.emit(self.source_path)
        except Exception as exc:  # noqa: BLE001 -- surface any failure to the UI
            logger.exception("Converting %s failed", self.source_path)
            self.signals.failed.emit(self.source_path, str(exc) or type(exc).__name__)
        else:
            self.signals.converted.emit(self.source_path, pdf_path)
