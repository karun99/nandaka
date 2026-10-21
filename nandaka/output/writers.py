"""Output production (FR-OUT-02, FR-OUT-03, DR-01/02).

Emit md, txt, pdf, csv, xlsx (+ svg/png/jpg handled by nandaka.images).
Every writer is stdlib-only; every file is re-opened and structurally
validated after writing (FR-OUT-03).
"""

from __future__ import annotations

import csv
import html
import json
import re
import zipfile
from pathlib import Path
from typing import Any, Iterable, Sequence

from .pdfwriter import markdown_to_pdf

WRITEABLE = ("md", "txt", "pdf", "csv", "xlsx")


# --------------------------------------------------------------------------- #
# Plain text formats
# --------------------------------------------------------------------------- #


def write_md(path: str | Path, text: str) -> dict[str, Any]:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return {"path": str(p), "format": "md", "bytes": p.stat().st_size,
            "chars": len(text)}


def write_txt(path: str | Path, text: str) -> dict[str, Any]:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_strip_md(text), encoding="utf-8")
    return {"path": str(p), "format": "txt", "bytes": p.stat().st_size,
            "chars": len(p.read_text(encoding="utf-8"))}


def _strip_md(text: str) -> str:
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.M)
    text = re.sub(r"\*\*|\_\_", "", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    return text


def write_csv(path: str | Path, rows: Sequence[dict[str, Any]],
              fieldnames: Sequence[str] | None = None) -> dict[str, Any]:
    """Streaming CSV writer (NFR-D-05): rows are written one at a time."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not rows and not fieldnames:
        fieldnames = ["empty"]
    names = list(fieldnames or dict.fromkeys(k for r in rows for k in r))
    count = 0
    with p.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=names, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: _flat(row.get(k)) for k in names})
            count += 1
    return {"path": str(p), "format": "csv", "rows": count, "cols": len(names)}


def _flat(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return "; ".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return v


# --------------------------------------------------------------------------- #
# Minimal XLSX writer (multi-sheet, stdlib zipfile only)
# --------------------------------------------------------------------------- #

_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _col_letter(idx: int) -> str:
    letters = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _sheet_xml(rows: Iterable[Sequence[Any]]) -> str:
    out = [_XML_DECL,
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
           "<sheetData>"]
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, val in enumerate(row):
            ref = f"{_col_letter(c)}{r}"
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                cells.append(f'<c r="{ref}"><v>{val}</v></c>')
            elif val is None or val == "":
                continue
            else:
                txt = html.escape(str(val), quote=False)
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">'
                             f"{txt}</t></is></c>")
        out.append(f'<row r="{r}">{"".join(cells)}</row>')
    out.append("</sheetData></worksheet>")
    return "".join(out)


def write_xlsx(path: str | Path, sheets: dict[str, Sequence[Sequence[Any]]],
               ) -> dict[str, Any]:
    """Write a workbook: {sheet_name: rows}. Streamed cell-by-cell."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    sheet_items = list(sheets.items()) or [("Sheet1", [["(empty)"]])]

    content_types = [_XML_DECL,
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
                     '<Default Extension="rels" ContentType='
                     '"application/vnd.openxmlformats-package.relationships+xml"/>',
                     '<Default Extension="xml" ContentType="application/xml"/>',
                     '<Override PartName="/xl/workbook.xml" ContentType='
                     '"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>']
    for i in range(1, len(sheet_items) + 1):
        content_types.append(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
    content_types.append("</Types>")

    workbook = [_XML_DECL,
                f'<workbook xmlns="{_NS_R}" '
                'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets>']
    wb_rels = [_XML_DECL,
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    for i, (name, _rows) in enumerate(sheet_items, start=1):
        safe = re.sub(r"[\[\]\*\?/\\:]", "_", str(name))[:31] or f"Sheet{i}"
        workbook.append(f'<sheet name="{html.escape(safe, quote=True)}" sheetId="{i}" '
                        f'r:id="rId{i}"/>')
        wb_rels.append(
            f'<Relationship Id="rId{i}" Type="{_NS_R}/worksheet" '
            f'Target="worksheets/sheet{i}.xml"/>')
    workbook.append("</sheets></workbook>")
    wb_rels.append(f'<Relationship Id="rId{len(sheet_items) + 1}" '
                   f'Type="{_NS_R}/styles" Target="styles.xml"/>')
    wb_rels.append("</Relationships>")

    root_rels = (_XML_DECL +
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 f'<Relationship Id="rId1" Type="{_NS_R}/officeDocument" '
                 'Target="xl/workbook.xml"/></Relationships>')
    styles = (_XML_DECL +
              '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
              '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
              '<borders count="1"><border/></borders>'
              '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
              '<cellXfs count="1"><xf/></cellXfs></styleSheet>')

    rows_written = 0
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "".join(content_types))
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", "".join(workbook))
        zf.writestr("xl/_rels/workbook.xml.rels", "".join(wb_rels))
        zf.writestr("xl/styles.xml", styles)
        for i, (_name, rows) in enumerate(sheet_items, start=1):
            rows = list(rows)
            rows_written += len(rows)
            zf.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(rows))
    return {"path": str(p), "format": "xlsx", "sheets": len(sheet_items),
            "rows": rows_written}


def write_pdf(path: str | Path, text: str) -> dict[str, Any]:
    return markdown_to_pdf(text, path)


# --------------------------------------------------------------------------- #
# Dispatch + validation (FR-OUT-03)
# --------------------------------------------------------------------------- #


def write_output(kind: str, path: str | Path, content: Any) -> dict[str, Any]:
    kind = kind.lower().lstrip(".")
    if kind == "md":
        return write_md(path, str(content))
    if kind == "txt":
        return write_txt(path, str(content))
    if kind == "pdf":
        return write_pdf(path, str(content))
    if kind == "csv":
        rows = content if isinstance(content, (list, tuple)) else [content]
        return write_csv(path, rows)
    if kind == "xlsx":
        sheets = content if isinstance(content, dict) else {"Sheet1": content}
        return write_xlsx(path, sheets)
    raise ValueError(f"unsupported output format '{kind}' (supported: {WRITEABLE})")


def validate_output(path: str | Path, fmt: str) -> dict[str, Any]:
    """Re-open the file and confirm structural integrity (FR-OUT-03)."""
    p = Path(path)
    result: dict[str, Any] = {"path": str(p), "format": fmt, "ok": False,
                              "detail": "", "reopened": True}
    if not p.exists():
        result["detail"] = "file missing after write"
        return result
    try:
        if fmt == "md":
            text = p.read_text(encoding="utf-8")
            result.update(ok=bool(text.strip()),
                          detail=f"{len(text)} chars, headings={len(re.findall(r'^#', text, re.M))}")
        elif fmt == "txt":
            text = p.read_text(encoding="utf-8")
            result.update(ok=True, detail=f"{len(text)} chars")
        elif fmt == "csv":
            with p.open("r", encoding="utf-8", newline="") as fh:
                reader = csv.reader(fh)
                header = next(reader, [])
                n = sum(1 for _ in reader)
            result.update(ok=len(header) > 0, detail=f"{n} data rows x {len(header)} cols")
        elif fmt == "xlsx":
            with zipfile.ZipFile(p) as zf:
                bad = zf.testzip()
                names = zf.namelist()
                sheet_names = [n for n in names if n.startswith("xl/worksheets/sheet")]
                rows = sum(zf.read(s).count(b"<row ") for s in sheet_names)
            result.update(ok=bad is None and bool(sheet_names),
                          detail=f"{len(sheet_names)} sheet(s), {rows} rows, zip ok={bad is None}")
        elif fmt == "pdf":
            data = p.read_bytes()
            head_ok = data[:5] == b"%PDF-"
            tail_ok = b"%%EOF" in data[-1024:]
            result.update(ok=head_ok and tail_ok,
                          detail=f"{len(data)} bytes, header={head_ok}, eof={tail_ok}")
        else:  # images
            data = p.read_bytes()
            if fmt == "png":
                ok = data[:8] == b"\x89PNG\r\n\x1a\n"
            elif fmt == "svg":
                ok = data.lstrip()[:5] == b"<svg " or b"<svg" in data[:400]
            else:
                ok = len(data) > 0
            result.update(ok=ok, detail=f"{len(data)} bytes")
    except Exception as exc:  # FR-OUT-03: any failure must surface
        result["detail"] = f"validation error: {type(exc).__name__}: {exc}"
    return result
