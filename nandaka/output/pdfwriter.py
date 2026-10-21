"""Minimal dependency-free PDF writer with mixed-font text (FR-OUT, IR/CO-10).

Strategy (keeps Termux installs compiler-free while producing real Telugu):

  * Latin runs render with the built-in Helvetica standard font (WinAnsi) —
    tiny files, no embedding, universal viewer support.
  * Non-Latin runs (Telugu/Indic) render with an embedded system TTF found at
    runtime (NANDAKA_FONT, bundled assets, then system font dirs) as a
    CIDFontType2/Identity-H font with a ToUnicode map, so text extracts.
  * If a document needs a script no available font covers, that is DISCLOSED
    in the returned warnings and substituted — never silently lost.

Scope: text documents (headings, paragraphs, bullets, tables-as-lines).
Markdown remains the source of truth for layout.
"""

from __future__ import annotations

import os
import re
import struct
import zlib
from pathlib import Path
from typing import Any

A4 = (595.28, 841.89)
MARGIN = 54.0

# --------------------------------------------------------------------------- #
# Font discovery + TTF parsing
# --------------------------------------------------------------------------- #


def _font_search_paths() -> list[Path]:
    out: list[Path] = []
    env = os.environ.get("NANDAKA_FONT")
    if env and Path(env).exists():
        out.append(Path(env))
    here = Path(__file__).resolve().parent.parent / "assets" / "fonts"
    if here.exists():
        out.extend(sorted(here.glob("*.ttf")))
    prefixes = [Path("/usr/share/fonts"), Path("/system/fonts")]
    prefix = os.environ.get("PREFIX")
    if prefix:
        prefixes.append(Path(prefix) / "share" / "fonts")
    prefixes.append(Path.home() / "fonts")
    for base in prefixes:
        if base.exists():
            try:
                out.extend(sorted(base.rglob("*.ttf")))
            except OSError:
                pass
    return out


def _prefer_key(p: Path) -> tuple[int, str]:
    name = p.name.lower()
    for i, token in enumerate(("telugu", "notosans", "noto", "dejavu", "liberation")):
        if token in name:
            return (i, str(p))
    return (9, str(p))


def _needs_unicode(text: str) -> bool:
    for ch in text:
        try:
            ch.encode("cp1252")
        except UnicodeEncodeError:
            return True
    return False


def _distinct_chars(text: str, limit: int = 800) -> set[str]:
    return {c for c in text if not c.isspace()} or set(" ")


class TTFont:
    """Just enough TTF: cmap (0/4/6/12), hmtx widths, head metrics."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        data = self.path.read_bytes()
        self.raw = data
        self.tables: dict[str, tuple[int, int]] = {}
        (num_tables,) = struct.unpack(">H", data[4:6])
        for i in range(num_tables):
            off = 12 + i * 16
            tag = data[off:off + 4].decode("latin-1", errors="replace")
            toff, tlen = struct.unpack(">II", data[off + 8:off + 16])
            self.tables[tag] = (toff, tlen)
        for req in ("cmap", "head", "hhea", "hmtx"):
            if req not in self.tables:
                raise ValueError(f"TTF missing required table: {req}")
        head_off = self.tables["head"][0]
        (self.units_per_em,) = struct.unpack(">H", data[head_off + 18:head_off + 20])
        x_min, y_min, x_max, y_max = struct.unpack(
            ">hhhh", data[head_off + 36:head_off + 44])
        self.bbox = (x_min, y_min, x_max, y_max) or (0, -200, 1000, 800)
        hhea = self.tables["hhea"][0]
        (self.ascender,) = struct.unpack(">h", data[hhea + 4:hhea + 6])
        (self.descender,) = struct.unpack(">h", data[hhea + 6:hhea + 8])
        (self.num_hmetrics,) = struct.unpack(">H", data[hhea + 34:hhea + 36])
        self.cmap: dict[int, int] = {}
        self._load_cmap(data)
        self._width_cache: dict[int, int] = {}

    # ---------------- cmap ---------------- #

    def _load_cmap(self, data: bytes) -> None:
        cmap_off = self.tables["cmap"][0]
        (n_sub,) = struct.unpack(">H", data[cmap_off + 2:cmap_off + 4])
        best: tuple[int, int] | None = None
        for i in range(n_sub):
            rec = cmap_off + 4 + i * 8
            plat, enc, off = struct.unpack(">HHI", data[rec:rec + 8])
            if (plat, enc) == (3, 10):
                pri = 0
            elif (plat, enc) == (0, 4):
                pri = 1
            elif (plat, enc) == (3, 1):
                pri = 2
            elif plat in (0, 3):
                pri = 3
            else:
                continue
            if best is None or pri < best[1]:
                best = (cmap_off + off, pri)
        if best is None:
            raise ValueError("no usable cmap subtable")
        sub, _ = best
        (fmt,) = struct.unpack(">H", data[sub:sub + 2])
        if fmt == 12:
            self._cmap12(data, sub)
        elif fmt == 4:
            self._cmap4(data, sub)
        elif fmt == 6:
            self._cmap6(data, sub)
        elif fmt == 0:
            for code in range(256):
                self.cmap[code] = data[sub + 6 + code]
        else:
            raise ValueError(f"unsupported cmap format {fmt}")

    def _cmap4(self, data: bytes, sub: int) -> None:
        seg_x2 = struct.unpack(">H", data[sub + 6:sub + 8])[0]
        seg = seg_x2 // 2
        ends = struct.unpack(f">{seg}H", data[sub + 14:sub + 14 + seg_x2])
        p = sub + 14 + seg_x2 + 2
        starts = struct.unpack(f">{seg}H", data[p:p + seg_x2])
        p += seg_x2
        deltas = struct.unpack(f">{seg}h", data[p:p + seg_x2])
        p += seg_x2
        range_off_pos = p
        range_offs = struct.unpack(f">{seg}H", data[p:p + seg_x2])
        for i in range(seg):
            for cp in range(starts[i], min(ends[i], 0xFFFF) + 1):
                if range_offs[i] == 0:
                    gid = (cp + deltas[i]) & 0xFFFF
                else:
                    addr = range_off_pos + i * 2 + range_offs[i] + (cp - starts[i]) * 2
                    if addr + 2 > len(data):
                        continue
                    (gid,) = struct.unpack(">H", data[addr:addr + 2])
                    if gid:
                        gid = (gid + deltas[i]) & 0xFFFF
                if gid:
                    self.cmap[cp] = gid

    def _cmap12(self, data: bytes, sub: int) -> None:
        (n_groups,) = struct.unpack(">I", data[sub + 12:sub + 16])
        p = sub + 16
        for _ in range(n_groups):
            start_c, end_c, start_g = struct.unpack(">III", data[p:p + 12])
            p += 12
            if end_c - start_c > 0x10FFFF:
                continue
            for cp in range(start_c, end_c + 1):
                self.cmap[cp] = start_g + (cp - start_c)

    def _cmap6(self, data: bytes, sub: int) -> None:
        first, count = struct.unpack(">HH", data[sub + 6:sub + 10])
        gids = struct.unpack(f">{count}H", data[sub + 10:sub + 10 + count * 2])
        for i, g in enumerate(gids):
            self.cmap[first + i] = g

    # ---------------- metrics ---------------- #

    def glyph(self, ch: str) -> int:
        return self.cmap.get(ord(ch), 0)

    def has(self, ch: str) -> bool:
        return ord(ch) in self.cmap

    def advance(self, gid: int) -> int:
        if gid in self._width_cache:
            return self._width_cache[gid]
        hmtx = self.tables["hmtx"][0]
        idx = min(gid, max(0, self.num_hmetrics - 1))
        (w,) = struct.unpack(">H", self.raw[hmtx + idx * 4:hmtx + idx * 4 + 2])
        self._width_cache[gid] = w
        return w

    def text_width(self, text: str, size: float) -> float:
        total = sum(self.advance(self.glyph(c)) for c in text)
        return total * size / self.units_per_em

    @property
    def postscript_name(self) -> str:
        return re.sub(r"[^A-Za-z0-9_-]", "", self.path.stem)[:40] or "EmbeddedFont"


def pick_unicode_font(text: str) -> TTFont | None:
    """Find a TTF covering every non-Latin1 char in *text*, if one exists.

    Fonts are tried in preference order (NANDAKA_FONT first, then bundled
    assets, then system fonts sorted toward Noto/DejaVu). Modern Android/
    Termux systems usually keep Latin in one file and Telugu in another;
    Latin is handled by the built-in Helvetica path, so only the non-Latin1
    coverage requirement is checked here.
    """
    needed = {c for c in _distinct_chars(text) if not _char_is_latin1(c)}
    if not needed:
        return None
    for path in sorted(_font_search_paths(), key=_prefer_key):
        try:
            font = TTFont(path)
        except (ValueError, struct.error, OSError):
            continue
        if all(font.has(c) for c in needed):
            return font
    return None


def _char_is_latin1(ch: str) -> bool:
    try:
        ch.encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False


# --------------------------------------------------------------------------- #
# Approximate Helvetica widths (for Latin runs and wrapping)
# --------------------------------------------------------------------------- #

_NARROW = set("iljtfrI.,:;'\"|!()[]{}/\\-")
_WIDE = set("mwMW@%")


def helvetica_width(text: str, size: float) -> float:
    w = 0.0
    for c in text:
        if c in _NARROW:
            w += 0.30
        elif c in _WIDE:
            w += 0.85
        elif c.isupper():
            w += 0.68
        elif c.isdigit():
            w += 0.56
        else:
            w += 0.52
    return w * size


def _latin1(text: str) -> str:
    out = []
    for ch in text:
        try:
            ch.encode("cp1252")
            out.append(ch)
        except UnicodeEncodeError:
            if ch in "‘’“”–—…•":
                out.append({"‘": "'", "’": "'", "“": '"', "”": '"',
                            "–": "-", "—": "-", "…": "...", "•": "*"}[ch])
            else:
                out.append("?")
    return "".join(out)


# --------------------------------------------------------------------------- #
# Document model
# --------------------------------------------------------------------------- #


class PDFWriter:
    def __init__(self, text_sample: str = ""):
        # Latin -> built-in Helvetica; other scripts -> embedded TTF if found
        self.u_font: TTFont | None = pick_unicode_font(text_sample)
        self.pages: list[list[tuple]] = []
        self._new_page()
        self.y = A4[1] - MARGIN
        self.warnings: list[str] = []
        self._used_gids: dict[int, int] = {}   # gid -> unicode (ToUnicode)
        self.missing_glyphs: set[str] = set()

    # ---------------- geometry ---------------- #

    def _new_page(self) -> None:
        self.pages.append([])
        self.y = A4[1] - MARGIN

    def _space(self, h: float) -> None:
        if self.y - h < MARGIN:
            self._new_page()

    def measure(self, text: str, size: float) -> float:
        """Mixed-font width: Helvetica for Latin-1, embedded font elsewhere."""
        total = 0.0
        for run, is_latin in self._runs(text):
            if is_latin:
                total += helvetica_width(run, size)
            elif self.u_font:
                total += self.u_font.text_width(run, size)
            else:
                total += helvetica_width(run, size)   # placeholder widths
        return total

    @staticmethod
    def _runs(text: str) -> list[tuple[str, bool]]:
        """Split into (run, is_latin1) pairs."""
        out: list[tuple[str, bool]] = []
        cur, cur_latin = "", None
        for ch in text:
            latin = _char_is_latin1(ch) or ch in "\t"
            if cur_latin is None or latin == cur_latin:
                cur += ch
                cur_latin = latin
            else:
                out.append((cur, cur_latin))
                cur, cur_latin = ch, latin
        if cur:
            out.append((cur, bool(cur_latin)))
        return out or [("", True)]

    # ---------------- public API ---------------- #

    def heading(self, text: str, level: int = 1) -> None:
        size = {1: 20, 2: 15, 3: 12.5}.get(level, 11)
        self.y -= size * 0.6
        self._wrap(text, size, bold=True, leading=size * 1.3)
        self.y -= 6

    def paragraph(self, text: str, size: float = 10.5, indent: float = 0.0) -> None:
        self._wrap(text, size, leading=size * 1.42, indent=indent)
        self.y -= 7

    def bullet(self, text: str, size: float = 10.5) -> None:
        self._wrap("• " + text, size, leading=size * 1.42, indent=10.0)
        self.y -= 2

    def kv(self, key: str, value: Any, size: float = 10.0) -> None:
        self._wrap(f"{key}: {value}", size, leading=size * 1.4)
        self.y -= 1

    def spacer(self, h: float = 8.0) -> None:
        self.y -= h

    def line(self) -> None:
        self.pages[-1].append(("line", MARGIN, self.y, A4[0] - MARGIN, self.y))
        self.y -= 8

    # ---------------- wrapping ---------------- #

    def _wrap(self, text: str, size: float, *, bold: bool = False,
              leading: float | None = None, indent: float = 0.0) -> None:
        leading = leading or size * 1.4
        avail = A4[0] - 2 * MARGIN - indent
        for raw_line in str(text).splitlines() or [""]:
            words = raw_line.split() or [""]
            line = ""
            for word in words:
                trial = f"{line} {word}".strip()
                if self.measure(trial, size) <= avail or not line:
                    line = trial
                else:
                    self._emit_line(line, size, bold, indent, leading)
                    line = word
            self._emit_line(line, size, bold, indent, leading)

    def _emit_line(self, line: str, size: float, bold: bool,
                   indent: float, leading: float) -> None:
        self._space(leading)
        self.pages[-1].append(("text", MARGIN + indent, self.y, line, size, bold))
        self.y -= leading

    # ---------------- glyph bookkeeping ---------------- #

    def _unicode_glyphs(self, text: str) -> bytes:
        out = bytearray()
        for ch in text:
            gid = self.u_font.glyph(ch) if self.u_font else 0
            if gid == 0 and ch not in ("\n", "\r", "\t", " "):
                self.missing_glyphs.add(ch)
            self._used_gids.setdefault(gid, ord(ch))
            out += struct.pack(">H", gid)
        return bytes(out)

    # ---------------- rendering ---------------- #

    def _page_stream(self, ops: list[tuple], font_res: str) -> bytes:
        parts: list[bytes] = []
        for op in ops:
            if op[0] == "line":
                _, x1, y1, x2, y2 = op
                parts.append(f"0.6 w {x1:.1f} {y1:.1f} m {x2:.1f} {y2:.1f} l S"
                             .encode())
                continue
            _, x, y, text, size, bold = op
            parts.append(f"BT 1 0 0 1 {x:.1f} {y:.1f} Tm".encode())
            for run, is_latin in self._runs(text):
                if is_latin:
                    fname = "F2" if bold else "F1"
                    esc = (_latin1(run).replace("\\", r"\\").replace("(", r"\(")
                           .replace(")", r"\)"))
                    parts.append(f"/{fname} {size:.1f} Tf ({esc}) Tj"
                                 .encode("cp1252", errors="replace"))
                else:
                    if self.u_font:
                        hexed = self._unicode_glyphs(run).hex()
                        parts.append(f"/F3 {size:.1f} Tf <{hexed}> Tj"
                                     .encode("latin-1"))
                    else:
                        # no font covers this script: placeholders + warning
                        esc = _latin1(run).replace("(", r"\(").replace(")", r"\)")
                        parts.append(f"/F1 {size:.1f} Tf ({esc}) Tj"
                                     .encode("cp1252", errors="replace"))
            parts.append(b"ET")
        return b"\n".join(parts)

    # ---------------- helpers ---------------- #

    @staticmethod
    def _stream_obj(dict_body: str, data: bytes, compress: bool = True) -> bytes:
        if compress:
            data = zlib.compress(data, 6)
            dict_body = re.sub(r"/Length \d+", f"/Length {len(data)}", dict_body)
            if "/Filter" not in dict_body:
                dict_body = dict_body[:-2] + " /Filter /FlateDecode >>"
        else:
            dict_body = re.sub(r"/Length \d+", f"/Length {len(data)}", dict_body)
        return (dict_body.encode("latin-1") + b"\nstream\n" + data +
                b"\nendstream")

    def _tounicode_cmap(self) -> bytes:
        lines = ["/CIDInit /ProcSet findresource begin", "12 dict begin",
                 "begincmap",
                 "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) "
                 "/Supplement 0 >> def",
                 "/CMapName /Adobe-Identity-UCS def", "/CMapType 2 def",
                 "1 begincodespacerange <0000> <FFFF> endcodespacerange"]
        items = sorted(self._used_gids.items())
        for i in range(0, len(items), 100):
            chunk = items[i:i + 100]
            lines.append(f"{len(chunk)} beginbfchar")
            for gid, uni in chunk:
                lines.append(f"<{gid:04X}> <{uni:04X}>")
            lines.append("endbfchar")
        lines += ["endcmap", "CMapName currentdict /CMap defineresource pop",
                  "end", "end"]
        return "\n".join(lines).encode("utf-8")


def _build_document(markdown: str) -> tuple[bytes, list[str], int, str]:
    """Two-pass build: stream pages first (collecting used glyphs), then
    assemble the file with the embedded-font objects inserted."""
    writer = PDFWriter(markdown)
    if any(_needs_unicode(ln) for ln in markdown.splitlines()[:200]) and \
            writer.u_font is None:
        writer.warnings.append(
            "document contains non-Latin text but no covering TTF was found — "
            "set NANDAKA_FONT=/path/to/font.ttf (or install fonts) and "
            "re-export; affected glyphs are shown as placeholders")

    # pass 1: build page streams (populates _used_gids)
    _render_pages(writer, markdown)

    # pass 2: full render with embedded font when glyphs were actually used
    data = render_with_fonts(writer)
    if writer.missing_glyphs:
        sample = ", ".join(f"U+{ord(c):04X}" for c in sorted(writer.missing_glyphs)[:6])
        writer.warnings.append(
            f"{len(writer.missing_glyphs)} character(s) missing from "
            f"{writer.u_font.path.name if writer.u_font else 'available fonts'} "
            f"({sample}); shown as notdef")
    font_desc = (str(writer.u_font.path) if (writer.u_font and writer._used_gids)
                 else "Helvetica (built-in)")
    return data, sorted(set(writer.warnings)), len(writer.pages), font_desc


def _render_pages(writer: PDFWriter, markdown: str) -> None:
    in_table = False
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if not line:
            if in_table:
                in_table = False
                writer.spacer(4)
            writer.spacer(3)
            continue
        if line.startswith("|"):
            if not in_table:
                writer.spacer(4)
                in_table = True
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            writer.paragraph("  |  ".join(c for c in cells if c), size=9.0)
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            writer.heading(m.group(2), min(len(m.group(1)), 3))
            continue
        if re.match(r"^\s*[-*+]\s+", line):
            writer.bullet(re.sub(r"^\s*[-*+]\s+", "", line))
            continue
        if re.match(r"^\s*\d+\.\s+", line):
            writer.bullet(line.strip())
            continue
        if line.startswith("```") or line.strip() == "---":
            continue
        writer.paragraph(re.sub(r"\*\*|\_\_|\*|`", "", line))
    writer.spacer(6)
    writer.paragraph(
        "Generated by Nandaka — SSJ compliance tag is recorded in the batch's "
        "SSJ report; this file preserves content, not interactivity.", size=8.0)


def render_with_fonts(writer: PDFWriter) -> bytes:
    """Like PDFWriter.render but appends embedded-font objects AFTER pages so
    the glyph usage set is complete (Type0 font + ToUnicode + W array)."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    fn = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
             b"/Encoding /WinAnsiEncoding >>")
    fb = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
             b"/Encoding /WinAnsiEncoding >>")
    font_res = f"/F1 {fn} 0 R /F2 {fb} 0 R"

    # page + content streams
    n_obj_first = len(objects) + 1
    content_ids = [add(writer._stream_obj("<< /Length 0 >>",
                                          writer._page_stream(page, font_res),
                                          compress=True))
                   for page in writer.pages]
    pages_id = n_obj_first + 2 * len(writer.pages)
    page_ids = [add(b"") for _ in content_ids]
    for pid, cid in zip(page_ids, content_ids):
        objects[pid - 1] = (
            f"<< /Type /Page /Parent {pages_id} 0 R "
            f"/MediaBox [0 0 {A4[0]:.2f} {A4[1]:.2f}] "
            f"/Resources << /Font << {font_res} /F3 PLACEHOLDER >> >> "
            f"/Contents {cid} 0 R >>").encode("latin-1")

    # Pages node must be created NOW: its object id is already baked into the
    # page objects as /Parent, so it has to precede the font objects below.
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    pages_obj = add(f"<< /Type /Pages /Kids [{kids}] "
                    f"/Count {len(page_ids)} >>".encode("latin-1"))
    assert pages_obj == pages_id, "page object id mismatch"

    # embedded font objects (only if non-Latin glyphs were used)
    f3_ref = ""
    if writer.u_font and writer._used_gids:
        f: TTFont = writer.u_font
        fontfile_id = add(writer._stream_obj(
            f"<< /Length {len(f.raw)} /Length1 {len(f.raw)} >>",
            f.raw, compress=False))
        upm = f.units_per_em
        bbox = " ".join(str(v) for v in f.bbox)
        desc_id = add(
            (f"<< /Type /FontDescriptor /FontName /{f.postscript_name} "
             f"/Flags 32 /FontBBox [{bbox}] /ItalicAngle 0 "
             f"/Ascent {f.ascender} /Descent {f.descender} "
             f"/CapHeight {f.ascender} /StemV 80 /FontFile2 {fontfile_id} 0 R >>"
             ).encode("latin-1"))
        w_parts = [f"{gid} [{round(f.advance(gid) * 1000 / upm)}]"
                   for gid in sorted(writer._used_gids)]
        cid_id = add(
            (f"<< /Type /Font /Subtype /CIDFontType2 "
             f"/BaseFont /{f.postscript_name} /CIDSystemInfo << /Registry "
             f"(Adobe) /Ordering (Identity) /Supplement 0 >> /FontDescriptor "
             f"{desc_id} 0 R /DW 1000 /W [ {' '.join(w_parts)} ] "
             f"/CIDToGIDMap /Identity >>").encode("latin-1"))
        tu_id = add(writer._stream_obj("<< /Length 0 >>",
                                       writer._tounicode_cmap(), compress=True))
        type0_id = add(
            (f"<< /Type /Font /Subtype /Type0 /BaseFont /{f.postscript_name} "
             f"/Encoding /Identity-H /DescendantFonts [{cid_id} 0 R] "
             f"/ToUnicode {tu_id} 0 R >>").encode("latin-1"))
        f3_ref = f"/F3 {type0_id} 0 R"
        # patch page resources now that F3 is known
        for pid in page_ids:
            body = objects[pid - 1].replace(b"/F3 PLACEHOLDER",
                                            f3_ref.encode("latin-1"))
            objects[pid - 1] = body
    else:
        for pid in page_ids:
            objects[pid - 1] = objects[pid - 1].replace(b" /F3 PLACEHOLDER", b"")

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    cat_id = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("latin-1"))

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode("latin-1")
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root {cat_id} 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n").encode("latin-1")
    return bytes(out)


# --------------------------------------------------------------------------- #
# Public helper
# --------------------------------------------------------------------------- #


def markdown_to_pdf(markdown: str, out_path: str | Path) -> dict[str, Any]:
    """Render simple markdown (headings, bullets, paragraphs, tables) to PDF."""
    data, warnings, pages, font_desc = _build_document(markdown)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return {"path": str(out), "bytes": len(data), "pages": pages,
            "font": font_desc, "warnings": warnings}
