"""PyMuPDF-backed page with the pdfplumber interface extraction uses.

pdfplumber builds its character and vector objects by interpreting the
page content in pure Python (pdfminer). That interpretation was about
two thirds of extraction time -- 8.7s for the characters of one 2-page
print sheet, where PyMuPDF's C engine reads the same 92,000 characters
in 0.25s.

`MuPdfPage` reads characters and vector drawings with PyMuPDF and
presents them the way core/pdf_extractor.py already consumes a
pdfplumber page:

  * ``chars`` -- one dict per glyph with pdfplumber's keys (text, x0,
    x1, top, bottom, doctop, upright, fontname, size, matrix, ...), in
    content-stream order;
  * ``extract_words(**kwargs)`` -- pdfplumber's own word grouping
    (`pdfplumber.utils.text.extract_words`) over those chars;
  * ``find_tables(table_settings)`` -- pdfplumber's own table finder
    (`pdfplumber.table.TableFinder`) over ``edges`` built from the
    drawings; ``lines`` / ``rects`` for the gutter checks;
  * ``bbox`` / ``width`` / ``height``.

Coordinates are in the space the viewer renders (PyMuPDF's page
rectangle, origin at its top-left, y down, page rotation applied), so
``bbox`` starts at (0, 0) and no MediaBox offset correction is needed.
The text matrix is rebuilt from each span's writing direction as a unit
rotation (a, b, c, d) plus the glyph origin in PDF y-up space (e, f);
extraction uses it only for baselines and rotated-text direction.
"""

from __future__ import annotations

from typing import Any

import fitz
import numpy as np
from pdfplumber.table import TableFinder
from pdfplumber.utils.geometry import rect_to_edges
from pdfplumber.utils.text import LIGATURES as _LIGATURES
from pdfplumber.utils.text import extract_words as _plumber_extract_words

_REPLACEMENT_CHAR = "�"

# The Symbol font's characters as Unicode. A PDF made from Word often
# writes them as private-use codes (U+F000 + the font's own code): an
# arrow in a table, "", against "↓" in the other version of
# the same label -- the same arrow on both pages, read as a difference
# 42 times. Only for a font named Symbol: other symbol fonts (Wingdings)
# put different glyphs at the same codes.
_SYMBOL_FONT_CHARS = dict(zip(
    range(0x20, 0x7F),
    " !∀#∃%&∋()∗+,−./0123456789:;<=>?"
    "≅ΑΒΧΔΕΦΓΗΙϑΚΛΜΝΟ"
    "ΠΘΡΣΤΥςΩΞΨΖ[∴]⊥_"
    "‾αβχδεφγηιϕκλμνο"
    "πθρστυϖωξψζ{|}∼",
))
_SYMBOL_FONT_CHARS.update({
    0xA2: "′", 0xA3: "≤", 0xA4: "⁄", 0xA5: "∞", 0xA6: "ƒ", 0xAB: "↔",
    0xAC: "←", 0xAD: "↑", 0xAE: "→", 0xAF: "↓", 0xB0: "°", 0xB1: "±",
    0xB2: "″", 0xB3: "≥", 0xB4: "×", 0xB5: "∝", 0xB6: "∂", 0xB7: "•",
    0xB8: "÷", 0xB9: "≠", 0xBA: "≡", 0xBB: "≈", 0xBC: "…", 0xD7: "⋅",
})


# Letters spread evenly across a cell ("D o n n é e s"): at least this
# many in a row, gaps within this share of each other and under this many
# type sizes (a short word spread over a wide cell: "d o s e").
_SPREAD_MIN_LETTERS = 4
_SPREAD_GAP_TOLERANCE = 0.15
_SPREAD_MAX_GAP_EM = 2.5


def _join_letter_spaced(words: list[dict]) -> list[dict]:
    """``words`` with each run of single letters spread evenly along a
    line -- a Word table cell set to "distributed" alignment, "D o n n é e
    s" for "Données" -- joined into the word it spells. Only a word's
    shape (a first letter, then lower-case letters) and only even spacing:
    atom labels of a structure ("O O N H S") and other single capitals stay
    apart."""
    out: list[dict] = []
    k = 0
    while k < len(words):
        run = [words[k]]
        j = k + 1
        while j < len(words) and _spread_letter(words[j]) and _spread_letter(run[-1]) and _same_line(run[-1], words[j]):
            gaps = [b["x0"] - a["x1"] for a, b in zip(run, run[1:])]
            gap = words[j]["x0"] - run[-1]["x1"]
            size = run[-1]["bottom"] - run[-1]["top"]
            if not 0 < gap < _SPREAD_MAX_GAP_EM * size or (gaps and abs(gap - sum(gaps) / len(gaps)) > _SPREAD_GAP_TOLERANCE * sum(gaps) / len(gaps) + 0.3):
                break
            run.append(words[j])
            j += 1
        letters = "".join(w["text"] for w in run)
        if (len(run) >= _SPREAD_MIN_LETTERS and letters[1:].islower()
                and all(w.get("upright", True) for w in run)):
            out.append(_merged_word(run))
            k = j
        else:
            out.append(words[k])
            k += 1
    return out


def _spread_letter(word: dict) -> bool:
    return len(word["text"]) == 1 and word["text"].isalpha()


def _same_line(a: dict, b: dict) -> bool:
    return abs(a["top"] - b["top"]) < 0.5 * (a["bottom"] - a["top"]) and b["x0"] > a["x0"]


def _merged_word(run: list[dict]) -> dict:
    merged = dict(run[0])
    merged["text"] = "".join(w["text"] for w in run)
    merged["x0"], merged["x1"] = run[0]["x0"], run[-1]["x1"]
    merged["top"] = min(w["top"] for w in run)
    merged["bottom"] = max(w["bottom"] for w in run)
    for key in ("doctop",):
        if key in merged:
            merged[key] = min(w.get(key, w["top"]) for w in run)
    if "chars" in merged:
        merged["chars"] = [c for w in run for c in w.get("chars") or []]
    return merged


def _without_overprints(chars: list[dict]) -> list[dict]:
    """``chars`` without a glyph drawn again on top of itself -- the same
    character, size and place (within half a point). An artwork slug drawn
    twice read "881155--0022--22002222..iinndddd" for "815-02-2022.indd"."""
    seen: set[tuple] = set()
    kept: list[dict] = []
    for c in chars:
        key = (c["text"], round(c["x0"] * 2), round(c["top"] * 2), round(float(c.get("size") or 0.0), 1))
        if key in seen and not c["text"].isspace():
            continue
        seen.add(key)
        kept.append(c)
    return kept if len(kept) < len(chars) else chars


def _without_underline_glyphs(chars: list[dict]) -> list[dict]:
    """``chars`` without the "_" glyphs drawn over other text: a sheet
    header "Final Package Insert" underlined by a line of underscores
    typed across it read as "_F_in__a_l _P_a_c_k_a_g_e_". A run of
    underscores lying on letters of its line is decoration, all of it; one
    on its own (a rule, a blank to fill in) is kept."""
    underscores = [k for k, c in enumerate(chars) if c["text"] == "_"]
    if not underscores:
        return chars
    bucket = 10.0
    by_x: dict[int, list[dict]] = {}
    for c in chars:
        if c["text"] not in ("_", " "):
            for b in range(int(c["x0"] // bucket), int(c["x1"] // bucket) + 1):
                by_x.setdefault(b, []).append(c)

    def over_text(u: dict) -> bool:
        height = u["bottom"] - u["top"]
        for b in range(int(u["x0"] // bucket), int(u["x1"] // bucket) + 1):
            for c in by_x.get(b, ()):
                if (min(u["x1"], c["x1"]) - max(u["x0"], c["x0"]) > 0.3 * (u["x1"] - u["x0"])
                        and min(u["bottom"], c["bottom"]) - max(u["top"], c["top"])
                        > 0.5 * min(height, c["bottom"] - c["top"])):
                    return True
        return False

    # The underscores of one line, drawn one after another, are one rule:
    # when any of it lies on text, all of it is the underline.
    runs: list[list[int]] = []
    for k in underscores:
        last = chars[runs[-1][-1]] if runs else None
        if last is not None and runs[-1][-1] == k - 1 and abs(chars[k]["top"] - last["top"]) < 1.0:
            runs[-1].append(k)
        else:
            runs.append([k])
    drop = {k for run in runs if any(over_text(chars[k]) for k in run) for k in run}
    if not drop:
        return chars
    return [c for k, c in enumerate(chars) if k not in drop]


def _char_text(ucs: int, font: str) -> str:
    """The text of one glyph: its Unicode, a Symbol-font private-use code
    as the character it draws (see `_SYMBOL_FONT_CHARS`)."""
    if ucs < 0:
        return _REPLACEMENT_CHAR
    if 0xF020 <= ucs <= 0xF0FF and "symbol" in font.lower():
        return _SYMBOL_FONT_CHARS.get(ucs - 0xF000, chr(ucs))
    return chr(ucs)

# A path segment is kept for the table finder when it is exactly
# horizontal or vertical, and for the gutter-rule check when it is
# within this much of straight and at least this long (see
# core.pdf_extractor._find_gutter_crossing_hlines).
_NEAR_STRAIGHT = 2.5
_LONG_RULE = 30.0


class MuPdfPage:
    def __init__(self, page: fitz.Page) -> None:
        self._page = page
        self._boxes: "_BoxIndex | None" = None
        rect = page.rect
        self.width = float(rect.width)
        self.height = float(rect.height)
        self.bbox = (0.0, 0.0, self.width, self.height)
        # Unrotated page space -> the displayed (rotated) page, origin at
        # its top-left.
        self._to_view = page.rotation_matrix * fitz.Matrix(1, 0, 0, 1, -rect.x0, -rect.y0)
        self._rotated = page.rotation != 0 or rect.x0 != 0 or rect.y0 != 0
        self._chars: list[dict] | None = None
        self._lines: list[dict] | None = None
        self._rects: list[dict] | None = None
        self._edges: list[dict] | None = None

    # --- text ------------------------------------------------------------

    @property
    def chars(self) -> list[dict]:
        if self._chars is None:
            self._chars = self._read_chars()
        return self._chars

    def _read_chars(self) -> list[dict]:
        height = self.height
        to_view = self._to_view
        rotation = fitz.Matrix(to_view.a, to_view.b, to_view.c, to_view.d, 0, 0)
        chars: list[dict] = []
        append = chars.append
        previous = None
        for span in self._page.get_texttrace():
            # Text painted with fill AND stroke (render mode 2) arrives as
            # a fill span followed by an identical stroke span; pdfminer
            # reports each glyph once, so the repeat is dropped.
            signature = tuple((c[0], c[3]) for c in span["chars"])
            if span["type"] in (1, 2) and signature == previous:
                continue
            previous = signature
            size = span["size"]
            font = span["font"]
            dx, dy = span["dir"]
            if self._rotated:
                direction = fitz.Point(dx, dy) * rotation
                dx, dy = direction.x, direction.y
            # pdfminer's rule: upright unless turned a quarter or more --
            # and not upside down: a foil prints every other row turned
            # half a turn, and read as upright text "DRUG - CAUTION" came
            # out "NOITUAC - GURD". Turned text is read along its own
            # direction (core.pdf_extractor `_ordered_rotated_words`).
            upright = dx > 1e-6
            a, b, c, d = dx, -dy, dy, dx
            span_chars = span["chars"]
            if self._rotated:
                # The whole span in one step, in MuPDF's own single
                # precision (see _view_points) -- per glyph, PyMuPDF
                # objects cost ~60us, most of extraction on a rotated page.
                view_ox, view_oy = _view_points(
                    [g[2][0] for g in span_chars], [g[2][1] for g in span_chars], to_view
                )
                view_boxes = _view_rects([g[3] for g in span_chars], to_view)
            i = 0
            n = len(span_chars)
            while i < n:
                ucs, _gid, origin, glyph_bbox = span_chars[i]
                if self._rotated:
                    x0, top, x1, bottom = view_boxes[i]
                    ox, oy = view_ox[i], view_oy[i]
                else:
                    x0, top, x1, bottom = glyph_bbox
                    ox, oy = origin

                # PyMuPDF decomposes multi-character ligature glyphs (e.g. "ti",
                # "ffi", "fl") into a base char (gid >= 0, non-zero bbox) followed
                # by continuation chars (gid == -1, zero-width bbox). Apportion
                # the glyph's advance width across all characters in the ligature
                # group so subsequent letters aren't seen as separated by a wide gap.
                j = i + 1
                while (
                    j < n
                    and span_chars[j][1] == -1
                    and abs(span_chars[j][3][2] - span_chars[j][3][0]) < 1e-3
                ):
                    j += 1

                if j == i + 1:
                    # One glyph, one character -- nearly every glyph. The
                    # general formulas below with k = 0, group_len = 1,
                    # written out so the values are bit for bit the same.
                    glyph_w = x1 - x0
                    glyph_h = bottom - top
                    if upright:
                        cur_x0, cur_x1, cur_top, cur_bottom = x0 + 0 * glyph_w, x0 + glyph_w, top, bottom
                        cur_ox, cur_oy = ox + 0 * glyph_w, oy
                    else:
                        cur_x0, cur_x1 = x0, x1
                        cur_top, cur_bottom = top + 0 * glyph_h, top + glyph_h
                        cur_ox, cur_oy = ox, oy + 0 * glyph_h
                    append(
                        {
                            "text": _char_text(ucs, font),
                            "x0": cur_x0,
                            "x1": cur_x1,
                            "top": cur_top,
                            "bottom": cur_bottom,
                            "doctop": cur_top,
                            "y0": height - cur_bottom,
                            "y1": height - cur_top,
                            "width": cur_x1 - cur_x0,
                            "height": cur_bottom - cur_top,
                            "upright": upright,
                            "fontname": font,
                            "size": size,
                            "matrix": (a, b, c, d, cur_ox, height - cur_oy),
                            "object_type": "char",
                        }
                    )
                    i = j
                    continue

                group_len = j - i
                glyph_w = x1 - x0
                glyph_h = bottom - top

                for k in range(group_len):
                    cur_ucs, cur_gid, cur_origin, _ = span_chars[i + k]
                    if self._rotated:
                        cur_ox, cur_oy = view_ox[i + k], view_oy[i + k]
                    else:
                        cur_ox, cur_oy = cur_origin

                    if upright:
                        cur_x0 = x0 + k * (glyph_w / group_len)
                        cur_x1 = x0 + (k + 1) * (glyph_w / group_len)
                        cur_top = top
                        cur_bottom = bottom
                        cur_ox = ox + k * (glyph_w / group_len)
                    else:
                        cur_x0 = x0
                        cur_x1 = x1
                        cur_top = top + k * (glyph_h / group_len)
                        cur_bottom = top + (k + 1) * (glyph_h / group_len)
                        cur_oy = oy + k * (glyph_h / group_len)

                    append(
                        {
                            "text": _char_text(cur_ucs, font),
                            "x0": cur_x0,
                            "x1": cur_x1,
                            "top": cur_top,
                            "bottom": cur_bottom,
                            "doctop": cur_top,
                            "y0": height - cur_bottom,
                            "y1": height - cur_top,
                            "width": cur_x1 - cur_x0,
                            "height": cur_bottom - cur_top,
                            "upright": upright,
                            "fontname": font,
                            "size": size,
                            "matrix": (a, b, c, d, cur_ox, height - cur_oy),
                            "object_type": "char",
                        }
                    )
                i = j
        return chars

    def extract_words(self, **kwargs: Any) -> list[dict]:
        """pdfplumber's word grouping over the glyphs, then split wherever
        the PDF's OWN word spaces say a word ends.

        The glyph stream (`get_texttrace`) leaves out most space
        characters -- 304 of 5,138 on one real 6.3pt leaflet page -- so
        the grouping has only glyph gaps to go on, and at small sizes a
        word space (~1.5pt) is narrower than any safe gap tolerance:
        whole lines came back as one "word"
        ("patientswhohavenothealedafter4to8weeksoftreatment,"). Lowering
        the tolerance instead splits justified words letter by letter.
        MuPDF's own word segmentation does see the spaces, so its word
        boxes are used to ADD breaks -- never to join anything, and never
        to break where MuPDF sees one word.
        """
        want_chars = kwargs.get("return_chars", False)
        kwargs["return_chars"] = True
        words = _plumber_extract_words(_without_underline_glyphs(_without_overprints(self.chars)), **kwargs)
        if words:
            words = _split_at_word_boxes(words, self._word_boxes())
            words = _join_letter_spaced(words)
        if not want_chars:
            for word in words:
                word.pop("chars", None)
        return words

    def word_box_of(self, char: dict) -> int | None:
        """Which of MuPDF's own words ``char`` lies in (an index), or None
        -- MuPDF sees the word spaces the glyph stream leaves out."""
        if self._boxes is None:
            self._boxes = self._word_boxes()
        return self._boxes.which(char, self._boxes.overlapping(char))

    def _word_boxes(self) -> "_BoxIndex":
        entries = self._page.get_text("words")
        if self._rotated:
            boxes = _view_rects([entry[:4] for entry in entries], self._to_view)
        else:
            boxes = [entry[:4] for entry in entries]
        return _BoxIndex(boxes)

    def extract_text(self) -> str:
        return self._page.get_text()

    # --- vector graphics ---------------------------------------------------

    @property
    def lines(self) -> list[dict]:
        self._read_drawings()
        return self._lines

    @property
    def rects(self) -> list[dict]:
        self._read_drawings()
        return self._rects

    @property
    def edges(self) -> list[dict]:
        if self._edges is None:
            self._read_drawings()
            edges = [
                edge for rect in self._rects if not rect.get("highlight") for edge in rect_to_edges(rect)
            ]
            edges.extend(dict(line, object_type="curve_edge") for line in self._lines)
            self._edges = edges
        return self._edges

    def _read_drawings(self) -> None:
        """Ruling lines and rectangles, as pdfplumber line/rect dicts.

        Read from PyMuPDF's raw `get_cdrawings()` (plain tuples) rather
        than `get_drawings()`, which wraps every coordinate in a Python
        Point -- 3.3 million of them on one outlined scan, 23s for a
        4-page file. And only segments something downstream can use are
        kept: the table finder takes horizontal/vertical edges only, and
        the gutter-rule check a near-horizontal line 30pt or longer. A
        glyph outline's curves are neither; there were a million of them.
        The test is done before rotating, which is exact -- page rotation
        is a multiple of 90 degrees, which only swaps the axes.
        """
        if self._lines is not None:
            return
        lines: list[dict] = []
        rects: list[dict] = []
        to_view = self._to_view if self._rotated else None
        height = self.height
        near, long_rule = _NEAR_STRAIGHT, _LONG_RULE
        append = lines.append
        for path in self._page.get_cdrawings():
            items = path.get("items")
            if not items:
                continue
            highlight_fill = _is_highlight_fill(path)
            for item in items:
                kind = item[0]
                if kind == "l" or kind == "c":
                    p0 = item[1]
                    p1 = item[2] if kind == "l" else item[4]
                    segments = ((p0, p1),)
                elif kind == "re":
                    r = fitz.Rect(item[1]).normalize()
                    if to_view is not None:
                        r = r * to_view
                    rect = self._rect_dict(r)
                    if highlight_fill and _HIGHLIGHT_MIN_SIDE < r.width and _HIGHLIGHT_MIN_SIDE < r.height <= _HIGHLIGHT_MAX_HEIGHT:
                        rect["highlight"] = True
                    rects.append(rect)
                    continue
                elif kind == "qu":
                    q = fitz.Quad(item[1])
                    corners = (tuple(q.ul), tuple(q.ur), tuple(q.lr), tuple(q.ll), tuple(q.ul))
                    segments = tuple(zip(corners, corners[1:]))
                else:
                    continue
                for p0, p1 in segments:
                    x0, y0 = p0[0], p0[1]
                    x1, y1 = p1[0], p1[1]
                    if x0 != x1 and y0 != y1:
                        dx = x1 - x0 if x1 > x0 else x0 - x1
                        dy = y1 - y0 if y1 > y0 else y0 - y1
                        if not ((dx <= near and dy >= long_rule) or (dy <= near and dx >= long_rule)):
                            continue  # a diagonal or curve no consumer looks at
                    if to_view is not None:
                        # Rotated page: MuPDF's own (single-precision)
                        # transform, exactly as before.
                        a, b = fitz.Point(p0) * to_view, fitz.Point(p1) * to_view
                        x0, y0, x1, y1 = a.x, a.y, b.x, b.y
                    append(_segment(x0, y0, x1, y1, height))
        self._lines = lines
        self._rects = rects

    def _rect_dict(self, r: fitz.Rect) -> dict:
        return {
            "object_type": "rect",
            "x0": r.x0,
            "x1": r.x1,
            "top": r.y0,
            "bottom": r.y1,
            "doctop": r.y0,
            "y0": self.height - r.y1,
            "y1": self.height - r.y0,
            "width": r.width,
            "height": r.height,
        }

    def find_tables(self, table_settings: dict | None = None) -> list:
        return TableFinder(self, table_settings).tables


# A text highlight (Word's highlighter, a reviewer's marked change) is a
# rectangle filled -- not outlined -- in a pure highlighter color behind
# one line of text. Its edges are not table rules: taken as rules they cut
# a table's real columns into slivers ("4 doses of 2.5 mg/0.6 mL" across
# three of them), the table was then rejected as a grid over flowing text,
# and read line by line, every cell running into the next. Borders, which
# Word draws as thin black filled rectangles, and cell shading (greys,
# tints) are kept.
_HIGHLIGHT_MIN_SIDE = 2.0  # thinner is a drawn rule
_HIGHLIGHT_MAX_HEIGHT = 30.0  # one line of text, not a shaded block


def _is_highlight_fill(path: dict) -> bool:
    if path.get("type") != "f":
        return False
    fill = path.get("fill")
    if not fill or len(fill) != 3:
        return False
    if not all(v <= 0.03 or v >= 0.97 for v in fill):
        return False  # a tint or a grey: shading
    levels = {round(v) for v in fill}
    return len(levels) == 2  # not black, not white: yellow, green, cyan, pink, red, blue


def _view_points(xs: list[float], ys: list[float], m: fitz.Matrix) -> tuple[list[float], list[float]]:
    """``fitz.Point(p) * m`` for many points at once, bit for bit.

    MuPDF transforms in single precision: x' = (x*a + y*c) + e. numpy
    float32 arithmetic rounds each step the same way, so the results are
    identical (checked against PyMuPDF on 512,283 glyph transforms, real
    rotated pages and random ones)."""
    x = np.asarray(xs, dtype=np.float32)
    y = np.asarray(ys, dtype=np.float32)
    a, b, c, d, e, f = (np.float32(v) for v in (m.a, m.b, m.c, m.d, m.e, m.f))
    return ((x * a + y * c) + e).tolist(), ((x * b + y * d) + f).tolist()


def _view_rects(boxes: list, m: fitz.Matrix) -> list[tuple[float, float, float, float]]:
    """``fitz.Rect(r) * m`` for many rectangles at once, bit for bit:
    the four corners transformed, then their bounding box."""
    if not boxes:
        return []
    x0s, y0s, x1s, y1s = (list(v) for v in zip(*boxes))
    corners = [_view_points(px, py, m) for px, py in ((x0s, y0s), (x1s, y0s), (x0s, y1s), (x1s, y1s))]
    xs = np.array([c[0] for c in corners])
    ys = np.array([c[1] for c in corners])
    return list(zip(xs.min(0).tolist(), ys.min(0).tolist(), xs.max(0).tolist(), ys.max(0).tolist()))


def _segment(x0: float, y0: float, x1: float, y1: float, height: float) -> dict:
    """A path segment as a pdfplumber line dict."""
    if x0 == x1:
        orientation = "v"
    elif y0 == y1:
        orientation = "h"
    else:
        orientation = None
    left, right = (x0, x1) if x0 <= x1 else (x1, x0)
    top, bottom = (y0, y1) if y0 <= y1 else (y1, y0)
    return {
        "object_type": "line",
        "x0": left,
        "x1": right,
        "top": top,
        "bottom": bottom,
        "doctop": top,
        "y0": height - bottom,
        "y1": height - top,
        "width": right - left,
        "height": bottom - top,
        "orientation": orientation,
    }


class _BoxIndex:
    """MuPDF word boxes on a grid of cells, for "which word is this glyph
    in?" lookups: a word only checks the few boxes sharing its cells, not
    every word on its line."""

    _BUCKET = 4.0
    _X_BUCKET = 40.0

    def __init__(self, boxes: list[tuple[float, float, float, float]]) -> None:
        self._boxes = boxes
        self._buckets: dict[tuple[int, int], list[int]] = {}
        for index, (x0, y0, x1, y1) in enumerate(boxes):
            for bucket in range(int(y0 // self._BUCKET), int(y1 // self._BUCKET) + 1):
                for column in range(int(x0 // self._X_BUCKET), int(x1 // self._X_BUCKET) + 1):
                    self._buckets.setdefault((bucket, column), []).append(index)

    def __bool__(self) -> bool:
        return bool(self._boxes)

    def overlapping(self, word: dict) -> list[int]:
        """Boxes overlapping ``word``'s rectangle -- usually exactly one,
        in which case there is nothing to split."""
        seen: set[int] = set()
        hits = []
        columns = range(int(word["x0"] // self._X_BUCKET), int(word["x1"] // self._X_BUCKET) + 1)
        for bucket in range(int(word["top"] // self._BUCKET), int(word["bottom"] // self._BUCKET) + 1):
            for column in columns:
                for index in self._buckets.get((bucket, column), ()):
                    if index in seen:
                        continue
                    seen.add(index)
                    x0, y0, x1, y1 = self._boxes[index]
                    if x0 < word["x1"] and x1 > word["x0"] and y0 < word["bottom"] and y1 > word["top"]:
                        hits.append(index)
        return hits

    def which(self, char: dict, candidates: list[int]) -> int | None:
        cx = (char["x0"] + char["x1"]) / 2.0
        cy = (char["top"] + char["bottom"]) / 2.0
        found = None
        for index in candidates:
            x0, y0, x1, y1 = self._boxes[index]
            if x0 - 0.3 <= cx <= x1 + 0.3 and y0 - 0.3 <= cy <= y1 + 0.3:
                if found is not None and found != index:
                    return None  # overlapping boxes: no opinion
                found = index
        return found


def _split_at_word_boxes(words: list[dict], boxes: _BoxIndex) -> list[dict]:
    if not boxes:
        return words
    out: list[dict] = []
    for word in words:
        chars = word.get("chars") or []
        if len(chars) < 2 or not word.get("upright", True) or _has_rtl(word["text"]):
            # Right-to-left words keep the existing grouping: MuPDF breaks
            # joined Arabic script into separate letters.
            out.append(word)
            continue
        candidates = boxes.overlapping(word)
        if len(candidates) < 2:
            out.append(word)
            continue
        owners = [boxes.which(c, candidates) for c in chars]
        cuts = [
            k for k in range(1, len(chars))
            if owners[k] is not None and owners[k - 1] is not None and owners[k] != owners[k - 1]
        ]
        expanded = "".join(_LIGATURES.get(c["text"], c["text"]) for c in chars)
        if not cuts or expanded != word["text"]:
            out.append(word)  # nothing to split, or text not rebuilt 1:1 from glyphs
            continue
        for start, end in zip([0] + cuts, cuts + [len(chars)]):
            part = chars[start:end]
            piece = dict(word)
            piece["chars"] = part
            piece["text"] = "".join(_LIGATURES.get(c["text"], c["text"]) for c in part)
            piece["x0"] = min(c["x0"] for c in part)
            piece["x1"] = max(c["x1"] for c in part)
            piece["top"] = min(c["top"] for c in part)
            piece["bottom"] = max(c["bottom"] for c in part)
            if "doctop" in word:
                piece["doctop"] = word["doctop"] + (piece["top"] - word["top"])
            piece["width"] = piece["x1"] - piece["x0"]
            piece["height"] = piece["bottom"] - piece["top"]
            out.append(piece)
    return out


def _has_rtl(text: str) -> bool:
    return any("֐" <= ch <= "ࣿ" or "יִ" <= ch <= "ﻼ" for ch in text)
