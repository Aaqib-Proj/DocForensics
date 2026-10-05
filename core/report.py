"""Comparison report (core/report.py).

The record a regulated review needs to keep: which two documents were
compared (names, sizes and SHA-256, so the report can be tied to the
exact files), when and by which build, what the comparison could not
read, every difference with its severity, category and pages, and the
reviewer's decision on each -- who, when -- plus any row a rematch
removed. Written as a self-contained HTML page (to read, print to PDF
or attach) or as CSV (to sort and filter).

No Qt: standalone-testable.
"""

from __future__ import annotations

import csv
import html
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from core.diff_engine import CATEGORY_LABELS, SEVERITY_ORDER, DiffItem, Location
from core.review_store import file_digest


@dataclass
class ReportDecision:
    status: str  # "accepted" / "rejected"
    reviewer: str
    decided_at: datetime


@dataclass
class ReportInput:
    approved_path: str
    suspect_path: str
    items: list[DiffItem]
    decision_of: Callable[[DiffItem], Optional[ReportDecision]]
    category_of: Callable[[DiffItem], str]
    reviewer: str
    app_version: str
    warnings: list[str] = field(default_factory=list)
    rematch_log: list[dict] = field(default_factory=list)


_COLUMNS = (
    "#", "Severity", "Category", "Original page", "Suspect page",
    "Original text", "Suspect text", "Decision", "Decided by", "Decided at (UTC)",
)


def write_report(path: str, data: ReportInput) -> None:
    """Write ``data`` to ``path`` -- CSV for a .csv path, else HTML."""
    if path.lower().endswith(".csv"):
        _write_csv(path, data)
    else:
        _write_html(path, data)


def _pages(location: Optional[Location], caret: Optional[Location]) -> str:
    where = location or caret
    if where is None:
        return ""
    pages = sorted(where.boxes_by_page) or [where.page]
    return ", ".join(str(p) for p in pages)


def _rows(data: ReportInput) -> list[list[str]]:
    rows = []
    for number, item in enumerate(data.items, start=1):
        decision = data.decision_of(item)
        rows.append([
            str(number),
            item.severity,
            CATEGORY_LABELS.get(data.category_of(item), data.category_of(item)),
            _pages(item.approved_location, item.approved_caret),
            _pages(item.suspect_location, item.suspect_caret),
            item.approved_text,
            item.suspect_text,
            decision.status if decision else "not reviewed",
            decision.reviewer if decision else "",
            decision.decided_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if decision else "",
        ])
    return rows


def _file_facts(path: str) -> tuple[str, str, str]:
    try:
        size = f"{os.path.getsize(path):,} bytes"
        digest = file_digest(path)
    except OSError:
        size, digest = "unavailable", "unavailable"
    return os.path.basename(path), size, digest


def _write_csv(path: str, data: ReportInput) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        for label, file_path in (("Original", data.approved_path), ("Suspect", data.suspect_path)):
            name, size, digest = _file_facts(file_path)
            writer.writerow([f"{label} document", name, size, f"SHA-256 {digest}"])
        writer.writerow(["Generated", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                         f"by {data.reviewer or 'unknown'}", f"DocForensics Analyzer {data.app_version}"])
        for warning in data.warnings:
            writer.writerow(["WARNING", warning])
        writer.writerow([])
        writer.writerow(_COLUMNS)
        writer.writerows(_rows(data))
        if data.rematch_log:
            writer.writerow([])
            writer.writerow(["Rematch log", "When (UTC)", "Reviewer", "Outcome", "Original text", "Suspect text", "Reason"])
            for entry in data.rematch_log:
                writer.writerow(["", entry.get("at", ""), entry.get("reviewer", ""), entry.get("outcome", ""),
                                 entry.get("approved_text", ""), entry.get("suspect_text", ""), entry.get("reason", "")])


_SEVERITY_COLORS = {
    "critical": "#cf222e", "major": "#bc4c00", "minor": "#9a6700", "typo": "#6e7781", "reorder": "#6e7781",
}


def _write_html(path: str, data: ReportInput) -> None:
    e = html.escape
    rows = _rows(data)
    by_severity = Counter(item.severity for item in data.items)
    by_status = Counter(row[7] for row in rows)
    files = []
    for label, file_path in (("Original", data.approved_path), ("Suspect", data.suspect_path)):
        name, size, digest = _file_facts(file_path)
        files.append(f"<tr><th>{label}</th><td>{e(name)}</td><td>{e(size)}</td><td><code>{e(digest)}</code></td></tr>")
    summary = " &middot; ".join(
        f"<span style='color:{_SEVERITY_COLORS[s]}'><b>{by_severity[s]}</b> {s}</span>"
        for s in SEVERITY_ORDER if by_severity[s]
    ) or "No differences found."
    review = " &middot; ".join(f"<b>{count}</b> {e(status)}" for status, count in sorted(by_status.items()))
    warnings = "".join(f"<li>{e(w)}</li>" for w in data.warnings)
    body_rows = []
    for row in rows:
        color = _SEVERITY_COLORS.get(row[1], "#000")
        cells = [e(c) for c in row]
        cells[1] = f"<span style='color:{color};font-weight:600'>{cells[1]}</span>"
        body_rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    rematch = ""
    if data.rematch_log:
        entries = "".join(
            "<tr>" + "".join(f"<td>{e(str(entry.get(k, '')))}</td>" for k in
                             ("at", "reviewer", "outcome", "approved_text", "suspect_text", "reason")) + "</tr>"
            for entry in data.rematch_log
        )
        rematch = (
            "<h2>Rematch log</h2><p>Rows the reviewer re-compared on their own. A row "
            "<b>removed</b> here is no longer in the table above.</p>"
            "<table><tr><th>When (UTC)</th><th>Reviewer</th><th>Outcome</th><th>Original text</th>"
            f"<th>Suspect text</th><th>Reason</th></tr>{entries}</table>"
        )
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Comparison report</title>
<style>
 body {{ font: 13px/1.45 'Segoe UI', Arial, sans-serif; color: #1f2328; background: #fff; margin: 24px; }}
 h1 {{ font-size: 20px; margin: 0 0 4px; }} h2 {{ font-size: 15px; margin: 22px 0 6px; }}
 table {{ border-collapse: collapse; width: 100%; margin-top: 6px; }}
 th, td {{ border: 1px solid #d0d7de; padding: 4px 6px; text-align: left; vertical-align: top; }}
 th {{ background: #f6f8fa; }} code {{ font-size: 11px; word-break: break-all; }}
 .warn {{ background: #fff1f0; border: 1px solid #cf222e; padding: 8px 12px; color: #82071e; }}
 .muted {{ color: #656d76; }}
 td:nth-child(6), td:nth-child(7) {{ white-space: pre-wrap; max-width: 420px; }}
 @media print {{ body {{ margin: 0; }} tr {{ page-break-inside: avoid; }} }}
</style></head><body>
<h1>Document comparison report</h1>
<div class="muted">Generated {e(generated)} by {e(data.reviewer or 'unknown')} &middot; DocForensics Analyzer {e(data.app_version)}</div>
<h2>Documents</h2>
<table><tr><th></th><th>File</th><th>Size</th><th>SHA-256</th></tr>{''.join(files)}</table>
{f'<h2>What could not be compared</h2><ul class="warn">{warnings}</ul>' if warnings else ''}
<h2>Summary</h2>
<p>{len(data.items)} difference{'s' if len(data.items) != 1 else ''}: {summary}<br>Review: {review or 'nothing reviewed'}</p>
<h2>Differences</h2>
<table><tr>{''.join(f'<th>{e(c)}</th>' for c in _COLUMNS)}</tr>{''.join(body_rows)}</table>
{rematch}
</body></html>
"""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(page)
