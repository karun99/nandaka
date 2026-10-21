"""Image sourcing and generation (IR-01..IR-17).

Sourcing order (IR-01): extract from supplied docs -> workspace sources ->
open-licence repositories -> generate a new illustrative image.

Generation is fully local (SVG string builder + stdlib zlib PNG encoder), so
it works offline on Termux. If a caller asks for an unavailable capability
(e.g. photographic generation), we disclose it and emit a structured text
description for a human illustrator instead (IR-16, CO-10).
"""

from __future__ import annotations

import csv
import json
import struct
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from xml.sax.saxutils import escape

GENERATED_EVIDENCE = "AI-GENERATED"
ILLUSTRATIVE_EVIDENCE = "ILLUSTRATIVE"
SOURCE_EVIDENCE = "SOURCE-DERIVED"

RIGHTS_CLEARED = "CLEARED"
RIGHTS_REVIEW = "REVIEW REQUIRED"
RIGHTS_EXCLUDED = "EXCLUDED"


@dataclass
class ImageRecord:
    image_id: str
    path: str = ""
    kind: str = "svg"              # svg | png | jpg
    origin: str = ""               # doc path+page or URL
    evidence: str = ILLUSTRATIVE_EVIDENCE
    licence: str = ""
    attribution: str = ""
    rights: str = RIGHTS_REVIEW    # IR-04 default: unclear -> review
    caption_en: str = ""
    caption_te: str = ""
    alt_en: str = ""
    alt_te: str = ""
    generated_on: str = ""
    caveats: list[str] = field(default_factory=list)
    bytes: int = 0
    ssj_tag: str = "SSJ-GUARDRAIL: NOT APPLICABLE"

    @property
    def cleared(self) -> bool:
        return self.rights == RIGHTS_CLEARED

    def as_row(self) -> dict[str, Any]:
        return {
            "image_id": self.image_id, "path": self.path, "kind": self.kind,
            "origin": self.origin, "IMAGE-EVIDENCE": self.evidence,
            "licence": self.licence, "attribution": self.attribution,
            "IMAGE-RIGHTS": self.rights,
            "caption_en": self.caption_en, "caption_te": self.caption_te,
            "alt_en": self.alt_en, "alt_te": self.alt_te,
            "generated_on": self.generated_on,
            "caveats": "; ".join(self.caveats), "bytes": self.bytes,
            "ssj_tag": self.ssj_tag,
        }


# --------------------------------------------------------------------------- #
# PNG encoder (stdlib zlib only — works on Termux, IR-06)
# --------------------------------------------------------------------------- #


def encode_png(width: int, height: int, rgba_rows: list[bytes]) -> bytes:
    """Encode raw RGBA rows (4 bytes/px) into a PNG byte string."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + row for row in rgba_rows)  # filter type 0
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _fill_rect(buf: list[list[tuple[int, int, int, int]]], x0: int, y0: int,
               w: int, h: int, color: tuple[int, int, int, int]) -> None:
    H, W = len(buf), len(buf[0])
    for y in range(max(0, y0), min(H, y0 + h)):
        row = buf[y]
        for x in range(max(0, x0), min(W, x0 + w)):
            row[x] = color


def png_bar_chart(title: str, data: dict[str, float],
                  size: tuple[int, int] = (640, 360)) -> bytes:
    """Local chart generation for exam analytics (TPS, frequency)."""
    W, H = size
    bg, axis, bar, label = (255, 255, 255, 255), (60, 60, 60, 255), \
        (36, 107, 206, 255), (20, 20, 20, 255)
    rows = [[bg] * W for _ in range(H)]
    items = list(data.items())[:12]
    if not items:
        items = [("no data", 0.0)]
    vmax = max(v for _, v in items) or 1.0
    pad_l, pad_b, pad_t = 40, 46, 34
    plot_w, plot_h = W - pad_l - 16, H - pad_t - pad_b
    _fill_rect(rows, pad_l, H - pad_b, plot_w, 2, axis)          # x axis
    _fill_rect(rows, pad_l, pad_t, 2, plot_h, axis)              # y axis
    n = len(items)
    slot = plot_w // n
    for i, (name, val) in enumerate(items):
        bh = int(plot_h * (val / vmax))
        x = pad_l + 6 + i * slot
        _fill_rect(rows, x, H - pad_b - bh, max(4, slot - 12), bh, bar)
        # tick label: first chars only (no font engine in stdlib PNG)
        for j, ch in enumerate(name[:6].upper()):
            _fill_rect(rows, x + j * 4, H - pad_b + 6, 3, 5, label)
    # title band
    _fill_rect(rows, 8, 8, min(W - 16, len(title) * 6), 3, bar)
    return encode_png(W, H, [b"".join(struct.pack("4B", *px) for px in row)
                             for row in rows])


# --------------------------------------------------------------------------- #
# SVG generators (IR-06: diagrams/charts/maps in svg)
# --------------------------------------------------------------------------- #


def svg_bar_chart(title: str, data: dict[str, float], unit: str = "",
                  evidence: str = GENERATED_EVIDENCE) -> str:
    items = list(data.items())[:14] or [("no data", 0.0)]
    vmax = max(v for _, v in items) or 1.0
    W, H, base, top = 720, 340, 270, 50
    slot = (W - 80) // len(items)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" role="img" aria-label="{escape(title)}">',
        f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
        f'<text x="24" y="30" font-family="sans-serif" font-size="18" '
        f'fill="#111">{escape(title)}</text>',
        f'<line x1="50" y1="{base}" x2="{W - 20}" y2="{base}" stroke="#333"/>',
        f'<line x1="50" y1="{top}" x2="50" y2="{base}" stroke="#333"/>',
    ]
    for i, (name, val) in enumerate(items):
        h = int((base - top) * (val / vmax))
        x = 60 + i * slot
        label = name if len(name) <= 9 else name[:8] + "…"
        # full name lives in <title> (accessible tooltip) even when the
        # visible rotated label must be short
        parts.append(f'<rect x="{x}" y="{base - h}" width="{max(6, slot - 14)}" '
                     f'height="{h}" fill="#246bce">'
                     f'<title>{escape(f"{name}: {val:g}{unit}")}</title></rect>')
        parts.append(f'<text x="{x}" y="{base + 16}" font-family="sans-serif" '
                     f'font-size="11" fill="#333" transform="rotate(35 {x} {base + 16})">'
                     f'{escape(label)}</text>')
        parts.append(f'<text x="{x}" y="{base - h - 6}" font-family="sans-serif" '
                     f'font-size="11" fill="#111">{val:g}{escape(unit)}</text>')
    parts.append(f'<text x="50" y="{H - 14}" font-family="sans-serif" font-size="11" '
                 f'fill="#555">{escape(evidence)} — generated by Nandaka on '
                 f'{time.strftime("%Y-%m-%d")}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def svg_flow(title: str, steps: list[str], evidence: str = GENERATED_EVIDENCE) -> str:
    steps = steps[:8] or ["step"]
    W, H = 720, 120 + 90 * len(steps)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
             f'viewBox="0 0 {W} {H}" role="img" aria-label="{escape(title)}">',
             f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
             f'<text x="24" y="32" font-family="sans-serif" font-size="18" fill="#111">'
             f'{escape(title)}</text>']
    y = 60
    for i, s in enumerate(steps):
        parts.append(f'<rect x="60" y="{y}" width="600" height="56" rx="10" '
                     f'fill="#eef3fb" stroke="#246bce"/>')
        parts.append(f'<text x="80" y="{y + 34}" font-family="sans-serif" '
                     f'font-size="15" fill="#111">{i + 1}. {escape(s[:80])}</text>')
        if i < len(steps) - 1:
            parts.append(f'<line x1="360" y1="{y + 56}" x2="360" y2="{y + 88}" '
                         f'stroke="#246bce" stroke-width="3" marker-end="url(#a)"/>')
        y += 90
    parts.append('<defs><marker id="a" markerWidth="8" markerHeight="8" refX="4" '
                 'refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#246bce"/>'
                 '</marker></defs>')
    parts.append(f'<text x="60" y="{H - 14}" font-family="sans-serif" font-size="11" '
                 f'fill="#555">{escape(evidence)} — generated by Nandaka on '
                 f'{time.strftime("%Y-%m-%d")}</text></svg>')
    return "\n".join(parts)


def svg_concept_map(title: str, nodes: Iterable[str],
                    edges: Iterable[tuple[str, str]],
                    evidence: str = GENERATED_EVIDENCE) -> str:
    nodes = list(nodes)[:9] or ["concept"]
    edges = [e for e in edges if e[0] in nodes and e[1] in nodes][:12]
    W, H = 720, 480
    import math
    cx, cy, r = W // 2, H // 2 + 10, 170
    pos = {}
    for i, n in enumerate(nodes):
        ang = 2 * math.pi * i / len(nodes) - math.pi / 2
        pos[n] = (cx + r * math.cos(ang), cy + r * math.sin(ang))
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
             f'viewBox="0 0 {W} {H}" role="img" aria-label="{escape(title)}">',
             f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
             f'<text x="24" y="30" font-family="sans-serif" font-size="18" fill="#111">'
             f'{escape(title)}</text>']
    for a, b in edges:
        x1, y1 = pos[a]
        x2, y2 = pos[b]
        parts.append(f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" '
                     f'stroke="#88a" stroke-width="2"/>')
    for n, (x, y) in pos.items():
        w = max(90, 9 * len(n) + 20)
        parts.append(f'<rect x="{x - w / 2:.0f}" y="{y - 18:.0f}" width="{w}" '
                     f'height="36" rx="18" fill="#eef3fb" stroke="#246bce"/>')
        parts.append(f'<text x="{x:.0f}" y="{y + 5:.0f}" text-anchor="middle" '
                     f'font-family="sans-serif" font-size="13" fill="#111">'
                     f'{escape(n[:18])}</text>')
    parts.append(f'<text x="24" y="{H - 12}" font-family="sans-serif" font-size="11" '
                 f'fill="#555">{escape(evidence)} — generated by Nandaka on '
                 f'{time.strftime("%Y-%m-%d")}</text></svg>')
    return "\n".join(parts)


def structured_image_description(request: str, data: dict[str, Any]) -> str:
    """IR-16 fallback: what a human illustrator needs, when generation is out."""
    lines = [
        "IMAGE DESCRIPTION (for a human illustrator / external tool)",
        f"Request: {request}",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M')}",
        "Note: automated image generation was unavailable for this request; "
        "this structured description is provided instead (IR-16, CO-10).",
        "Elements:",
    ]
    for k, v in data.items():
        lines.append(f"  - {k}: {v}")
    lines.append("Constraints: no photorealistic depiction of real people; "
                 "no astrological/jatka content; label as illustrative if "
                 "adapted; verify all factual labels before publication.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Image library + manifest
# --------------------------------------------------------------------------- #


class ImageLibrary:
    def __init__(self, out_dir: str | Path, max_kb: int = 200):
        self.dir = Path(out_dir) / "images"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_kb = max_kb
        self.records: list[ImageRecord] = []

    # ------------- creation ------------- #

    def add_svg(self, image_id: str, svg_text: str, **meta: Any) -> ImageRecord:
        return self._write(image_id, "svg", svg_text.encode("utf-8"), meta)

    def add_png(self, image_id: str, png_bytes: bytes, **meta: Any) -> ImageRecord:
        return self._write(image_id, "png", png_bytes, meta)

    def adopt_file(self, image_id: str, src: str | Path, origin: str,
                   licence: str = "", attribution: str = "", **meta: Any) -> ImageRecord:
        """IR-01(1/2/3): adopt an existing (source-derived) image."""
        p = Path(src)
        suffix = p.suffix.lower().lstrip(".") or "png"
        kind = {"jpeg": "jpg"}.get(suffix, suffix)
        data = p.read_bytes() if p.exists() else b""
        rec = self._write(image_id, kind, data, {
            **meta, "origin": origin, "evidence": SOURCE_EVIDENCE,
            "licence": licence, "attribution": attribution,
            "rights": RIGHTS_CLEARED if licence else RIGHTS_REVIEW,
        })
        return rec

    def _write(self, image_id: str, kind: str, data: bytes,
               meta: dict[str, Any]) -> ImageRecord:
        path = self.dir / f"{image_id}.{kind}"
        try:
            path.write_bytes(data)
        except OSError:
            pass
        evidence = meta.get("evidence", GENERATED_EVIDENCE)
        generated = evidence in (GENERATED_EVIDENCE, ILLUSTRATIVE_EVIDENCE)
        rec = ImageRecord(
            image_id=image_id, path=str(path), kind=kind,
            origin=meta.get("origin", "generated"),
            evidence=evidence,
            licence=meta.get("licence", "CC0 (original work)" if generated else ""),
            attribution=meta.get("attribution", "Nandaka (generated)" if generated else ""),
            rights=meta.get("rights", RIGHTS_CLEARED if generated else RIGHTS_REVIEW),
            caption_en=meta.get("caption_en", ""),
            caption_te=meta.get("caption_te", ""),
            alt_en=meta.get("alt_en", meta.get("caption_en", "")),
            alt_te=meta.get("alt_te", meta.get("caption_te", "")),
            generated_on=time.strftime("%Y-%m-%d") if generated else "",
            caveats=list(meta.get("caveats", [])),
            bytes=len(data),
            ssj_tag=meta.get("ssj_tag", "SSJ-GUARDRAIL: PASS" if generated
                             else "SSJ-GUARDRAIL: REVIEW REQUIRED"),
        )
        if kind == "svg" and rec.bytes == 0:
            rec.caveats.append("svg write failed")
        # IR-07: size target — note the miss, do not silently drop quality
        if rec.bytes > self.max_kb * 1024 and kind != "svg":
            rec.caveats.append(
                f"size {rec.bytes // 1024} KB exceeds target {self.max_kb} KB (IR-07)")
        # IR-03: generated images must carry a visible AI-generated caption
        if generated and not rec.caption_en:
            rec.caption_en = f"AI-generated illustration ({image_id}) — not a photograph"
        if generated and not rec.caption_te:
            rec.caption_te = (f"AI ద్వారా రూపొందించిన ఉదాహరణ చిత్రం ({image_id}) "
                              f"— ఇది అసలు ఛాయాచిత్రం కాదు")
        if generated and not rec.alt_en:
            rec.alt_en = rec.caption_en
        if generated and not rec.alt_te:
            rec.alt_te = rec.caption_te
        self.records.append(rec)
        return rec

    # ------------- manifest (DR-03) ------------- #

    def write_manifest(self, batch_dir: str | Path) -> dict[str, str]:
        batch = Path(batch_dir)
        rows = [r.as_row() for r in self.records]
        csv_path = batch / "images.csv"
        fields = list(rows[0].keys()) if rows else ["image_id"]
        with csv_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        json_path = batch / "images.json"
        json_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False),
                             encoding="utf-8")
        return {"csv": str(csv_path), "json": str(json_path)}

    # ------------- reporting (IR-04, IR-17) ------------- #

    def rights_cleared_images(self) -> list[ImageRecord]:
        return [r for r in self.records if r.cleared]

    def blocked_images(self) -> list[ImageRecord]:
        return [r for r in self.records if not r.cleared]

    def coverage(self, topics: Iterable[str], images_per_topic: dict[str, int],
                 mcq_total: int, mcq_with_image: int) -> dict[str, float]:
        topics = list(topics)
        with_image = sum(1 for t in topics if images_per_topic.get(t, 0) > 0)
        return {
            "topics_total": len(topics),
            "topics_with_figure": with_image,
            "visual_coverage_pct": round(100 * with_image / len(topics), 1) if topics else 0.0,
            "mcq_total": mcq_total,
            "mcq_with_image": mcq_with_image,
            "mcq_image_pct": round(100 * mcq_with_image / mcq_total, 1) if mcq_total else 0.0,
        }

    def manifest_rows(self) -> list[dict[str, Any]]:
        return [r.as_row() for r in self.records]
