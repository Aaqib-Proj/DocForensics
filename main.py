"""DocForensics Analyzer -- MVP-22.

MVP-4 wired core/pdf_extractor.py + core/diff_engine.py into the GUI --
a "Compare" button that runs the diff on a background thread via
workers/diff_worker.py, a results table (ui/results_panel.py), and
click-to-navigate: clicking a result highlights and scrolls to that
word on whichever panel(s) it appears in. None of that GUI wiring
changes here.

MVP-5 fixed core/pdf_extractor.py's reading order on multi-column
pages and added a 4th, low-priority "typo" severity tier in
core/diff_engine.py so character-level source-PDF noise doesn't drown
out genuinely dangerous changes like a dosage/strength digit swap.

MVP-6: ui/document_panel.py now renders every page of a PDF into one
continuous, scrollable, zoomable view instead of just page 1, with its
own Prev/Next page controls -- see that module's docstring.
Click-to-navigate now works for a diff on any page. ui/results_panel.py
also got a visual pass: high-contrast severity "badges", alternating
row backgrounds, wrapped/expandable text for long passages, and a
proper header style, all independent of the OS/app color theme.

MVP-7: trackpad zoom. A two-finger pinch on the document
view (QPinchGesture from Windows precision touchpads / macOS trackpads,
plus the Ctrl+wheel pinch many other touchpads emit) zooms BOTH panels
through the same shared factor as the toolbar +/- buttons, so pinching
works exactly like clicking the buttons -- without the clicks.

MVP-13 (current): OCR and pixel-level Visual Diff were REMOVED --
this build is pure digital-text-vs-digital-text comparison only (a
scanned/image-only PDF now fails extraction with a clear error rather
than being routed through OCR). Two things changed as a result:
  1. `core.diff_engine.Location` now tracks EVERY page a
     discrepancy's text touches (`boxes_by_page`), not just the first
     one -- fixing a real bug where a discrepancy anchored to, say,
     page 3 of a multi-page document would fail to highlight AT ALL
     against a shorter "concise" counterpart document, because the
     old Location silently dropped every word not on its first page.
  2. `core.document_extractor` adds DOCX, PPTX, and XLSX support
     alongside PDF, normalizing every format to the same Word shape
     the diff engine already used -- so a DOCX draft can be compared
     against its final PDF export, a PPTX deck against a revised
     PPTX, etc., all through the exact same diff/highlight pipeline.

MVP-14 (current): fixed a real Word-draft-vs-PDF comparison that came
back with scrambled/corrupted text and wrong-looking coordinates --
see README.md section 12 for the full writeup. Three bugs, all in
extraction/rendering, not the diff logic itself:
  1. `core.pdf_extractor` now correctly reconstructs rotated (sideways)
     text -- e.g. a packaging spine caption -- instead of scattering it
     into scrambled single-character noise.
  2. `core.pdf_extractor`'s RTL bracket-mirroring (")" <-> "(") was
     firing on every word, not just genuine RTL text, corrupting
     ordinary punctuation like "(2.1)" on every PDF; now gated to
     actual RTL words only.
  3. `ui.document_panel`'s DOCX/PPTX/XLSX synthesized preview now scales
     each word to fit its own box exactly, so adjacent words can no
     longer visually run together into unreadable text.

MVP-15: a pure insertion/deletion's "missing" marker is now sized to
about ONE CHARACTER at the real insertion point in that document's OWN
layout (`core.diff_engine.DiffItem.approved_caret`/`suspect_caret`),
instead of mirroring the other document's full (and often much larger)
bbox onto a layout it has nothing to do with. See README.md section 13.

MVP-16 (current): fixed RTL (Arabic) word/character order corruption
caused by a Latin/numeric run embedded in RTL text -- a batch code, PIN
code, or acronym glued to Arabic words with no space -- see README.md
section 14 for the full writeup:
  1. `core.pdf_extractor._sort_line_words` no longer reverses an
     embedded LTR/digit run's own fragments just because the
     surrounding line is RTL (a real case: "0-31" split by pdfplumber
     into "0"+"-31" in one PDF came out as "-31 0").
  2. `core.pdf_extractor._fix_rtl_word_chars` now applies a simplified
     Unicode Bidi Algorithm instead of a blind whole-string reversal,
     so an embedded Latin/digit run inside one merged RTL word survives
     intact (a real case: "MIDC" was corrupted to "CDIM").
  3. `core.pdf_extractor.build_comparison_tokens` gained a second stitch
     rule, `_is_code_fragment_split`, re-merging a numeric/code value
     pdfplumber split into multiple word-fragments (digits/punctuation
     only, touching with near-zero gap) -- fixing the residual case
     where fixes 1-2 alone left correct content/order still mismatched
     at the token-count level.

MVP-17: fixed a real Draft-vs-Approved comparison that came back with
77 mostly-nonsensical "critical" rows -- a side-effect bullet paired
against an unrelated sentence about drug composition, repeated dozens
of times. Root cause: a genuine, unambiguous 10pt column gutter on a
small-font leaflet was rejected by the previous flat 12pt minimum
(tuned for a different document's larger font), collapsing two real
side-by-side columns into row-major order and interleaving them
word-by-word. `core.pdf_extractor`'s column-gutter minimum now scales
with the PAGE'S OWN measured word spacing instead of one constant --
see README.md section 15. Verified: the suspect-side critical count
dropped from 77 (nearly all cascaded misalignment) to 25, every one
now a genuine content difference.

MVP-18: fixed highlight coordinates that were visibly offset from the
real text on the SAME file pair's Suspect panel -- a professionally-
produced packaging artwork PDF's MediaBox had a non-(0,0) origin
`(-9, -9)` (a bleed-box convention), so pdfplumber's raw word
coordinates (reported in that native, offset space) no longer lined up
with PyMuPDF's rendering (which always normalizes the rendered page to
start at `(0, 0)`) -- every highlight landed exactly 9pt away from the
actual word. `core.pdf_extractor.extract_words` now subtracts each
page's own origin from every word's coordinates at extraction time.
See README.md section 16. A PDF with a normal `(0,0)` origin (the
common case) is completely unaffected.

MVP-19: MVP-8's out-of-order section matching -- match sections by
header regardless of stream position, so relocated content demotes to
"reordered_section" instead of a critical cascade -- only ever worked
for FDA/EMA-style NUMBERED headers. A real Draft-vs-Approved comparison
(a Discover Cardmember Agreement, whose sections "CARDMEMBER
AGREEMENT" / "INTEREST CHARGES" / "ARBITRATION" have no leading
numbers at all) fell back to a linear stream diff, and the suspect
PDF's print sheet also combined two unrelated pieces of content in a
different relative order than approved -- cascading into dozens of
nonsense pairings from the very first word. `core.section_diff.build_
section_map` now also recognizes a short, ALL-CAPS, PURE-LETTER-TOKENS
line as a header even with no number, and `core.diff_engine._match_
sections` title-anchors two such unnumbered headers the same way it
already does for numbered ones. See README.md section 17 -- including
a known remaining limitation (one section's own internal column layout
still needed a further fix) that MVP-20 below picks up directly.

MVP-20 (current): fixed that exact remaining limitation -- a page can
legitimately stack a single-column clause, a 2-column glossary, AND a
3-column arbitration clause, and MVP-17's page-wide adaptive gutter
still measured gutters once for the WHOLE page, so a single-column
section's lines "crossed" a real, completely clean 3-column gutter
elsewhere on the same page, hiding it. `core.pdf_extractor._split_
into_zones` now runs gutter detection SEPARATELY per zone (split at
likely section-header lines). A second, related bug fixed in the same
pass: a borderless "label: value" table (no ruling lines, so the table
detector correctly skips it) was being read column-major like flowing
body text, joining every label together and then every explanation
together instead of each row's own pair -- `_bands_are_column_flow`
now requires roughly-equal column widths before reading a zone
column-major. A third fix (for two running heads coincidentally
landing on the same page-row of an oversized combined-sheet layout)
was tried, measured with a direct A/B comparison on the real document,
found to cause MORE damage than it fixed, and deliberately reverted --
see README.md section 18 for that account in full. Verified: the
reported ARBITRATION section, previously an unreadable 3-column
interleave, now extracts as a clean, complete, correctly-ordered
clause; the full 51-assertion test suite passes unchanged.

MVP-21 (current): MVP-20's own zone-splitting shipped two regressions,
both severe enough to be reported as "everything is gone, no more
correct comparison even in arabic too" -- fixed the same day, verified
against the actual reported files. (1) `is_likely_header_line`'s
numbered-header check accepted ANY line starting with a bare 1-3 digit
number with no further validation, so ordinary numbered list items
("1. Within 30 days...") and bare drug-dose numbers ("150 mg") each
forced a spurious new zone boundary -- fragmenting real documents into
1-2 row zones, too little data for `_adaptive_min_gutter` to trust,
which then misread ordinary within-line word gaps as column gutters
and scrambled single lines into fake columns. Now requires the same
same-line caps-title evidence `build_section_map`'s own numbered
branch already required. (2) `_bands_are_column_flow`'s width-balance
ratio, built for a 2-band borderless label/value table, wrongly
collapsed a genuine 4-column proof-sheet zone to row-major just
because one column (a bullet-marker strip) was narrower than the
others -- interleaving all four columns' text word-by-word. A
borderless label/value table inherently has exactly two columns, so
the check now only applies at 2 bands; 3+ independently-detected bands
are always column-flow. See README.md section 19 for the full
before/after evidence on both real reported files. Full 54-assertion
suite passes unchanged.

MVP-23 (current): fixed 2-column borderless composition tables (e.g.
herbal composition: "فاساكا (أدهاتودا فاسيكا) 150 مجم") misclassified as
flowing multi-column text due to band width balance ratio (0.41 > 0.40),
which caused all herb names to be extracted first and all dosages afterwards.
Increased `_BAND_WIDTH_BALANCE_RATIO` to 0.55 and updated `_sort_line_words`
so direction-neutral punctuation (like brackets) inherits RTL direction
on RTL lines, preventing brackets from sandwiching inside LTR numeric runs.

Run with:
    python main.py
"""

import logging
import multiprocessing
import os
import sys

from PySide6.QtCore import QSettings, Qt, QThreadPool, QTimer
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QToolBar,
    QWidget,
)

from core import word_pool
from core.diff_engine import DiffItem
from core.office_convert import available_engine
from core.token_map import APPROVED, CONTEXT, SUSPECT, TokenMap
from ui import theme
from ui.document_panel import DocumentPanel
from ui.results_panel import ACCEPTED, REJECTED, ResultsPanel
from ui.crop_dialog import CropDialog
from workers.diff_worker import ComparisonResult, DiffWorker
from workers.extraction_cache import ExtractionCache

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

APP_NAME = "DocForensics Analyzer"
APP_VERSION = "MVP-30"

_SHORTCUTS = [
    ("Open Original / Suspect", "Ctrl+O / Ctrl+Shift+O"),
    ("Compare", "F5"),
    ("Next / previous difference", "F8 / Shift+F8"),
    ("Accept / Reject / Reset selected", "A / R / U (in the list)"),
    ("Zoom in / out (last used viewer)", "Ctrl++ / Ctrl+\u2212"),
    ("Fit width (last used viewer)", "Ctrl+0"),
    ("Zoom with the trackpad or wheel", "Pinch / Ctrl+Wheel"),
    ("Lock views", "Ctrl+L"),
    ("Show highlights", "Ctrl+H"),
    ("Find in list", "Ctrl+F"),
]


class MainWindow(QMainWindow):
    """DocForensics Analyzer shell: two independently zoomable
    DocumentPanels (each accepting PDF/DOCX/PPTX/XLSX) + Compare +
    results.

    Viewer coordination:
      * Each viewer zooms and scrolls on its own. "Lock views" keeps
        them in step (same zoom; scrolling one moves the other to the
        same text, or the same page position before a comparison).
      * Clicking a difference -- in the results table OR on a viewer's
        highlight -- navigates both viewers to it, selects its row, and
        gives the other viewer the zoom of the one last used.
      * Clicking ordinary text in one viewer takes the other viewer to
        the same text, using the comparison's word alignment.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1440, 960)
        self.settings = QSettings("DocForensics", "Analyzer")
        theme.apply(self.settings.value("theme", theme.SYSTEM, str))

        self.threadpool = QThreadPool()
        self._active_worker: DiffWorker | None = None  # keep a ref so it isn't GC'd
        self._diff_results: list[DiffItem] = []  # stored for overlay drawing
        self._crop_data: dict[str, dict[int, tuple[float, float]]] = {}  # crop regions
        self._token_map: TokenMap | None = None  # word alignment of the last comparison
        self._syncing_views = False  # guard against lock-sync feedback loops

        # Two hidden Word instances start now and stay open, so a Word
        # document is laid out without waiting for Word to start, and
        # the Original and Suspect are laid out in parallel.
        if available_engine() == "word":
            word_pool.start(size=2)

        # Text extraction starts in the background as soon as each
        # document loads, so Compare usually only has to diff.
        self.extraction_cache = ExtractionCache(self)
        self.extraction_cache.ready.connect(self._on_extraction_ready)
        self.extraction_cache.failed.connect(self._on_extraction_failed)

        # --- Blink timer for discrepancy overlays ---
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(600)  # ms
        self._blink_visible = True
        self._blink_timer.timeout.connect(self._on_blink_tick)

        self.original_panel = DocumentPanel("Original")
        self.suspect_panel = DocumentPanel("Suspect")
        DocumentPanel.last_dir = self.settings.value("last_dir", "", str)

        # The viewer the user used last: its zoom is the one the other
        # viewer adopts when navigating to a difference.
        self._active_panel = self.original_panel
        for panel in (self.original_panel, self.suspect_panel):
            panel.view_moved.connect(lambda p=panel: self._on_view_moved(p))
            panel.zoom_changed.connect(lambda factor, p=panel: self._on_panel_zoomed(p, factor))
            panel.interacted.connect(lambda p=panel: self._set_active_panel(p))
            panel.point_clicked.connect(
                lambda page, x, y, p=panel: self._on_viewer_clicked(p, page, x, y)
            )
            # Enable Compare only once both documents are actually loaded;
            # a Word document is laid out in the background first.
            panel.loaded.connect(self._on_document_loaded)
            panel.loading.connect(self._on_document_loading)

        splitter = QSplitter()
        splitter.setObjectName("documents")
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self.original_panel)
        splitter.addWidget(self.suspect_panel)
        splitter.setSizes([700, 700])
        self.setCentralWidget(splitter)

        self.results_panel = ResultsPanel()
        self.results_panel.item_activated.connect(self.on_result_activated)
        self.results_panel.view_changed.connect(self._redraw_overlays)
        self.results_panel.view_changed.connect(self._update_review_actions)
        self.results_panel.table.itemSelectionChanged.connect(self._update_review_actions)
        self.results_dock = QDockWidget("Deviations", self)
        self.results_dock.setObjectName("deviations")
        self.results_dock.setWidget(self.results_panel)
        # Prevent detaching / floating so viewer deviations are never hidden behind deviation list
        self.results_dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable)
        self.results_dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        self.results_dock.setFloating(False)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.results_dock)
        self.resizeDocks([self.results_dock], [340], Qt.Orientation.Vertical)

        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._build_status_bar()

        reviewer = self.settings.value("reviewer", "", str) or self.results_panel.reviewer
        self.reviewer_edit.setText(reviewer)
        self.results_panel.reviewer = reviewer
        self.reviewer_edit.textChanged.connect(self._on_reviewer_changed)
        # Only a name the user typed is remembered (not one set by code).
        self.reviewer_edit.textEdited.connect(lambda name: self.settings.setValue("reviewer", name.strip()))

        self._restore_layout()
        self._update_compare_enabled()
        self._update_review_actions()

    # ------------------------------------------------------------------
    # Window chrome: actions, menus, toolbar, status bar, settings
    # ------------------------------------------------------------------

    def _action(self, text, slot=None, shortcut=None, checkable=False, tip=None) -> QAction:
        action = QAction(text, self)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        if checkable:
            action.setCheckable(True)
        if tip:
            action.setToolTip(tip)
            action.setStatusTip(tip)
        if slot is not None:
            (action.toggled if checkable else action.triggered).connect(slot)
        return action

    def _build_actions(self) -> None:
        self.open_original_action = self._action(
            "Open &Original\u2026", self.original_panel.on_load_clicked, "Ctrl+O"
        )
        self.open_suspect_action = self._action(
            "Open &Suspect\u2026", self.suspect_panel.on_load_clicked, "Ctrl+Shift+O"
        )
        self.compare_action = self._action("&Compare", self.on_compare_clicked, "F5")
        self.crop_action = self._action("Crop &Header/Footer...", self.on_crop_clicked, "Ctrl+H")
        self.exit_action = self._action("E&xit", self.close, "Alt+F4")

        self.next_action = self._action(
            "&Next Difference", lambda: self._step_difference(1), "F8", tip="Go to the next difference (F8)"
        )
        self.prev_action = self._action(
            "&Previous Difference", lambda: self._step_difference(-1), "Shift+F8",
            tip="Go to the previous difference (Shift+F8)",
        )
        self.accept_action = self._action(
            "&Accept Selected", lambda: self.results_panel.decide_selected(ACCEPTED)
        )
        self.reject_action = self._action(
            "&Reject Selected", lambda: self.results_panel.decide_selected(REJECTED)
        )
        self.reset_action = self._action("Rese&t Selected", lambda: self.results_panel.decide_selected(None))
        self.find_action = self._action("&Find in List", self._focus_search, "Ctrl+F")

        self.lock_checkbox = self._action(
            "&Lock Views", self._on_lock_toggled, "Ctrl+L", checkable=True,
            tip="Keep both viewers in step: zooming or scrolling one moves the other to the same text.",
        )
        self.show_highlights_checkbox = self._action(
            "Show &Highlights", None, "Ctrl+H", checkable=True,
            tip="Show the differences on both documents.",
        )
        self.show_highlights_checkbox.setChecked(True)
        self.show_highlights_checkbox.toggled.connect(self._on_show_highlights_toggled)
        self.show_highlights_checkbox.setEnabled(False)
        self.blink_checkbox = self._action(
            "&Blink Highlights", self._on_blink_toggled, checkable=True,
            tip="Make the difference highlights blink.",
        )
        self.blink_checkbox.setEnabled(False)  # enabled after a comparison
        self.toggle_deviations_action = self.results_dock.toggleViewAction()
        self.toggle_deviations_action.setText("&Deviation List")
        self.toggle_deviations_action.setShortcut(QKeySequence("Ctrl+D"))
        self.toggle_deviations_action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.toggle_deviations_action.setToolTip("Show or hide the deviation list panel.")
        self.toggle_deviations_action.setStatusTip("Show or hide the deviation list panel.")

        self.comparator_action = self._action(
            "Co&mparator", checkable=True,
            tip="Show the selected difference's Original and Suspect text side by side, "
                "with the exact words that differ marked.",
        )
        self.comparator_action.setChecked(self.settings.value("comparator", True, bool))
        self.results_panel.set_comparator_visible(self.comparator_action.isChecked())
        self.comparator_action.toggled.connect(self._on_comparator_toggled)

        self.zoom_in_action = self._action("Zoom &In", lambda: self._active_panel.zoom_by(1.25), "Ctrl+=")
        self.zoom_in_action.setShortcuts([QKeySequence("Ctrl+="), QKeySequence("Ctrl++")])
        self.zoom_out_action = self._action("Zoom &Out", lambda: self._active_panel.zoom_by(1 / 1.25), "Ctrl+-")
        self.fit_action = self._action("&Fit Width", lambda: self._active_panel._on_fit_clicked(), "Ctrl+0")

        self.theme_group = QActionGroup(self)
        self.theme_actions = {}
        for mode, label in ((theme.SYSTEM, "&System"), (theme.LIGHT, "&Light"), (theme.DARK, "&Dark")):
            action = self._action(label, checkable=True)
            action.triggered.connect(lambda _=False, m=mode: self._set_theme(m))
            self.theme_group.addAction(action)
            self.theme_actions[mode] = action
        self.theme_actions[theme.manager().mode].setChecked(True)

        self.shortcuts_action = self._action("&Keyboard Shortcuts", self._show_shortcuts, "F1")
        self.about_action = self._action(f"&About {APP_NAME}", self._show_about)

    def _build_menus(self) -> None:
        bar = self.menuBar()
        file_menu = bar.addMenu("&File")
        file_menu.addActions([self.open_original_action, self.open_suspect_action])
        file_menu.addSeparator()
        file_menu.addAction(self.compare_action)
        file_menu.addAction(self.crop_action)
        file_menu.addSeparator()
        file_menu.addAction(self.exit_action)

        view_menu = bar.addMenu("&View")
        view_menu.addActions([self.zoom_in_action, self.zoom_out_action, self.fit_action])
        view_menu.addSeparator()
        view_menu.addActions([self.lock_checkbox, self.show_highlights_checkbox, self.blink_checkbox])
        view_menu.addSeparator()
        view_menu.addAction(self.toggle_deviations_action)
        view_menu.addAction(self.comparator_action)
        theme_menu = view_menu.addMenu("&Theme")
        theme_menu.addActions(list(self.theme_actions.values()))
        view_menu.addSeparator()
        view_menu.addAction(self._action("&Reset Layout", self._reset_layout))

        review_menu = bar.addMenu("&Review")
        review_menu.addActions([self.next_action, self.prev_action])
        review_menu.addSeparator()
        review_menu.addActions([self.accept_action, self.reject_action, self.reset_action])
        review_menu.addSeparator()
        review_menu.addAction(self.find_action)

        help_menu = bar.addMenu("&Help")
        help_menu.addActions([self.shortcuts_action, self.about_action])

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main", self)
        toolbar.setObjectName("main_toolbar")
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)

        self.compare_button = QPushButton("Compare")
        self.compare_button.setObjectName("primary")
        self.compare_button.clicked.connect(self.on_compare_clicked)
        toolbar.addWidget(self.compare_button)

        self.crop_button = QPushButton("Crop Header/Footer")
        self.crop_button.setEnabled(False)
        self.crop_button.setToolTip("Define header/footer regions to exclude from comparison.")
        self.crop_button.clicked.connect(self.on_crop_clicked)
        toolbar.addWidget(self.crop_button)

        toolbar.addSeparator()
        toolbar.addAction(self.prev_action)
        toolbar.addAction(self.next_action)
        for action in (self.prev_action, self.next_action):
            toolbar.widgetForAction(action).setProperty("flat", True)
        self.prev_action.setText("\u2039 Previous")
        self.next_action.setText("Next \u203a")
        toolbar.addSeparator()
        for action in (self.lock_checkbox, self.show_highlights_checkbox, self.blink_checkbox):
            toolbar.addAction(action)
            toolbar.widgetForAction(action).setProperty("flat", True)
        self.lock_checkbox.setIconText("Lock views")
        self.show_highlights_checkbox.setIconText("Highlights")
        self.blink_checkbox.setIconText("Blink")

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        reviewer_label = QLabel("Reviewer")
        reviewer_label.setObjectName("muted")
        toolbar.addWidget(reviewer_label)
        self.reviewer_edit = QLineEdit()
        self.reviewer_edit.setFixedWidth(160)
        self.reviewer_edit.setPlaceholderText("Your name")
        self.reviewer_edit.setToolTip("Name recorded with every Accept/Reject decision.")
        toolbar.addWidget(self.reviewer_edit)

    def _build_status_bar(self) -> None:
        bar = self.statusBar()
        bar.setSizeGripEnabled(False)
        self.status_label = QLabel("Open an Original and a Suspect document to compare.")
        bar.addWidget(self.status_label, stretch=1)
        self.busy_bar = QProgressBar()
        self.busy_bar.setRange(0, 0)  # indeterminate
        self.busy_bar.setFixedWidth(120)
        self.busy_bar.setTextVisible(False)
        self.busy_bar.hide()
        bar.addPermanentWidget(self.busy_bar)

    def _restore_layout(self) -> None:
        try:
            geometry = self.settings.value("geometry")
            if geometry is not None:
                self.restoreGeometry(geometry)
        except Exception:
            pass

        try:
            state = self.settings.value("window_state")
            if state is not None:
                self.restoreState(state)
        except Exception:
            pass

        # Ensure deviation list is docked at the bottom and never floating over document viewers
        self.results_dock.setFloating(False)
        if self.dockWidgetArea(self.results_dock) != Qt.DockWidgetArea.BottomDockWidgetArea:
            self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.results_dock)

        try:
            splitter_state = self.settings.value("deviation_splitter")
            if splitter_state is not None:
                self.results_panel.splitter.restoreState(splitter_state)
        except Exception:
            pass

    def _reset_layout(self) -> None:
        self.results_dock.setFloating(False)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.results_dock)
        self.results_dock.show()
        self.resizeDocks([self.results_dock], [340], Qt.Orientation.Vertical)
        deviations = self.results_panel.splitter
        deviations.setSizes([deviations.width() * 3 // 5 or 1, deviations.width() * 2 // 5 or 1])
        splitter = self.centralWidget()
        half = max(splitter.width() // 2, 1)
        splitter.setSizes([half, half])

    def _set_theme(self, mode: str) -> None:
        theme.apply(mode)
        self.settings.setValue("theme", mode)

    def _refresh_busy(self) -> None:
        busy = (
            self._active_worker is not None
            or any(p._converting_path for p in (self.original_panel, self.suspect_panel))
            or any(not self.extraction_cache.extraction_for(p).done() for p in self._loaded_paths())
        )
        self.busy_bar.setVisible(busy)

    def _update_window_title(self) -> None:
        names = [os.path.basename(p) for p in (self.original_panel.file_path, self.suspect_panel.file_path) if p]
        self.setWindowTitle(" \u2194 ".join(names) + f" \u2014 {APP_NAME}" if names else APP_NAME)

    def _update_review_actions(self) -> None:
        total = len(self.results_panel.all_items())
        self.results_dock.setWindowTitle(f"Deviations · {total}" if total else "Deviations")
        self.toggle_deviations_action.setText(f"&Deviation List ({total})" if total else "&Deviation List")
        has_rows = self.results_panel.rowCount() > 0
        has_selection = bool(self.results_panel.table.selectedIndexes())
        for action in (self.next_action, self.prev_action):
            action.setEnabled(has_rows)
        for action in (self.accept_action, self.reject_action, self.reset_action):
            action.setEnabled(has_selection)

    def _step_difference(self, step: int) -> None:
        if not self.results_panel.select_adjacent(step):
            self.status_label.setText(
                "No more differences." if self.results_panel.rowCount() else "No differences listed."
            )

    def _focus_search(self) -> None:
        self.results_dock.show()
        self.results_panel.search_box.setFocus()
        self.results_panel.search_box.selectAll()

    def _show_shortcuts(self) -> None:
        rows = "".join(
            f"<tr><td style='padding:3px 18px 3px 0'>{name}</td><td><b>{keys}</b></td></tr>"
            for name, keys in _SHORTCUTS
        )
        QMessageBox.information(self, "Keyboard Shortcuts", f"<table>{rows}</table>")

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<b>{APP_NAME}</b> {APP_VERSION}<br><br>"
            "Compares an approved document with a suspect copy (PDF, Word, "
            "PowerPoint, Excel) word by word, and tracks the review of every "
            "difference.",
        )

    # ------------------------------------------------------------------
    # Load-state tracking -- enable Compare only once both sides are ready
    # ------------------------------------------------------------------

    def _on_document_loaded(self, path: str) -> None:
        self._token_map = None
        self._update_window_title()
        self._update_compare_enabled()
        self._start_extraction(path)

    def _on_document_loading(self, path: str) -> None:
        self._token_map = None
        self._update_compare_enabled()
        self._refresh_busy()
        if self._active_worker is None:
            self.status_label.setText(f"Laying out {os.path.basename(path)} with Microsoft Word...")

    # ------------------------------------------------------------------
    # Background extraction
    # ------------------------------------------------------------------

    def _start_extraction(self, path: str) -> None:
        self.extraction_cache.extraction_for(path)
        if self._active_worker is None:
            self._show_extraction_status()
        self._refresh_busy()

    def _loaded_paths(self) -> list[str]:
        return [p for p in (self.original_panel.file_path, self.suspect_panel.file_path) if p]

    def _is_loaded(self, path: str) -> bool:
        def norm(p: str) -> str:
            return os.path.normcase(os.path.abspath(p))

        return norm(path) in {norm(p) for p in self._loaded_paths()}

    def _on_extraction_ready(self, path: str) -> None:
        if self._active_worker is None and self._is_loaded(path):
            self._show_extraction_status()
        self._refresh_busy()

    def _show_extraction_status(self) -> None:
        paths = self._loaded_paths()
        pending = [
            os.path.basename(p)
            for p in paths
            if not self.extraction_cache.extraction_for(p).done()
        ]
        if pending:
            self.status_label.setText(f"Extracting {', '.join(pending)} in the background...")
        elif len(paths) == 2:
            self.status_label.setText("Both documents extracted -- ready to compare.")
        elif paths:
            self.status_label.setText(f"{os.path.basename(paths[0])} extracted.")

    def _on_extraction_failed(self, path: str, message: str) -> None:
        # The full error is shown in a dialog if the user clicks Compare.
        if self._active_worker is None and self._is_loaded(path):
            self.status_label.setText(f"Could not extract {os.path.basename(path)}: {message}")
        self._refresh_busy()

    def _update_compare_enabled(self) -> None:
        ready = (
            bool(self.original_panel.file_path and self.suspect_panel.file_path)
            and self._active_worker is None
        )
        self.compare_button.setEnabled(ready)
        self.compare_action.setEnabled(ready)
        self.compare_button.setToolTip(
            "Compare the two documents (F5)." if ready else "Open an Original and a Suspect document first."
        )
        any_loaded = (
            bool(self.original_panel.file_path or self.suspect_panel.file_path)
            and self._active_worker is None
        )
        if hasattr(self, "crop_button"):
            self.crop_button.setEnabled(any_loaded)
        if hasattr(self, "crop_action"):
            self.crop_action.setEnabled(any_loaded)

    # ------------------------------------------------------------------
    # Crop
    # ------------------------------------------------------------------

    def on_crop_clicked(self) -> None:
        """Open the Crop Header/Footer dialog."""
        dialog = CropDialog(
            original_path=self.original_panel.file_path,
            suspect_path=self.suspect_panel.file_path,
            existing_crops=self._crop_data if self._crop_data else None,
            parent=self,
        )
        if dialog.exec() == CropDialog.DialogCode.Accepted:
            self._crop_data = dialog.get_crop_data()
            self._draw_crop_overlays_on_panels()
            if self._crop_data:
                n_orig = len(self._crop_data.get(self.original_panel.file_path or "", {}))
                n_susp = len(self._crop_data.get(self.suspect_panel.file_path or "", {}))
                self.status_label.setText(
                    f"Crop set: {n_orig} original page(s), {n_susp} suspect page(s). "
                    f"Click Compare to run with crops applied."
                )
            else:
                self.status_label.setText("Crops cleared.")

    def _draw_crop_overlays_on_panels(self) -> None:
        """Draw semi-transparent red crop overlays on the main document panels."""
        if self.original_panel.file_path:
            self.original_panel.draw_crop_overlays(
                self._crop_data.get(self.original_panel.file_path, {})
            )
        if self.suspect_panel.file_path:
            self.suspect_panel.draw_crop_overlays(
                self._crop_data.get(self.suspect_panel.file_path, {})
            )

    # ------------------------------------------------------------------
    # Compare
    # ------------------------------------------------------------------

    def on_compare_clicked(self) -> None:
        if not (self.original_panel.file_path and self.suspect_panel.file_path):
            return  # button should be disabled in this case, but guard anyway

        self.compare_button.setEnabled(False)
        self.status_label.setText("Comparing...")
        self.results_panel.clear_results()
        self.results_panel.set_token_map(None)
        self._token_map = None
        self.original_panel.clear_highlight()
        self.suspect_panel.clear_highlight()
        # Stop blinking and clear previous overlays
        self._blink_timer.stop()
        self.original_panel.clear_overlays()
        self.suspect_panel.clear_overlays()

        approved_path = self.original_panel.file_path
        suspect_path = self.suspect_panel.file_path
        worker = DiffWorker(
            approved_path,
            suspect_path,
            approved_extraction=self.extraction_cache.extraction_for(approved_path),
            suspect_extraction=self.extraction_cache.extraction_for(suspect_path),
            crop_data=self._crop_data if self._crop_data else None,
        )
        worker.signals.finished.connect(self.on_diff_finished)
        worker.signals.failed.connect(self.on_diff_failed)
        worker.signals.progress.connect(self.status_label.setText)
        self._active_worker = worker
        self._refresh_busy()
        self.threadpool.start(worker)

    def on_diff_finished(self, result: ComparisonResult) -> None:
        results = result.items
        self._active_worker = None
        self._diff_results = results
        self._draw_crop_overlays_on_panels()
        self._token_map = result.token_map
        self.results_panel.set_token_map(result.token_map)
        self._update_compare_enabled()
        self._blink_visible = True
        self._refresh_busy()
        self.results_panel.set_results(results)
        if results:
            noun = "difference" if len(results) == 1 else "differences"
            critical = sum(1 for d in results if d.severity == "critical")
            self.status_label.setText(f"{len(results)} {noun} found \u00b7 {critical} critical.")
            
            self.show_highlights_checkbox.setEnabled(True)
            if self.show_highlights_checkbox.isChecked():
                self.blink_checkbox.setEnabled(True)
                if self.blink_checkbox.isChecked():
                    self._blink_visible = True
                    self._blink_timer.start()
            else:
                self.blink_checkbox.setEnabled(False)
                self.original_panel.set_overlays_visible(False)
                self.suspect_panel.set_overlays_visible(False)
        else:
            self.status_label.setText("No differences found.")
            self.show_highlights_checkbox.setEnabled(False)
            self.blink_checkbox.setEnabled(False)
            self._blink_timer.stop()

    def _redraw_overlays(self) -> None:
        """Highlight on the documents exactly the differences the list
        shows, minus accepted ones (they're done)."""
        listed = [
            item
            for item in self.results_panel.listed_items()
            if (decision := self.results_panel.decision(item)) is None or decision.status != ACCEPTED
        ]
        self.original_panel.draw_all_overlays(listed, side="approved")
        self.suspect_panel.draw_all_overlays(listed, side="suspect")
        visible = self.show_highlights_checkbox.isChecked() and self._blink_visible
        self.original_panel.set_overlays_visible(visible)
        self.suspect_panel.set_overlays_visible(visible)

    def _on_comparator_toggled(self, visible: bool) -> None:
        self.results_panel.set_comparator_visible(visible)
        self.settings.setValue("comparator", visible)

    def _on_reviewer_changed(self, name: str) -> None:
        self.results_panel.reviewer = name.strip() or "unknown"

    def on_diff_failed(self, message: str) -> None:
        self._active_worker = None
        self._refresh_busy()
        self._update_compare_enabled()
        self.status_label.setText("Comparison failed.")
        QMessageBox.critical(self, "Comparison failed", message)

    def on_result_activated(self, diff_item: DiffItem, source: DocumentPanel | None = None) -> None:
        """Click-to-navigate: highlight the word on whichever panel(s) have it.

        ``source`` is the viewer the click came from (None for the
        results table: the viewer used last). The other viewer first
        takes the source viewer's zoom, so both show the difference at
        the same scale.

        A Location can span MULTIPLE pages (see core/diff_engine.py's
        Location.iter_pages) -- most commonly when comparing a
        multi-page document against a shorter/"concise" one, where a
        discrepancy's text on the longer document's page 3 must still
        resolve correctly against the shorter document's page 1
        rather than silently failing to highlight anywhere. Every
        (page, boxes) pair the Location touches gets its own
        highlight call; scrolling/centering follows the FIRST page
        touched, since that is what an "activated" click should focus
        on.

        For an insertion/deletion only ONE side has the text, so the
        other side gets a dashed red "missing" marker sized to about
        ONE CHARACTER at the actual insertion point in ITS OWN layout
        (`diff_item.approved_caret`/`suspect_caret` -- see
        core/diff_engine.py's `_caret_location`), not a marker the same
        size as the missing text drawn at the OTHER document's
        coordinates: a whole added sentence used to highlight as a
        sentence-sized box on the document that doesn't have the
        sentence, landing on unrelated content and looking like a
        mistake. The old other-side-bbox behavior is kept only as a
        fallback for the rare case with no caret at all (no neighboring
        word on either side to anchor to, e.g. an empty document).
        """
        # A fresh click replaces any previous found/missing markers.
        self.original_panel.clear_highlight()
        self.suspect_panel.clear_highlight()
        self._match_zoom(source or self._active_panel)

        found_any = False
        missing_side = None
        notes: list[str] = []
        if diff_item.approved_location is not None:
            loc = diff_item.approved_location
            ok = self.original_panel.highlight_location(loc)
            found_any |= ok
            if not ok:
                missing_side = "Original"
            # The suspect side lacks this text: mark the actual
            # insertion point in the Suspect document's own layout.
            if diff_item.suspect_location is None:
                if diff_item.suspect_caret is not None:
                    self.suspect_panel.highlight_missing_location(diff_item.suspect_caret)
                else:
                    self.suspect_panel.highlight_missing_location(loc)
                    if loc.page > self.suspect_panel._page_count:
                        notes.append(
                            f"Suspect has only {self.suspect_panel._page_count} page(s); "
                            f"this text is on page {loc.page} of the Original."
                        )
        if diff_item.suspect_location is not None:
            loc = diff_item.suspect_location
            ok = self.suspect_panel.highlight_location(loc)
            found_any |= ok
            if not ok:
                missing_side = "Suspect" if missing_side is None else "both"
            if diff_item.approved_location is None:
                if diff_item.approved_caret is not None:
                    self.original_panel.highlight_missing_location(diff_item.approved_caret)
                else:
                    self.original_panel.highlight_missing_location(loc)
                    if loc.page > self.original_panel._page_count:
                        notes.append(
                            f"Original has only {self.original_panel._page_count} page(s); "
                            f"this text is on page {loc.page} of the Suspect."
                        )

        if notes:
            self.status_label.setText(" ".join(notes))
        elif not found_any:
            self.status_label.setText(
                "That discrepancy's page doesn't exist in this document."
            )
        elif missing_side is not None:
            self.status_label.setText(
                f"Jumped to the match; the {missing_side} document doesn't have that page."
            )

    # ------------------------------------------------------------------
    # Blink control for severity overlays
    # ------------------------------------------------------------------

    def _on_blink_tick(self) -> None:
        """Toggle overlay visibility on each timer tick."""
        self._blink_visible = not self._blink_visible
        self.original_panel.set_overlays_visible(self._blink_visible)
        self.suspect_panel.set_overlays_visible(self._blink_visible)

    def _on_blink_toggled(self, state: int) -> None:
        """Checkbox toggled: start or stop blinking."""
        if state:
            # Resume blinking -- make sure overlays are visible first
            self._blink_visible = True
            self.original_panel.set_overlays_visible(True)
            self.suspect_panel.set_overlays_visible(True)
            self._blink_timer.start()
        else:
            # Stop blinking -- keep overlays visible (steady)
            self._blink_timer.stop()
            self._blink_visible = True
            self.original_panel.set_overlays_visible(True)
            self.suspect_panel.set_overlays_visible(True)

    def _on_show_highlights_toggled(self, state: int) -> None:
        """Checkbox toggled: show or hide overlays."""
        if state:
            self.blink_checkbox.setEnabled(True)
            if self.blink_checkbox.isChecked():
                self._blink_visible = True
                self._blink_timer.start()
            else:
                self._blink_visible = True
            
            self.original_panel.set_overlays_visible(self._blink_visible)
            self.suspect_panel.set_overlays_visible(self._blink_visible)
        else:
            self.blink_checkbox.setEnabled(False)
            self._blink_timer.stop()
            self._blink_visible = False
            self.original_panel.set_overlays_visible(False)
            self.suspect_panel.set_overlays_visible(False)

    # ------------------------------------------------------------------
    # Viewer coordination: lock, zoom matching, click-to-sync
    # ------------------------------------------------------------------

    def _other(self, panel: DocumentPanel) -> DocumentPanel:
        return self.suspect_panel if panel is self.original_panel else self.original_panel

    def _side(self, panel: DocumentPanel) -> str:
        return APPROVED if panel is self.original_panel else SUSPECT

    def _set_active_panel(self, panel: DocumentPanel) -> None:
        self._active_panel = panel

    def _match_zoom(self, source: DocumentPanel) -> None:
        """Give the other viewer ``source``'s zoom (no-op if equal)."""
        target = self._other(source)
        if abs(target.zoom_factor() - source.zoom_factor()) > 1e-6:
            self._syncing_views = True
            try:
                target.set_zoom(source.zoom_factor())
            finally:
                self._syncing_views = False

    def _on_lock_toggled(self, locked: bool) -> None:
        if locked:
            self._match_zoom(self._active_panel)
            self._follow(self._active_panel)
            self.status_label.setText("Views locked: zoom and scrolling move both documents.")
        else:
            self.status_label.setText("Views unlocked: each document zooms and scrolls on its own.")

    def _on_panel_zoomed(self, panel: DocumentPanel, _factor: float) -> None:
        if self.lock_checkbox.isChecked() and not self._syncing_views:
            self._match_zoom(panel)
            self._follow(panel)

    def _on_view_moved(self, panel: DocumentPanel) -> None:
        if self.lock_checkbox.isChecked() and not self._syncing_views:
            self._follow(panel)

    def _follow(self, source: DocumentPanel) -> None:
        """Move the other viewer to what ``source`` shows at its center:
        the same text after a comparison, else the same page position."""
        position = source.view_position()
        if position is None:
            return
        page, x, y = position
        target = self._other(source)
        target_position = (page, x, y)
        if self._token_map is not None:
            side = self._side(source)
            clicked = self._token_map.token_at(side, page, x, y, max_distance=None)
            if clicked is not None:
                # The closest word that reliably exists in both documents.
                index, other_index = self._token_map.nearby_counterpart(side, clicked)
                if other_index is not None:
                    here = self._token_map.location(side, index)
                    there = self._token_map.location(self._side(target), other_index)
                    if here is not None and there is not None:
                        # Keep the view's offset from the word, so the
                        # other viewer scrolls smoothly with this one.
                        target_position = (
                            there.page,
                            x + (_center_x(there) - _center_x(here)),
                            y + (_center_y(there) - _center_y(here)),
                        )
        self._syncing_views = True
        try:
            target.center_on_position(*target_position)
        finally:
            self._syncing_views = False

    def _on_viewer_clicked(self, panel: DocumentPanel, page: int, x: float, y: float) -> None:
        """A click on a page.

        * Text that exists in the other document (matched by the
          comparison, or found there by its surrounding phrase): both
          words are marked and the other viewer goes to it. A word found
          only by its phrase that lies in a highlighted difference also
          selects that row.
        * Otherwise, a highlighted difference under the click selects
          its row and navigates both viewers to it.
        * Otherwise the other viewer stays where it is -- it is never
          sent to unrelated text -- and the status bar says why.
        """
        self._active_panel = panel
        other = self._other(panel)
        side = self._side(panel)
        item = panel.item_at(page, x, y) if self.show_highlights_checkbox.isChecked() else None

        if self._token_map is None:
            if item is not None:
                self._activate_from_viewer(item, panel)
            elif other.file_path is not None:
                self._match_zoom(panel)
                other.center_on_position(page, x, y)
                self.status_label.setText(
                    "Showing the same page position -- compare the documents to jump to the same text."
                )
            return

        index = self._token_map.token_at(side, page, x, y)
        other_index, kind = (None, None) if index is None else self._token_map.counterpart(side, index)

        if other_index is None:
            if item is not None:
                self._activate_from_viewer(item, panel)
                return
            self.original_panel.clear_highlight()
            self.suspect_panel.clear_highlight()
            if index is not None:
                here = self._token_map.location(side, index)
                if here is not None:
                    panel.highlight_location(here, style="sync", navigate=False)
                self.status_label.setText(
                    f"This text isn't in the {self._title(other)} (removed or reworded), "
                    "so the other view stays where it is."
                )
            return

        self._match_zoom(panel)
        self.original_panel.clear_highlight()
        self.suspect_panel.clear_highlight()
        here = self._token_map.location(side, index)
        if here is not None:
            panel.highlight_location(here, style="sync", navigate=False)
        there = self._token_map.location(self._side(other), other_index)
        if there is not None:
            other.highlight_location(there, style="sync")
        message = f"Showing the same text in the {self._title(other)}"
        if kind == CONTEXT:
            message += " (found by its surrounding words; the comparison paired this passage differently)"
        if kind == CONTEXT and item is not None and self.results_panel.select_item(item):
            message += "; its flagged difference is selected below"
        self.status_label.setText(message + ".")

    def _activate_from_viewer(self, item: DiffItem, panel: DocumentPanel) -> None:
        shown = self.results_panel.select_item(item)
        self.on_result_activated(item, source=panel)
        self.status_label.setText(
            "Selected this difference in the results below."
            if shown
            else "This difference is hidden by the table filter above the results."
        )

    def _title(self, panel: DocumentPanel) -> str:
        return "Original" if panel is self.original_panel else "Suspect"

    def closeEvent(self, event) -> None:  # noqa: N802 -- Qt override
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("window_state", self.saveState())
        self.settings.setValue("deviation_splitter", self.results_panel.splitter.saveState())
        self.settings.setValue("last_dir", DocumentPanel.last_dir)
        if self._active_worker is not None:
            self._active_worker.cancel()
        self.extraction_cache.shutdown()
        word_pool.shutdown()
        super().closeEvent(event)


def _center_x(location) -> float:
    return (location.bbox[0] + location.bbox[2]) / 2


def _center_y(location) -> float:
    return (location.bbox[1] + location.bbox[3]) / 2


def main() -> None:
    multiprocessing.freeze_support()  # extraction worker processes in a frozen build
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("DocForensics")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
