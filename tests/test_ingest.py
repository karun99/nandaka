"""Ingestion tests (FR-IN-01..06): all supported formats, gap reporting."""

from pathlib import Path

import pytest

from nandaka.ingest import ingest, ingest_path, inventory

TEXTS = ("md", "txt")


def _files(tmp_path):
    (tmp_path / "notes.md").write_text("# Notes\nSome study notes.", encoding="utf-8")
    (tmp_path / "plain.txt").write_text("plain text source", encoding="utf-8")
    (tmp_path / "data.csv").write_text("q,topic\n1,Polity\n2,Economy\n", encoding="utf-8")
    # xlsx via our own writer (round-trip through stdlib zip reader)
    from nandaka.output.writers import write_xlsx
    write_xlsx(tmp_path / "book.xlsx", {"Sheet1": [["a", "b"], [1, 2]]})
    from nandaka.images import encode_png
    row = bytes([1, 2, 3, 255] * 8)
    (tmp_path / "fig.png").write_bytes(encode_png(8, 8, [row] * 8))
    (tmp_path / "diag.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>',
                                       encoding="utf-8")
    return tmp_path


def test_accepts_every_declared_format(tmp_path):
    """TC-07: one file per format; all accepted with provenance."""
    d = _files(tmp_path)
    docs = ingest([d / "notes.md", d / "plain.txt", d / "data.csv",
                   d / "book.xlsx", d / "fig.png", d / "diag.svg"])
    kinds = {x.kind for x in docs}
    assert kinds == {"text", "csv", "xlsx", "image"}
    assert not any(x.notes for x in docs if x.kind != "image")


def test_csv_streaming_summary(tmp_path):
    d = _files(tmp_path)
    doc = ingest_path(d / "data.csv")
    assert doc.tables == 1
    assert doc.provenance["rows"] == 3
    assert "Polity" in doc.text()


def test_xlsx_roundtrip_ingest(tmp_path):
    d = _files(tmp_path)
    doc = ingest_path(d / "book.xlsx")
    assert doc.tables == 1
    assert doc.provenance["cells"] == 4
    assert "a | b" in doc.text()


def test_missing_input_reported(tmp_path):
    doc = ingest_path(tmp_path / "ghost.pdf")
    assert doc.kind == "missing"
    assert any("not found" in n for n in doc.notes)


def test_unsupported_format_stated(tmp_path):
    bad = tmp_path / "weird.docx"
    bad.write_bytes(b"PK\x03\x04")
    doc = ingest_path(bad)
    assert doc.kind == "unknown"
    assert any("unsupported" in n for n in doc.notes)


def test_pdf_page_by_page(tmp_path):
    """Round-trip: our PDF writer -> pypdf page extraction (NFR-D-04)."""
    pytest.importorskip("pypdf")
    from nandaka.output.writers import write_pdf
    info = write_pdf(tmp_path / "src.pdf",
                     "# Page one\nFirst page content line.\n\n## Section\nMore text.")
    doc = ingest_path(info["path"])
    assert doc.kind == "pdf"
    assert doc.pages >= 1
    assert "First page content line." in doc.text()


def test_pdf_without_pypdf_degrades(tmp_path, monkeypatch):
    """AD-03: missing optional dep is a disclosed gap, not a crash."""
    import nandaka.ingest as ing
    from nandaka.output.writers import write_pdf
    info = write_pdf(tmp_path / "s.pdf", "# T\nbody")
    real_import = __import__

    def fake_import(name, *a, **k):
        if name == "pypdf":
            raise ImportError("no pypdf")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", fake_import)
    doc = ing.ingest_path(info["path"])
    assert doc.kind == "pdf"
    assert any("nandaka[pdf]" in n for n in doc.notes)


def test_inventory_lists_gaps(tmp_path):
    d = _files(tmp_path)
    docs = ingest([d / "notes.md", tmp_path / "ghost.txt"])
    inv = inventory(docs)
    assert inv["total"] == 2
    assert any("not found" in g for g in inv["gaps"])


def test_chunked_page_iteration(tmp_path):
    (tmp_path / "big.txt").write_text("word " * 5000, encoding="utf-8")
    doc = ingest_path(tmp_path / "big.txt")
    chunks = list(doc.iter_pages(size=1000))
    assert len(chunks) > 10
    assert sum(len(c) for c in chunks) == doc.chars
