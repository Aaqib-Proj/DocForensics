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
from pdfplumber.table import TableFinder
from pdfplumber.utils.geometry import rect_to_edges
from pdfplumber.utils.text import extract_words as _plumber_extract_words

_REPLACEMENT_CHAR = "�"


class MuPdfPage:
    def __init__(self, page: fitz.Page) -> None:
        self._page = page
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
            # pdfminer's rule: upright unless turned a quarter or more.
            upright = abs(dx) > 1e-6
            a, b, c, d = dx, -dy, dy, dx
            span_chars = span["chars"]
            i = 0
            n = len(span_chars)
            while i < n:
                ucs, _gid, origin, glyph_bbox = span_chars[i]
                if self._rotated:
                    r = fitz.Rect(glyph_bbox) * to_view
                    x0, top, x1, bottom = r.x0, r.y0, r.x1, r.y1
                    o = fitz.Point(origin) * to_view
                    ox, oy = o.x, o.y
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

                group_len = j - i
                glyph_w = x1 - x0
                glyph_h = bottom - top

                for k in range(group_len):
                    cur_ucs, cur_gid, cur_origin, _ = span_chars[i + k]
                    if self._rotated:
                        cur_o = fitz.Point(cur_origin) * to_view
                        cur_ox, cur_oy = cur_o.x, cur_o.y
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
                            "text": chr(cur_ucs) if cur_ucs >= 0 else _REPLACEMENT_CHAR,
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
        return _plumber_extract_words(self.chars, **kwargs)

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
            edges = [edge for rect in self._rects for edge in rect_to_edges(rect)]
            edges.extend(dict(line, object_type="curve_edge") for line in self._lines)
            self._edges = edges
        return self._edges

    def _read_drawings(self) -> None:
        if self._lines is not None:
            return
        lines: list[dict] = []
        rects: list[dict] = []
        to_view = self._to_view if self._rotated else None
        for path in self._page.get_drawings():
            for item in path["items"]:
                kind = item[0]
                if kind == "re":
                    r = fitz.Rect(item[1])
                    if to_view is not None:
                        r = r * to_view
                    rects.append(self._rect_dict(r))
                    continue
                if kind == "l":
                    points = [item[1], item[2]]
                elif kind == "c":
                    points = [item[1], item[4]]
                elif kind == "qu":
                    q = item[1]
                    points = [q.ul, q.ur, q.lr, q.ll, q.ul]
                else:
                    continue
                if to_view is not None:
                    points = [fitz.Point(p) * to_view for p in points]
                for p0, p1 in zip(points, points[1:]):
                    lines.append(self._segment_dict(p0, p1))
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

    def _segment_dict(self, p0, p1) -> dict:
        x0, x1 = min(p0.x, p1.x), max(p0.x, p1.x)
        top, bottom = min(p0.y, p1.y), max(p0.y, p1.y)
        if p0.x == p1.x:
            orientation = "v"
        elif p0.y == p1.y:
            orientation = "h"
        else:
            orientation = None
        return {
            "object_type": "line",
            "x0": x0,
            "x1": x1,
            "top": top,
            "bottom": bottom,
            "doctop": top,
            "y0": self.height - bottom,
            "y1": self.height - top,
            "width": x1 - x0,
            "height": bottom - top,
            "orientation": orientation,
        }

    def find_tables(self, table_settings: dict | None = None) -> list:
        return TableFinder(self, table_settings).tables
