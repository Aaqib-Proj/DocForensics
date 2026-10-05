"""Faster, identical drop-ins for pdfplumber's table geometry
(core/fast_tables.py).

Table detection was 71% of extraction on a form-heavy report (7.7s of
10.8s), almost all of it in two pdfplumber functions that do avoidable
quadratic work:

* ``edges_to_intersections`` re-sorts every horizontal edge once PER
  vertical edge, then tests all of them.
* ``intersections_to_cells`` rebuilds sets of edge boxes on every one of
  hundreds of thousands of comparisons, and rescans the whole rest of the
  point list for each point to find the points below and to its right.

The versions here return exactly what pdfplumber's return -- the same
intersections, edges listed in the same order, the same cells in the
same order -- checked against the originals in
dev_tests/test_extraction_diff_smoke.py (TEST 33). Only the work is
different:

* the horizontal edges are sorted once, and only the slice whose ``top``
  could possibly qualify is visited; pdfplumber's own condition is then
  applied verbatim, so floating-point edge cases come out the same;
* each point's sets of edge boxes are built once, and the points below
  and to the right are read from per-column and per-row lists.

`install()` puts them in place of pdfplumber's (idempotent).
"""

from __future__ import annotations

import bisect
from operator import itemgetter
from typing import Optional

import pdfplumber.table as _table
from pdfplumber import utils as _utils

_ORIGINALS = {
    "edges_to_intersections": _table.edges_to_intersections,
    "intersections_to_cells": _table.intersections_to_cells,
}

# How far beyond the tolerance the candidate slice reaches. The exact
# test is pdfplumber's own, applied to every edge in the slice, so this
# only has to be large enough never to cut off an edge that test would
# accept.
_SLICE_MARGIN = 1.0


def edges_to_intersections(edges, x_tolerance=1, y_tolerance=1):
    intersections: dict = {}
    v_edges = [e for e in edges if e["orientation"] == "v"]
    h_edges = sorted((e for e in edges if e["orientation"] == "h"), key=itemgetter("top", "x0"))
    h_tops = [h["top"] for h in h_edges]
    for v in sorted(v_edges, key=itemgetter("x0", "top")):
        v_top, v_bottom, v_x0 = v["top"], v["bottom"], v["x0"]
        start = bisect.bisect_left(h_tops, v_top - y_tolerance - _SLICE_MARGIN)
        stop = bisect.bisect_right(h_tops, v_bottom + y_tolerance + _SLICE_MARGIN)
        for k in range(start, stop):
            h = h_edges[k]
            if (
                (v_top <= (h["top"] + y_tolerance))
                and (v_bottom >= (h["top"] - y_tolerance))
                and (v_x0 >= (h["x0"] - x_tolerance))
                and (v_x0 <= (h["x1"] + x_tolerance))
            ):
                vertex = (v_x0, h["top"])
                if vertex not in intersections:
                    intersections[vertex] = {"v": [], "h": []}
                intersections[vertex]["v"].append(v)
                intersections[vertex]["h"].append(h)
    return intersections


def intersections_to_cells(intersections):
    v_sets = {p: frozenset(map(_utils.obj_to_bbox, d["v"])) for p, d in intersections.items()}
    h_sets = {p: frozenset(map(_utils.obj_to_bbox, d["h"])) for p, d in intersections.items()}

    def edge_connects(p1, p2) -> bool:
        if p1[0] == p2[0] and not v_sets[p1].isdisjoint(v_sets[p2]):
            return True
        if p1[1] == p2[1] and not h_sets[p1].isdisjoint(h_sets[p2]):
            return True
        return False

    points = sorted(intersections.keys())
    # Points sharing an x, and sharing a y, each in the sorted order. For
    # point i, the points AFTER it in the sorted list with the same x are
    # the ones below it; with the same y, the ones to its right -- which
    # is what pdfplumber filters out of points[i + 1:].
    columns: dict = {}
    rows: dict = {}
    position: dict = {}
    for p in points:
        column = columns.setdefault(p[0], [])
        row = rows.setdefault(p[1], [])
        position[p] = (len(column), len(row))
        column.append(p)
        row.append(p)

    def find_smallest_cell(i: int) -> Optional[tuple]:
        if i == len(points) - 1:
            return None
        pt = points[i]
        in_column, in_row = position[pt]
        below = columns[pt[0]][in_column + 1:]
        right = rows[pt[1]][in_row + 1:]
        for below_pt in below:
            if not edge_connects(pt, below_pt):
                continue
            for right_pt in right:
                if not edge_connects(pt, right_pt):
                    continue
                bottom_right = (right_pt[0], below_pt[1])
                if (
                    (bottom_right in intersections)
                    and edge_connects(bottom_right, right_pt)
                    and edge_connects(bottom_right, below_pt)
                ):
                    return (pt[0], pt[1], bottom_right[0], bottom_right[1])
        return None

    return [cell for cell in (find_smallest_cell(i) for i in range(len(points))) if cell]


def install() -> None:
    _table.edges_to_intersections = edges_to_intersections
    _table.intersections_to_cells = intersections_to_cells


def originals() -> dict:
    """pdfplumber's own functions, for comparison in tests."""
    return dict(_ORIGINALS)
