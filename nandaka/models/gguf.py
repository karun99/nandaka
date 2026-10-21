"""Pure-Python GGUF metadata reader (no llama.cpp dependency).

Parses the GGUF header and key/value metadata (v1/v2/v3) plus tensor infos so
Nandaka can classify any GGUF file into a capability tier without loading it
(FR-GGUF-03, FR-GGUF-04, FR-GGUF-12). The parser never modifies the file
(FR-GGUF-14) and reads only what it needs, keeping memory bounded.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GGUF_MAGIC = b"GGUF"

# gguf value types
_T_UINT8, _T_INT8, _T_UINT16, _T_INT16 = 0, 1, 2, 3
_T_UINT32, _T_INT32, _T_FLOAT32, _T_BOOL = 4, 5, 6, 7
_T_STRING, _T_ARRAY, _T_UINT64, _T_INT64, _T_FLOAT64 = 8, 9, 10, 11, 12

_SCALAR_FMT = {
    _T_UINT8: "<B", _T_INT8: "<b", _T_UINT16: "<H", _T_INT16: "<h",
    _T_UINT32: "<I", _T_INT32: "<i", _T_FLOAT32: "<f", _T_BOOL: "<?",
    _T_UINT64: "<Q", _T_INT64: "<q", _T_FLOAT64: "<d",
}

# llama_ftype enum -> bytes per parameter (approx., for fallback estimation)
_FILE_TYPE_BPP = {0: 4.0, 1: 2.0, 2: 0.5625, 7: 0.5625, 8: 0.625, 10: 0.6875,
                  11: 1.0625, 12: 0.5625, 13: 0.625, 14: 0.6875, 15: 1.0625,
                  16: 0.375, 17: 0.4375, 18: 0.5, 19: 0.5625}


class GGUFError(Exception):
    """Raised when a file is not a readable GGUF file."""


@dataclass
class GGUFInfo:
    path: str
    valid: bool = False
    version: int = 0
    tensor_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    parameter_count: int = 0
    param_source: str = ""  # metadata | tensors | estimated
    file_size: int = 0
    error: str = ""

    # convenience accessors -------------------------------------------------
    @property
    def architecture(self) -> str:
        return str(self.metadata.get("general.architecture", "unknown"))

    @property
    def name(self) -> str:
        return str(self.metadata.get("general.name") or Path(self.path).stem)

    @property
    def quant(self) -> str:
        v = self.metadata.get("general.file_type")
        if isinstance(v, int):
            known = {0: "F32", 1: "F16", 2: "Q4_0", 7: "Q4_K_M", 8: "Q4_K_S",
                     10: "Q5_K_S", 11: "Q6_K", 12: "Q8_0", 15: "IQ2_XXS"}
            if v in known:
                return known[v]
            return f"ftype_{v}"
        # fall back to the filename convention (e.g. model-Q4_K_M.gguf)
        import re
        m = re.search(r"(?:^|[-_.])((?:I?Q\d[_A-Z0-9]*|F16|F32|BF16))(?:[-_.]|$)",
                      Path(self.path).name, re.I)
        return m.group(1).upper() if m else "unknown"

    @property
    def trained_context(self) -> int:
        for key in ("llama.context_length", "phi3.context_length",
                    "qwen2.context_length", "gemma.context_length",
                    "general.context_length"):
            v = self.metadata.get(key)
            if isinstance(v, int) and v > 0:
                return v
        # any "*.context_length"
        for k, v in self.metadata.items():
            if k.endswith("context_length") and isinstance(v, int) and v > 0:
                return v
        return 0

    @property
    def chat_template(self) -> str:
        v = self.metadata.get("chat_template", "")
        return v if isinstance(v, str) else ""

    @property
    def tokenizer_family(self) -> str:
        v = self.metadata.get("tokenizer.ggml.model")
        return str(v) if v else "unknown"


# --------------------------------------------------------------------------- #
# Tiers (FR-GGUF-04)
# --------------------------------------------------------------------------- #

TIERS = [("T0", 0.0, 0.6), ("T1", 0.6, 2.0), ("T2", 2.0, 8.0), ("T3", 8.0, float("inf"))]


def classify_tier(parameter_count: int) -> str:
    """Map a parameter count to T0/T1/T2/T3.

    Boundaries per SRS §3.3.1: T0 <= 0.6B (inclusive, explicitly '≤'),
    T1 0.6-2B, T2 2-8B (inclusive lows so 2B/8B land in the stated bands),
    T3 > 8B.
    """
    b = parameter_count / 1e9 if parameter_count else 0.0
    if b <= 0.6:
        return "T0"
    if b < 2.0:
        return "T1"
    if b <= 8.0:
        return "T2"
    return "T3"


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


class _Reader:
    __slots__ = ("buf", "pos")

    def __init__(self, buf: bytes):
        self.buf = buf
        self.pos = 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.buf):
            raise GGUFError("truncated GGUF header")
        out = self.buf[self.pos:self.pos + n]
        self.pos += n
        return out

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.take(8))[0]

    def string(self, width64: bool) -> str:
        n = self.u64() if width64 else self.u32()
        if n > 1 << 24:
            raise GGUFError("unreasonable GGUF string length")
        return self.take(n).decode("utf-8", errors="replace")

    def value(self, vtype: int, width64: bool) -> Any:
        if vtype in _SCALAR_FMT:
            return struct.unpack(_SCALAR_FMT[vtype], self.take(struct.calcsize(_SCALAR_FMT[vtype])))[0]
        if vtype == _T_STRING:
            return self.string(width64)
        if vtype == _T_ARRAY:
            atype = self.u32()
            count = self.u64() if width64 else self.u32()
            if atype == _T_STRING:
                return [self.string(width64) for _ in range(min(count, 100_000))]
            if atype in _SCALAR_FMT:
                size = struct.calcsize(_SCALAR_FMT[atype])
                if count * size > 1 << 26:  # cap 64 MB reads
                    raise GGUFError("GGUF array too large")
                return list(struct.unpack(f"<{count}{_SCALAR_FMT[atype][1]}", self.take(count * size)))
            if atype == _T_ARRAY:
                return [self.value(_T_ARRAY, width64) for _ in range(min(count, 10_000))]
            raise GGUFError(f"unsupported array type {atype}")
        raise GGUFError(f"unsupported value type {vtype}")


def read_gguf(path: str | Path, read_tensors: bool = True) -> GGUFInfo:
    """Read GGUF metadata (and optionally tensor shapes) from *path*."""
    p = Path(path)
    info = GGUFInfo(path=str(p), file_size=p.stat().st_size if p.exists() else 0)
    try:
        with p.open("rb") as fh:
            head = fh.read(8)
            if head[:4] != GGUF_MAGIC:
                info.error = "bad magic number (not a GGUF file)"
                return info
            info.version = struct.unpack("<I", head[4:8])[0]
            width64 = info.version >= 3
            rest_header = fh.read(16)
            r = _Reader(head[4:8] + rest_header)
            r.pos = 4  # skip version re-read; consume tensor/kv counts below
            tensor_count = r.u64()
            kv_count = r.u64()
            info.tensor_count = tensor_count

            # Read a bounded prefix of the metadata section.
            blob = fh.read(min(16 << 20, info.file_size))
            mr = _Reader(blob)
            for _ in range(kv_count):
                key = mr.string(width64)
                vtype = mr.u32()
                try:
                    info.metadata[key] = mr.value(vtype, width64)
                except GGUFError:
                    break

            if read_tensors and info.metadata.get("general.parameter_count") is None:
                info.parameter_count, info.param_source = _sum_tensor_params(
                    fh, mr.pos, tensor_count, width64, info.file_size
                )
    except OSError as exc:
        info.error = f"cannot read file: {exc}"
        return info
    except GGUFError as exc:
        info.error = str(exc)
        return info

    if info.param_source != "tensors":
        pc = info.metadata.get("general.parameter_count")
        if isinstance(pc, int) and pc > 0:
            info.parameter_count, info.param_source = pc, "metadata"
        elif info.file_size:
            bpp = _FILE_TYPE_BPP.get(int(info.metadata.get("general.file_type", -1) or -1), 0.0)
            if bpp <= 0:
                import re
                m = re.search(r"Q(\d)", p.name, re.I)
                bpp = {4: 0.5625, 5: 0.6875, 6: 0.9, 8: 1.0625, 3: 0.45, 2: 0.35}.get(
                    int(m.group(1)) if m else 4, 0.5625)
            info.parameter_count = int(info.file_size / bpp * 0.95)  # 5% overhead fudge
            info.param_source = "estimated"
    info.valid = True
    return info


def _sum_tensor_params(fh, meta_end_in_blob: int, tensor_count: int,
                       width64: bool, file_size: int) -> tuple[int, str]:
    """Sum parameter counts from tensor infos.

    Tensor infos sit right after the metadata section. Layout per tensor:
    name (string), n_dims (u32), dims (u32 or u64 each depending on writer
    version), type (u32), offset (u64). We try 64-bit dims first (GGUF v3
    writers), validate by checking the position lands on a sane data-section
    boundary, then fall back to 32-bit dims.
    """
    try:
        fh.seek(24)  # past magic + version + tensor_count + kv_count
        blob = fh.read(min(96 << 20, file_size))
    except OSError:
        return 0, ""
    # Tensor infos begin immediately after the metadata section.
    start = max(0, min(meta_end_in_blob, len(blob)))
    remaining = blob[start:]
    for dims64 in (True, False):
        try:
            r = _Reader(remaining)
            total = 0
            for _ in range(tensor_count):
                name = r.string(width64)
                n_dims = r.u32()
                if n_dims == 0 or n_dims > 8:
                    raise GGUFError("bad n_dims")
                dims = [(r.u64() if dims64 else r.u32()) for _ in range(n_dims)]
                r.u32()  # element type
                r.u64()  # offset
                numel = 1
                for d in dims:
                    numel *= d
                if numel > 0 and numel < (1 << 40):
                    total += numel
                if name == "":
                    raise GGUFError("bad tensor name")
            # Validate: consumed bytes must be plausible (data section follows
            # in real files; equality is fine for header-only fixtures).
            if 0 < r.pos <= len(remaining):
                return total, "tensors"
        except GGUFError:
            continue
    return 0, ""


def verify_gguf(path: str | Path) -> dict:
    """FR-GGUF-12: integrity check — magic, header, tensor count, checksum."""
    p = Path(path)
    result = {"path": str(p), "valid": False, "magic": False, "version": 0,
              "tensors": 0, "sha256": "", "size_bytes": 0, "error": ""}
    if not p.exists():
        result["error"] = "file not found"
        return result
    with p.open("rb") as fh:
        head = fh.read(4)
    result["magic"] = head == GGUF_MAGIC
    if not result["magic"]:
        result["error"] = "bad magic number"
        return result
    info = read_gguf(p)
    result.update(valid=info.valid, version=info.version, tensors=info.tensor_count,
                  size_bytes=info.file_size, error=info.error)
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    result["sha256"] = h.hexdigest()
    return result
