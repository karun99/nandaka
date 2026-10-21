"""Output writers: csv/xlsx/pdf/md/txt write + structural validation (FR-OUT-03)."""

import csv
import zipfile

from nandaka.output.pdfwriter import markdown_to_pdf
from nandaka.output.writers import (validate_output, write_csv, write_md,
                                    write_output, write_pdf, write_txt,
                                    write_xlsx)

SAMPLE_MD = """# Chapter One

Intro paragraph with **bold** and `code`.

## Concepts

- point one
- point two

1. numbered item

| a | b |
| --- | --- |
| 1 | 2 |
"""


def test_write_md_and_validate(tmp_path):
    info = write_md(tmp_path / "r.md", SAMPLE_MD)
    v = validate_output(info["path"], "md")
    assert v["ok"] and "headings=" in v["detail"]


def test_write_txt_strips_markup(tmp_path):
    info = write_txt(tmp_path / "r.txt", SAMPLE_MD)
    v = validate_output(info["path"], "txt")
    assert v["ok"]
    text = (tmp_path / "r.txt").read_text(encoding="utf-8")
    assert "**" not in text and "# " not in text.splitlines()[0]


def test_write_csv_streaming(tmp_path):
    rows = [{"id": "a", "val": 1}, {"id": "b", "val": 2}]
    info = write_csv(tmp_path / "r.csv", rows)
    assert info["rows"] == 2 and info["cols"] == 2
    v = validate_output(info["path"], "csv")
    assert v["ok"] and "2 data rows" in v["detail"]
    with open(info["path"], encoding="utf-8") as fh:
        back = list(csv.DictReader(fh))
    assert back[0]["id"] == "a"


def test_write_csv_handles_nested_and_unicode(tmp_path):
    rows = [{"id": "x", "note": ["ఒకటి", "రెండు"], "meta": {"k": 1}},
            {"id": "y"}]
    info = write_csv(tmp_path / "u.csv", rows, fieldnames=["id", "note", "meta"])
    text = open(info["path"], encoding="utf-8").read()
    assert "ఒకటి" in text and "one" not in text.split("\n")[1]
    assert validate_output(info["path"], "csv")["ok"]


def test_write_xlsx_multipsheet_roundtrip(tmp_path):
    sheets = {
        "MCQs": [["id", "q"], ["m1", "ప్రశ్న?"], ["m2", "q2"]],
        "Checks": [["id", "ok"], ["m1", True]],
    }
    info = write_xlsx(tmp_path / "wb.xlsx", sheets)
    assert info["sheets"] == 2
    v = validate_output(info["path"], "xlsx")
    assert v["ok"] and "2 sheet" in v["detail"]
    with zipfile.ZipFile(info["path"]) as zf:
        assert zf.testzip() is None
        sheet1 = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "ప్రశ్న?" in sheet1 and "xml:space" in sheet1   # unicode preserved
        assert zf.read("xl/workbook.xml").decode().count("<sheet ") == 2


def test_xlsx_header_count_matches_rows(tmp_path):
    rows = [["a", "b"], [1, 2], [3, 4]]
    info = write_xlsx(tmp_path / "t.xlsx", {"S": rows})
    assert info["rows"] == 3
    with zipfile.ZipFile(info["path"]) as zf:
        xml = zf.read("xl/worksheets/sheet1.xml")
    assert xml.count(b"<row ") == 3


def test_pdf_valid_structure(tmp_path):
    info = write_pdf(tmp_path / "doc.pdf", SAMPLE_MD)
    v = validate_output(info["path"], "pdf")
    assert v["ok"], v
    data = open(info["path"], "rb").read()
    assert data.startswith(b"%PDF-1.4") and b"%%EOF" in data[-1024:]
    assert b"/Type /Catalog" in data and b"/Type /Pages" in data
    assert info["pages"] >= 1


def test_pdf_telugu_without_font_is_disclosed(tmp_path, monkeypatch):
    """No silent data loss: uncovered non-Latin text must be disclosed.

    Hermetic — the font search is stubbed empty so this asserts the contract
    regardless of which fonts the host happens to have installed.
    """
    import nandaka.output.pdfwriter as pw
    monkeypatch.setattr(pw, "_font_search_paths", lambda: [])
    monkeypatch.delenv("NANDAKA_FONT", raising=False)

    info = markdown_to_pdf("# అధ్యాయం\n\nతెలుగు వాక్యం ఇక్కడ ఉంది.",
                           tmp_path / "te.pdf")
    assert info["path"]
    assert info["font"] == "Helvetica (built-in)"   # nothing embedded
    assert any("no covering TTF" in w for w in info["warnings"]), info["warnings"]
    # the file is still produced and structurally valid — degraded, not broken
    v = validate_output(info["path"], "pdf")
    assert v["ok"], v


def test_pdf_latin_only_never_warns_about_fonts(tmp_path, monkeypatch):
    """The disclosure fires only when it is true (no false alarms)."""
    import nandaka.output.pdfwriter as pw
    monkeypatch.setattr(pw, "_font_search_paths", lambda: [])
    monkeypatch.delenv("NANDAKA_FONT", raising=False)

    info = markdown_to_pdf("# Chapter One\n\nLatin only text here.",
                           tmp_path / "en.pdf")
    assert info["font"] == "Helvetica (built-in)"
    assert not any("covering TTF" in w for w in info["warnings"]), info["warnings"]


def test_write_output_dispatch(tmp_path):
    assert write_output("md", tmp_path / "a.md", "hi")["format"] == "md"
    assert write_output("csv", tmp_path / "a.csv", [{"x": 1}])["format"] == "csv"
    assert write_output("xlsx", tmp_path / "a.xlsx", {"S": [["h"], [1]]})["format"] == "xlsx"
    try:
        write_output("docx", tmp_path / "a.docx", "x")
        assert False, "should reject unknown format"
    except ValueError as exc:
        assert "unsupported" in str(exc)


def test_missing_file_validation(tmp_path):
    v = validate_output(tmp_path / "ghost.md", "md")
    assert v["ok"] is False and "missing" in v["detail"]


def test_metrics_helpers():
    from nandaka.output.metrics import (BASELINE_MINUTES, RunTimer,
                                        automation_coverage, quality_metrics,
                                        time_saved_pct)
    assert BASELINE_MINUTES == 120
    assert time_saved_pct(60) == 50.0
    assert time_saved_pct(0) == 100.0
    assert automation_coverage(8, 10) == 80.0
    assert automation_coverage(5, 0) == 0.0
    q = quality_metrics(items_total=10, factual_errors=1, images_total=4,
                        images_cleared=3, topics_total=5,
                        images_with_coverage=4)
    assert q["factual_error_rate_pct"] == 10.0
    assert q["image_licence_clearance_rate_pct"] == 75.0
    assert q["visual_coverage_rate_pct"] == 80.0
    t = RunTimer()
    t.start("drafting")
    t.stop("drafting")
    assert "drafting" in t.as_minutes()
