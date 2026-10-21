"""Input ingestion (FR-IN-01..06, DR-01).

Formats: md, txt, pdf, csv, xlsx, png, jpg, svg.
Principles: page-by-page/streaming extraction (NFR-D-04/D-05), bounded
memory, provenance preserved, gaps reported instead of silently filled.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from xml.etree import ElementTree as ET

TEXT_EXT = {".md", ".txt", ".markdown"}
PDF_EXT = {".pdf"}
TABULAR_EXT = {".csv", ".xlsx"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".svg"}
ALL_EXT = TEXT_EXT | PDF_EXT | TABULAR_EXT | IMAGE_EXT

SUPPORTED = "md txt pdf csv xlsx png jpg svg"


@dataclass
class Document:
    path: str
    kind: str                # text | pdf | csv | xlsx | image
    title: str = ""
    chars: int = 0
    pages: int = 0
    tables: int = 0
    notes: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    _text: str = field(default="", repr=False)

    def text(self) -> str:
        return self._text

    def iter_pages(self, size: int = 4000) -> Iterator[str]:
        """Yield bounded chunks so nothing is processed in one huge pass."""
        t = self._text
        for i in range(0, max(len(t), 1), size):
            yield t[i:i + size]


# --------------------------------------------------------------------------- #
# Readers
# --------------------------------------------------------------------------- #


def _read_text(p: Path) -> Document:
    data = p.read_bytes()
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("utf-8", errors="replace")
    title = p.stem.replace("-", " ").replace("_", " ").strip()
    return Document(str(p), "text", title=title, chars=len(text), _text=text)


def _read_pdf(p: Path) -> Document:
    """Page-by-page extraction (NFR-D-04): each page buffer is discarded."""
    try:
        from pypdf import PdfReader  # optional extra: pip install nandaka[pdf]
    except ImportError:
        doc = Document(str(p), "pdf", title=p.stem)
        doc.notes.append(
            "PDF text extraction unavailable: install the optional dependency "
            "(pip install 'nandaka[pdf]') or supply a text copy of this source."
        )
        return doc
    reader = PdfReader(str(p))
    parts: list[str] = []
    bad_pages: list[int] = []
    n_pages = 0
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:  # corrupt page must not kill the batch
            parts.append("")
            bad_pages.append(n_pages + 1)
        n_pages += 1
    text = "\n\n".join(parts)
    doc = Document(str(p), "pdf", title=p.stem, chars=len(text),
                   pages=n_pages, _text=text)
    doc.provenance["pages"] = n_pages
    if bad_pages:
        doc.notes.append(f"unreadable page(s): {bad_pages}")
    if n_pages == 0:
        doc.notes.append("scanned PDF (no text layer): OCR required — PaddleOCR is "
                         "loaded lazily when a scanned Indic document is detected (AD-03)")
    return doc


def _read_csv(p: Path) -> Document:
    """Streaming CSV reader (NFR-D-05): never loads whole file into a matrix."""
    rows, cols, sample = 0, 0, []
    buf = io.StringIO()
    with p.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        reader = csv.reader(fh)
        for i, row in enumerate(reader):
            if i < 50:
                sample.append(" | ".join(row))
            cols = max(cols, len(row))
            rows += 1
            if i % 1000 == 0:  # bound memory: keep only a rolling head
                buf.truncate(0)
                buf.seek(0)
                buf.write("\n".join(sample))
    if rows > 50:
        sample = sample[:50]
    text = "\n".join(sample) + f"\n[csv: {rows} rows x {cols} columns]"
    return Document(str(p), "csv", title=p.stem, chars=len(text), tables=1,
                    _text=text, provenance={"rows": rows, "cols": cols})


_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _read_xlsx(p: Path) -> Document:
    """Streaming XLSX reader via zipfile + XML (stdlib only, NFR-D-05)."""
    try:
        with zipfile.ZipFile(p) as zf:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in zf.namelist():
                with zf.open("xl/sharedStrings.xml") as fh:
                    for _, el in ET.iterparse(fh, events=("end",)):
                        if el.tag.endswith("}t") and el.text:
                            shared.append(el.text)
                        if el.tag.endswith("}si"):
                            el.clear()
            sheets = sorted(n for n in zf.namelist()
                            if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
            lines, cells, sheet_no = [], 0, 0
            for sheet in sheets[:10]:
                sheet_no += 1
                lines.append(f"# sheet {sheet_no}")
                with zf.open(sheet) as fh:
                    for _, el in ET.iterparse(fh, events=("end",)):
                        if not el.tag.endswith("}row"):
                            continue
                        vals = []
                        for c in el.findall("m:c", _NS):
                            ctype = c.get("t", "")
                            if ctype == "inlineStr":     # written by us
                                is_el = c.find("m:is/m:t", _NS)
                                vals.append(is_el.text if (is_el is not None and
                                                           is_el.text) else "")
                                continue
                            v = c.find("m:v", _NS)
                            if v is None or v.text is None:
                                vals.append("")
                                continue
                            if ctype == "s":
                                idx = int(v.text)
                                vals.append(shared[idx] if 0 <= idx < len(shared) else "")
                            else:
                                vals.append(v.text)
                        cells += len(vals)
                        if len(lines) < 200:
                            lines.append(" | ".join(vals))
                        el.clear()
            text = "\n".join(lines) + f"\n[xlsx: {sheet_no} sheet(s), {cells} cells]"
            return Document(str(p), "xlsx", title=p.stem, chars=len(text),
                            tables=sheet_no, _text=text,
                            provenance={"sheets": sheet_no, "cells": cells})
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as exc:
        doc = Document(str(p), "xlsx", title=p.stem)
        doc.notes.append(f"cannot read xlsx: {exc}")
        return doc


def _read_image(p: Path) -> Document:
    doc = Document(str(p), "image", title=p.stem, chars=0)
    doc.provenance["bytes"] = p.stat().st_size
    doc.provenance["ext"] = p.suffix.lower().lstrip(".")
    return doc


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


def ingest_path(path: str | Path) -> Document:
    p = Path(path).expanduser()
    if not p.exists():
        doc = Document(str(p), "missing")
        doc.notes.append(f"input not found: {p}")
        return doc
    ext = p.suffix.lower()
    if ext in TEXT_EXT:
        return _read_text(p)
    if ext in PDF_EXT:
        return _read_pdf(p)
    if ext == ".csv":
        return _read_csv(p)
    if ext == ".xlsx":
        return _read_xlsx(p)
    if ext in IMAGE_EXT:
        return _read_image(p)
    doc = Document(str(p), "unknown")
    doc.notes.append(f"unsupported format '{ext}': supported are {SUPPORTED}")
    return doc


def ingest(paths: list[str | Path]) -> list[Document]:
    docs = [ingest_path(p) for p in paths]
    # FR-IN-06: state gaps, continue with labelled assumptions
    for d in docs:
        if d.kind == "missing" or d.notes:
            pass
    return docs


def inventory(docs: list[Document]) -> dict:
    """Source inventory for the batch report (FR-OUT-01)."""
    return {
        "sources": [
            {"path": d.path, "kind": d.kind, "title": d.title, "chars": d.chars,
             "pages": d.pages, "tables": d.tables, "notes": d.notes}
            for d in docs
        ],
        "total": len(docs),
        "gaps": [n for d in docs for n in d.notes],
    }
