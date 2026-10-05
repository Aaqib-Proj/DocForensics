"""Keeping a review (core/review_store.py).

A review -- which differences were accepted or rejected, by whom and
when, which were looked at, which were removed by a rematch -- used to
live only in memory and vanished with the window. This module keeps it
on disk, keyed by the CONTENT of the two documents (SHA-256 of each
file), so:

  * comparing the same two files again brings the review back, even
    after a restart or from a different folder;
  * a changed file is a different comparison -- an old decision can
    never silently carry over to a document that has since been edited.

Decisions are matched to differences by a fingerprint of what the
difference IS (kind, both texts, pages) plus its occurrence number, not
by its position in the list, so a re-run that orders rows differently
still finds them. No Qt: standalone-testable.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from typing import Any, Optional

from core.diff_engine import DiffItem, Location

_CHUNK = 1 << 20


def file_digest(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def comparison_key(approved_path: str, suspect_path: str) -> str:
    return f"{file_digest(approved_path)}_{file_digest(suspect_path)}"


def _first_page(location: Optional[Location], caret: Optional[Location]) -> int:
    where = location or caret
    return where.page if where is not None else 0


def fingerprints(items: list[DiffItem]) -> list[str]:
    """One stable identity per difference: what it is and where, plus
    an occurrence number so identical rows stay distinct."""
    seen: Counter = Counter()
    out = []
    for item in items:
        base = "\x1f".join(
            (
                item.kind,
                item.approved_text,
                item.suspect_text,
                str(_first_page(item.approved_location, item.approved_caret)),
                str(_first_page(item.suspect_location, item.suspect_caret)),
            )
        )
        seen[base] += 1
        out.append(f"{base}\x1f{seen[base]}")
    return out


class ReviewStore:
    """Reviews on disk, one JSON file per pair of documents."""

    def __init__(self, directory: str) -> None:
        self.directory = directory

    def _path(self, key: str) -> str:
        return os.path.join(self.directory, f"review_{key}.json")

    def load(self, key: str) -> dict[str, Any]:
        try:
            with open(self._path(key), encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, key: str, state: dict[str, Any]) -> None:
        os.makedirs(self.directory, exist_ok=True)
        target = self._path(key)
        temporary = target + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=1)
        os.replace(temporary, target)  # never leave a half-written review
