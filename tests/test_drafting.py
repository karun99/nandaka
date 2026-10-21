"""Drafting helpers: JSON parsing, section splitting, unsourced-claim flags,
MCQ shortfall honesty (FR-GOV-06), T0 forcing (FR-GOV-07), dedupe (FR-GOV-05)."""

import json

import pytest

from nandaka.drafting import (Drafter, parse_json_payload, split_sections,
                              unsourced_claims)
from nandaka.models.client import ChatError


class FakeClient:
    """Stands in for ChatClient so drafting logic tests run offline."""

    def __init__(self, replies, provider="local"):
        self.replies = list(replies)
        self.provider = provider
        self.temperature = 0.4
        self.seed = 42
        self.last_usage = {"prompt_tokens": 10, "completion_tokens": 10}
        self.calls = []

    def chat(self, messages, **kw):
        self.calls.append(messages)
        if not self.replies:
            raise ChatError("fake client exhausted")
        content = self.replies.pop(0)
        return {"choices": [{"message": {"content": content}}], "usage": {}}

    def text(self, messages, **kw):
        return self.chat(messages, **kw)["choices"][0]["message"]["content"]

    @staticmethod
    def system_user(system, user):
        return [{"role": "system", "content": system},
                {"role": "user", "content": user}]


def drafter(replies, tier="T1"):
    return Drafter(FakeClient(replies), {"name": "fake", "tier": tier,
                                         "path": "/fake.gguf", "quant": "Q4"})


# ---------------- parsing helpers ---------------- #


def test_parse_json_plain_and_fenced():
    assert parse_json_payload('{"a": 1}') == {"a": 1}
    assert parse_json_payload('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_payload('noise before {"a": [1, 2]} noise') == {"a": [1, 2]}
    assert parse_json_payload('["x"]') == ["x"]
    assert parse_json_payload("no json at all") is None


def test_parse_json_with_braces_in_strings():
    text = 'Result: {"stem": "use {curly} braces", "n": 2} trailing'
    data = parse_json_payload(text)
    assert data["stem"] == "use {curly} braces"
    assert data["n"] == 2


def test_split_sections():
    parts = split_sections("intro line\n## Concepts\nbody here\n### Detail\ndeep")
    assert parts[0][0] == "Intro"
    assert ("Concepts", "body here") in parts or parts[1][0] == "Concepts"


def test_unsourced_claims_flags_but_not_citations():
    text = ("The constitution was adopted in 1949. "
            "It was founded by the assembly (source: NCERT, p. 12). "
            "Short note.")
    claims = unsourced_claims(text)
    assert any("adopted in 1949" in c for c in claims)
    assert not any("founded by" in c for c in claims)   # has a source marker


# ---------------- MCQ generation ---------------- #


def _mcq_json(n=2, start=1):
    items = []
    for i in range(start, start + n):
        items.append({
            "id": f"m{i}", "topic": "Polity", "subtopic": "x",
            "stem_en": f"Question {i} about articles?",
            "stem_te": f"ప్రశ్న {i}?",
            "options_en": ["A1", "B1", "C1", "D1"],
            "options_te": ["ఎ1", "బి1", "సి1", "డి1"],
            "answer": "A1", "explanation_en": "because",
            "explanation_te": "ఎందుకంటే",
            "source_reference": "NCERT", "difficulty": "easy",
            "historical_evidence": "",
        })
    return json.dumps({"mcqs": items})


def test_generate_mcqs_happy_path():
    d = drafter([_mcq_json(2)], tier="T1")
    out = d.generate_mcqs({"topic": "Polity", "exam": "Group 2"}, count=2)
    assert out["produced"] == 2
    assert out["shortfall"] == 0
    assert out["provenance"] and out["provenance"][0]["tier"] == "T1"
    assert out["mcqs"][0]["forced_review"] is False


def test_generate_mcqs_shortfall_reported_never_fabricated():
    """FR-GOV-06: fewer usable results -> shortfall disclosed, no padding."""
    d = drafter([_mcq_json(1, start=1), "garbage", "garbage"], tier="T2")
    out = d.generate_mcqs({"topic": "Polity"}, count=3)
    assert out["produced"] == 1
    assert out["shortfall"] == 2
    assert any("NOT fabricating" in w for w in out["warnings"])


def test_t0_forces_every_mcq_to_review():
    """FR-GOV-07 / TC-10: T0 output always flagged for human review."""
    d = drafter([_mcq_json(2)], tier="T0")
    out = d.generate_mcqs({"topic": "Polity"}, count=2)
    assert all(m["forced_review"] for m in out["mcqs"])
    assert any("T0" in w for w in out["warnings"])


def test_bilingual_fields_present():
    """FR-GOV-03/04: required fields for EN+TE MCQs."""
    d = drafter([_mcq_json(1)], tier="T2")
    out = d.generate_mcqs({"topic": "Polity"}, count=1)
    m = out["mcqs"][0]
    for key in ("id", "topic", "stem_en", "stem_te", "options_en", "options_te",
                "answer", "explanation_en", "explanation_te",
                "source_reference", "difficulty", "historical_evidence",
                "tps", "probability", "negative_marking_note",
                "tier_generated", "forced_review"):
        assert key in m, key
    assert len(m["options_en"]) == 4 and len(m["options_te"]) == 4


def test_dedupe_within_batch():
    same = _mcq_json(1, start=1)
    items = parse_json_payload(same)["mcqs"]
    dup = dict(items[0])
    d = drafter([json.dumps({"mcqs": items + [dup]})], tier="T2")
    out = d.generate_mcqs({"topic": "Polity"}, count=2)
    assert out["produced"] == 1   # near-duplicate dropped (FR-GOV-05)


def test_generation_failure_is_honest():
    d = drafter(["this is not json"], tier="T2")
    out = d.generate_mcqs({"topic": "Polity"}, count=2)
    assert out["produced"] == 0
    assert any("JSON" in w or "json" in w for w in out["warnings"])


# ---------------- chapter drafting ---------------- #


def test_chapter_sections_and_provenance():
    section_md = ("## Introduction\nSome intro text about the chapter.\n\n"
                  "## Concepts\nConcept explanation with detail.")
    d = drafter([section_md] * 5, tier="T1")
    spec = {"class": "8", "subject": "Civics", "title": "Rights",
            "target_words": 600,
            "outcomes": ["Understand rights"], "sources": []}
    ch = d.draft_chapter(spec)
    assert ch["ok"]
    assert len(ch["sections"]) == len(Drafter.SECTIONS)
    assert all(p["model"] == "fake" and p["tier"] == "T1"
               for p in ch["provenance"])          # FR-SB-05
    assert "Introduction" in ch["text"]


def test_chapter_records_unsourced_claims():
    bad = ("## Concepts\nThe directive principles were adopted from the Irish "
           "constitution and they guide policy making in the country.")
    d = drafter([bad] * 5, tier="T1")
    ch = d.draft_chapter({"title": "x", "target_words": 300})
    assert ch["unsourced"]            # FR-SB-03 flags them


def test_book_bibliography_and_structure():
    sec = "### One\nText citing [Author, 2019] here.\n"
    replies = [sec] * 4 + ["# Dup\n", sec]
    d = drafter(replies, tier="T2")
    outline = {"title": "Book", "chapters": [
        {"title": "C1", "sections": [{"title": "One", "words": 100}]},
        {"title": "C1", "sections": [{"title": "Two", "words": 100}]},
    ]}
    book = d.draft_book(outline)
    assert book["ok"]
    assert any("Author, 2019" in b for b in book["bibliography"])   # FR-AB-02
    assert any("duplicate chapter" in w for w in book["structure_warnings"])


def test_tier_gate_blocks_long_draft_without_force():
    """FR-GGUF-11: T0 cannot silently produce long-form chapters."""
    from nandaka.models.registry import tier_gate
    ok, msg = tier_gate("long_draft", "T0", force=False)
    assert ok is False and "larger model" in msg and "T0" in msg
    ok2, _ = tier_gate("long_draft", "T0", force=True)
    assert ok2 is True                      # explicit override allowed
    ok3, msg3 = tier_gate("long_draft", "T1")
    assert ok3 is True and "partial" in msg3
    ok4, msg4 = tier_gate("mcq", "T0")
    assert ok4 is True and "HUMAN REVIEW" in msg4    # 'basic' quality notice
    ok5, _ = tier_gate("long_draft", "T2")
    assert ok5 is True                      # fully supported tier
