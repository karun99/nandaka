"""GGUF parsing, tier classification, integrity verification (FR-GGUF-*)."""

import struct

import pytest

from nandaka.models.gguf import GGUFError, classify_tier, read_gguf, verify_gguf
from tests.conftest import build_gguf


def test_tier_boundaries():
    """FR-GGUF-04: T0 <=0.6B, T1 0.6-2B, T2 2-8B, T3 >8B."""
    assert classify_tier(300_000_000) == "T0"
    assert classify_tier(600_000_000) == "T0"
    assert classify_tier(600_000_001) == "T1"
    assert classify_tier(1_500_000_000) == "T1"
    assert classify_tier(2_000_000_000) == "T2"
    assert classify_tier(7_900_000_000) == "T2"
    assert classify_tier(8_000_000_001) == "T3"


def test_read_metadata_and_tensor_params(model_dir):
    """FR-GGUF-03: architecture, params, context, template read from file."""
    p = build_gguf(model_dir / "t1-model.gguf", params=1_500_000_000,
                   chat_template="{% messages %}")
    info = read_gguf(p)
    assert info.valid and info.error == ""
    assert info.version == 3
    assert info.architecture == "llama"
    assert info.parameter_count == 1_500_000_000
    assert info.param_source == "tensors"
    assert info.quant == "Q8_0"
    assert info.trained_context == 4096
    assert info.chat_template.startswith("{%")
    assert classify_tier(info.parameter_count) == "T1"


def test_reject_non_gguf(tmp_path):
    bad = tmp_path / "not-a-model.gguf"
    bad.write_bytes(b"PK\x03\x04 definitely not gguf data" * 10)
    info = read_gguf(bad)
    assert info.valid is False
    assert "magic" in info.error
    res = verify_gguf(bad)
    assert res["valid"] is False and res["magic"] is False


def test_verify_reports_checksum_and_shape(model_dir):
    """FR-GGUF-12: magic, header, tensor count, sha256."""
    p = build_gguf(model_dir / "v.gguf", params=800_000_000)
    res = verify_gguf(p)
    assert res["valid"] and res["magic"] and res["version"] == 3
    assert res["tensors"] == 1
    assert len(res["sha256"]) == 64
    assert res["size_bytes"] == p.stat().st_size


def test_missing_file_verify(tmp_path):
    res = verify_gguf(tmp_path / "ghost.gguf")
    assert res["valid"] is False and "not found" in res["error"]


def test_truncated_header(tmp_path):
    p = tmp_path / "trunc.gguf"
    p.write_bytes(b"GGUF" + struct.pack("<I", 3))  # header cut short
    info = read_gguf(p)
    assert info.valid is False
    assert info.error


def test_parameter_count_from_metadata_key(model_dir):
    """Fallback: general.parameter_count metadata wins over tensor sum."""
    import struct as st
    from tests.conftest import _gstr
    kvs = []
    kvs.append(_gstr("general.architecture") + st.pack("<I", 8) +
               _gstr("gemma"))
    kvs.append(_gstr("general.parameter_count") + st.pack("<I", 10) +
               st.pack("<Q", 3_000_000_000))          # uint64 = type 10
    data = (b"GGUF" + st.pack("<I", 3) + st.pack("<QQ", 0, len(kvs)) +
            b"".join(kvs))
    p = model_dir / "meta-count.gguf"
    p.write_bytes(data)
    info = read_gguf(p)
    assert info.valid
    assert info.parameter_count == 3_000_000_000
    assert info.param_source == "metadata"
    assert classify_tier(info.parameter_count) == "T2"
