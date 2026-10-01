"""PDF and Document Merging utility (core/pdf_merge.py).

Provides utilities to merge multiple split documents (PDFs, DOCX) into
a single continuous PDF document for comparison.

This supports use cases where a suspect (or approved) document has been
split across multiple files (e.g. part 1 and part 2, or individual
chapters/sections), while the counterpart document is a single unified file.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
from typing import Any

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)

SUPPORTED_MERGE_EXTENSIONS = (".pdf", ".docx")


def natural_sort_key(s: str) -> list[int | str]:
    """Key for natural human sorting (e.g., 'part1', 'part2', 'part10')."""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", s)]


def merge_cache_dir() -> str:
    """Directory where merged documents are cached."""
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    path = os.path.join(base, "DocForensicsAnalyzer", "merged")
    os.makedirs(path, exist_ok=True)
    return path


def format_file_size(size_bytes: int) -> str:
    """Format bytes into human-readable size."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


def inspect_document(path: str) -> dict[str, Any]:
    """Inspect a document to return its page count, size, and validity."""
    if not os.path.exists(path):
        return {
            "path": path,
            "name": os.path.basename(path),
            "page_count": 0,
            "size_bytes": 0,
            "size_formatted": "0 B",
            "is_valid": False,
            "error": "File not found",
        }

    stat = os.stat(path)
    size_bytes = stat.st_size
    ext = os.path.splitext(path)[1].lower()

    if ext not in SUPPORTED_MERGE_EXTENSIONS:
        return {
            "path": path,
            "name": os.path.basename(path),
            "page_count": 0,
            "size_bytes": size_bytes,
            "size_formatted": format_file_size(size_bytes),
            "is_valid": False,
            "error": f"Unsupported format '{ext}'. Must be PDF or DOCX.",
        }

    try:
        if ext == ".docx":
            from core.office_convert import convert_to_pdf
            pdf_path = convert_to_pdf(path)
            doc = fitz.open(pdf_path)
        else:
            doc = fitz.open(path)

        page_count = doc.page_count
        doc.close()

        return {
            "path": path,
            "name": os.path.basename(path),
            "page_count": page_count,
            "size_bytes": size_bytes,
            "size_formatted": format_file_size(size_bytes),
            "is_valid": page_count > 0,
            "error": None if page_count > 0 else "Document has 0 pages",
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to inspect %s: %s", path, exc)
        return {
            "path": path,
            "name": os.path.basename(path),
            "page_count": 0,
            "size_bytes": size_bytes,
            "size_formatted": format_file_size(size_bytes),
            "is_valid": False,
            "error": str(exc),
        }


def default_merged_output_path(file_paths: list[str], prefix: str = "merged") -> str:
    """Generate a stable, unique cached output path for a list of input files."""
    components = []
    for p in file_paths:
        try:
            stat = os.stat(p)
            components.append(f"{os.path.normcase(os.path.abspath(p))}|{stat.st_mtime_ns}|{stat.st_size}")
        except OSError:
            components.append(os.path.normcase(os.path.abspath(p)))

    ident = "\n".join(components)
    digest = hashlib.sha1(ident.encode("utf-8")).hexdigest()[:16]

    # Meaningful stem from the first file name
    first_stem = "doc"
    if file_paths:
        first_stem = os.path.splitext(os.path.basename(file_paths[0]))[0][:30]
        # Clean special chars
        first_stem = re.sub(r"[^\w\-]", "_", first_stem)

    filename = f"{prefix}_{first_stem}_{len(file_paths)}files_{digest}.pdf"
    return os.path.join(merge_cache_dir(), filename)


def merge_pdf_documents(
    file_paths: list[str],
    output_path: str | None = None,
    prefix: str = "merged_suspect",
) -> tuple[str, list[dict[str, Any]]]:
    """Merge multiple documents (PDFs, DOCX) into a single continuous PDF file.

    Args:
        file_paths: Ordered list of document paths to merge.
        output_path: Destination path for the merged PDF. If None,
            a deterministic cached path in the user's app data is used.
        prefix: Prefix for generated file name when output_path is None.

    Returns:
        (merged_pdf_path, list_of_file_info_dicts)
        Each dict in the list contains:
          - path: str
          - name: str
          - page_count: int
          - start_page: int (1-indexed page in merged doc)
          - end_page: int (1-indexed page in merged doc)
          - size_bytes: int

    Raises:
        ValueError: If file_paths is empty.
        RuntimeError: If any file fails to convert or merge.
    """
    if not file_paths:
        raise ValueError("Cannot merge an empty list of files.")

    if output_path is None:
        output_path = default_merged_output_path(file_paths, prefix=prefix)

    merged_doc = fitz.open()
    file_infos: list[dict[str, Any]] = []
    current_page = 1

    try:
        for idx, path in enumerate(file_paths):
            if not os.path.exists(path):
                raise FileNotFoundError(f"File not found: '{path}'")

            ext = os.path.splitext(path)[1].lower()
            pdf_path_to_insert = path

            if ext == ".docx":
                from core.office_convert import convert_to_pdf
                pdf_path_to_insert = convert_to_pdf(path)
            elif ext != ".pdf":
                raise ValueError(f"Unsupported file format '{ext}' for '{os.path.basename(path)}'. Only PDF and DOCX are supported.")

            sub_doc = fitz.open(pdf_path_to_insert)
            count = sub_doc.page_count
            if count == 0:
                sub_doc.close()
                raise RuntimeError(f"'{os.path.basename(path)}' contains 0 pages.")

            # insert_pdf copies all pages from sub_doc into merged_doc
            merged_doc.insert_pdf(sub_doc)
            sub_doc.close()

            stat = os.stat(path)
            file_infos.append({
                "path": path,
                "name": os.path.basename(path),
                "page_count": count,
                "start_page": current_page,
                "end_page": current_page + count - 1,
                "size_bytes": stat.st_size,
                "size_formatted": format_file_size(stat.st_size),
            })
            current_page += count

        # Save merged PDF with deflation for compact size
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        merged_doc.save(output_path, deflate=True)
    except Exception as exc:
        logger.exception("Failed to merge documents: %s", exc)
        raise RuntimeError(f"Failed to merge documents: {exc}") from exc
    finally:
        merged_doc.close()

    logger.info("Successfully merged %d files into '%s' (%d total pages)", len(file_paths), output_path, current_page - 1)
    return output_path, file_infos
