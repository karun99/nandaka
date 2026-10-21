"""End-to-end pipeline tests with a stubbed model backend (offline, FR-WF-01).

No llama-server, no network: ModelManager is replaced by a fake that serves a
deterministic ChatClient, so the whole workflow (ingest -> analysis -> MCQ ->
images -> validation -> SSJ -> review -> export -> metrics) runs for real.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from nandaka.models.client import ChatError
from nandaka.pipeline import run_book, run_chapter, run_mcq_batch
from tests.conftest import make_papers_csv


# --------------------------------------------------------------------------- #
# Deterministic fakes
# --------------------------------------------------------------------------- #

# Distinct stems so the in-batch near-duplicate filter keeps every item.
_STEMS_EN = [
    "Which article of the Constitution guarantees equality before law?",
    "Who administers the oath of office to the President of India?",
    "Which house of Parliament is called the Council of States?",
    "What does the Directive Principles of State Policy provide for?",
    "Which body conducts elections to the Panchayati Raj institutions?",
    "What is the minimum age for contesting a Lok Sabha election?",
]
_STEMS_TE = [
    "చట్టం ముందు సమానత్వాన్ని ఏ అధికరణం హామీ ఇస్తుంది?",
    "రాష్ట్రపతికి ప్రమాణ స్వీకారం ఎవరు చేయిస్తారు?",
    "రాజ్యసభకు మరొక పేరేమిటి?",
    "రాష్ట్ర విధాన నిర్దేశక సూత్రాలు దేని కోసం ప్రతిపాదిస్తాయి?",
    "పంచాయతీ రాజ్ సంస్థల ఎన్నికలను ఏ సంస్థ నిర్వహిస్తుంది?",
    "లోక్‌సభ ఎన్నికల్లో పోటీ చేయడానికి కనీస వయసు ఎంత?",
]
_OPTIONS_EN = ["Article 14", "Article 19", "Article 21", "Article 32"]
_OPTIONS_TE = ["అధికరణ 14", "అధికరణ 19", "అధికరణ 21", "అధికరణ 32"]


def make_mcq(i: int) -> dict:
    """A structurally complete bilingual MCQ that clears the validation battery."""
    idx = i % len(_STEMS_EN)
    return {
        "id": f"mcq-{i + 1}",
        "topic": "Constitution",
        "subtopic": "Fundamental Rights",
        "stem_en": _STEMS_EN[idx],
        "stem_te": _STEMS_TE[idx],
        "options_en": list(_OPTIONS_EN),
        "options_te": list(_OPTIONS_TE),
        "answer": _OPTIONS_EN[idx % len(_OPTIONS_EN)],
        "explanation_en": "The text of the Constitution states this provision "
                          "(source-grounded fact, NCERT Class 8 Civics).",
        "explanation_te": "రాజ్యాంగ వచనం ఈ నిబంధనను పేర్కొంటుంది "
                          "(మూలం: NCERT తరగతి 8 సివిక్స్).",
        "source_reference": "NCERT Class 8 Civics, Chapter 2",
        "difficulty": ["easy", "medium", "hard"][i % 3],
        "historical_evidence": "PAPER-2023 direct question on this subtopic",
    }


class FakeClient:
    """Serves the OpenAI-shaped responses Drafter expects."""

    provider = "local"
    model = "fake-gguf"

    def __init__(self):
        self.temperature = 0.4
        self.seed = 42
        self.last_usage = {}
        self.calls: list[dict] = []

    def chat(self, messages, *, max_tokens=1024, temperature=None,
             json_mode=False, use_cache=True, extra=None):
        user = messages[-1]["content"]
        self.calls.append({"user": user, "json_mode": json_mode,
                           "max_tokens": max_tokens})
        if json_mode:
            m = re.search(r"Create (\d+) multiple-choice questions", user)
            n = int(m.group(1)) if m else 1
            content = json.dumps(
                {"mcqs": [make_mcq(len(self.calls) * 10 + k) for k in range(n)]},
                ensure_ascii=False)
        else:
            heading = re.search(r"Section: (\w[\w -]*)\n", user)
            name = heading.group(1) if heading else "Draft"
            content = (
                f"## {name}\n\nThis section explains the idea in plain, "
                "exam-accurate language. Every sentence here is short enough "
                "to revise from (source: NCERT handbook).\n\n"
                "- revision point one\n- revision point two")
        self.last_usage = {"prompt_tokens": 120, "completion_tokens": 80,
                           "total_tokens": 200}
        return {"choices": [{"message": {"content": content}}],
                "usage": dict(self.last_usage)}


class FakeManager:
    """Drop-in replacement for ModelManager (no llama-server needed)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.client_obj = FakeClient()
        self.unloaded = False
        self.ram = {"rss_mb": 0, "state": "fake"}

    def task_client(self, feature, model_ref=None, force=False,
                    prefer_cloud=False):
        entry = {"name": "fake-gguf", "path": "/nonexistent/fake.gguf",
                 "tier": "T2", "quant": "Q8_0", "parameters": 3_000_000_000,
                 "context_length": 4096}
        return self.client_obj, entry, ""

    def ram_report(self):
        return dict(self.ram)

    def unload(self):
        self.unloaded = True


@pytest.fixture()
def fake_backend(monkeypatch):
    import nandaka.pipeline as pipeline
    mgr = {"last": None}

    def factory(cfg):
        mgr["last"] = FakeManager(cfg)
        return mgr["last"]

    monkeypatch.setattr(pipeline, "ModelManager", factory)
    return mgr


# --------------------------------------------------------------------------- #
# MCQ batch
# --------------------------------------------------------------------------- #


def test_mcq_batch_offline_end_to_end(nandaka_home, fake_backend, tmp_path):
    cfg = nandaka_home
    papers = make_papers_csv(tmp_path / "past.csv")
    source = tmp_path / "notes.md"
    source.write_text("# Constitution\n\nEquality before law is Article 14.\n",
                      encoding="utf-8")

    spec = {
        "topic": "Constitution",
        "exam": "Group-2",
        "subject": "General Studies",
        "syllabus": ["Constitution", "Economy"],
        "marking": {"R": 1, "W": 0.33, "U": 0},
        "p_candidate": "0.6",
        "inputs": [str(source)],
    }
    summary = run_mcq_batch(cfg, spec, count=6, papers_csv=str(papers),
                             offline=True)

    assert summary["ok"] is True, summary.get("error")
    assert summary["mcqs"] >= 1
    assert summary["shortfall"] == 0

    batch_dir = Path(summary["batch_dir"])
    assert batch_dir.exists()
    for name in ("report.md", "report.txt", "report.pdf", "mcqs.csv",
                 "mcqs.xlsx", "validation.csv", "review-queue.csv",
                 "ssj-report.csv", "frequency.csv", "tps.csv",
                 "provenance.json", "metrics.json", "images.csv",
                 "batch-summary.json"):
        assert (batch_dir / name).exists(), f"missing {name}"
    assert summary["all_valid"], summary["invalid"]

    report = (batch_dir / "report.md").read_text(encoding="utf-8")
    assert "## 16. Next-Cycle Prompt" in report
    assert "Constitution" in report

    # provenance: offline run must be recorded as such (DR-06)
    prov = json.loads((batch_dir / "provenance.json").read_text(encoding="utf-8"))
    assert prov["cloud"]["enabled"] is False
    assert prov["models"][0]["tier"] == "T2"

    # SSJ batch tag is one of the four allowed values (SSJ-15)
    assert summary["ssj_tag"].startswith("SSJ-GUARDRAIL:")

    # analysis actually ran: TPS + frequency rows in the report
    tps = (batch_dir / "tps.csv").read_text(encoding="utf-8")
    assert "Constitution" in tps and "occurrence" in tps.lower()

    # negative-marking advice reached the report (FR-NM)
    assert "ATTEMPT" in report or "E(attempt)" in report

    # backend was released after the run (NFR-P-07)
    assert fake_backend["last"].unloaded is True


def test_mcq_batch_without_model_fails_gracefully(nandaka_home):
    """No GGUF + no cloud => honest error with an actionable hint (FR-IN-06)."""
    cfg = nandaka_home
    summary = run_mcq_batch(cfg, {"topic": "Constitution"}, count=3,
                             offline=True)
    assert summary["ok"] is False
    assert "no GGUF model selected" in summary["error"]
    assert "Drop a GGUF" in summary["hint"] or "--cloud" in summary["hint"]


def test_mcq_batch_images_off(nandaka_home, fake_backend, tmp_path):
    cfg = nandaka_home
    papers = make_papers_csv(tmp_path / "past.csv")
    summary = run_mcq_batch(cfg, {"topic": "Constitution"}, count=3,
                             papers_csv=str(papers), offline=True,
                             images="off")
    assert summary["ok"] is True
    batch_dir = Path(summary["batch_dir"])
    images_csv = (batch_dir / "images.csv").read_text(encoding="utf-8")
    # header only: nothing generated when images are disabled
    assert len(images_csv.strip().splitlines()) <= 1


# --------------------------------------------------------------------------- #
# Drafting pipelines (FR-SB / FR-AB)
# --------------------------------------------------------------------------- #


def test_run_chapter_offline(nandaka_home, fake_backend):
    cfg = nandaka_home
    spec = {"title": "Fundamental Rights", "class": "8",
            "subject": "Civics", "target_words": 600,
            "outcomes": ["Explain equality before law"]}
    summary = run_chapter(cfg, spec, formats=("md", "txt", "pdf"))
    assert summary["ok"] is True, summary.get("error")
    assert summary["sections"] == 5  # all SECTIONS produced

    batch_dir = Path(summary["batch_dir"])
    for fmt in ("md", "txt", "pdf"):
        assert (batch_dir / f"chapter.{fmt}").exists()
    assert (batch_dir / "provenance.json").exists()
    assert (batch_dir / "review-queue.csv").exists()
    assert (batch_dir / "ssj-report.json").exists()

    md = (batch_dir / "chapter.md").read_text(encoding="utf-8")
    assert md.startswith("# Fundamental Rights")
    assert "## Worked examples" in md

    prov = json.loads((batch_dir / "provenance.json").read_text(encoding="utf-8"))
    assert prov["tier"] == "T2"
    assert len(prov["sections"]) == 5
    assert fake_backend["last"].unloaded is True


def test_run_book_offline(nandaka_home, fake_backend):
    cfg = nandaka_home
    outline = {
        "title": "Indian Polity Primer",
        "chapters": [
            {"title": "The Constitution", "sections": [
                {"title": "Origins", "words": 300},
                {"title": "Key features", "words": 300}]},
            {"title": "Rights and Duties", "sections": [
                {"title": "Fundamental rights", "words": 300}]},
        ],
    }
    summary = run_book(cfg, outline)
    assert summary["ok"] is True, summary.get("error")
    assert summary["chapters"] == 2

    batch_dir = Path(summary["batch_dir"])
    assert (batch_dir / "book.md").exists()
    assert (batch_dir / "book.pdf").exists()
    assert (batch_dir / "provenance.json").exists()
    book = (batch_dir / "book.md").read_text(encoding="utf-8")
    assert "The Constitution" in book and "Rights and Duties" in book


def test_run_chapter_without_model(nandaka_home):
    cfg = nandaka_home
    summary = run_chapter(cfg, {"title": "X"})
    assert summary["ok"] is False
    assert "no GGUF model selected" in summary["error"]
