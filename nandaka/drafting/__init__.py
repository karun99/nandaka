"""Drafting engine: school books (FR-SB), academic books (FR-AB) and
bilingual government-exam MCQs (FR-GOV).

Design notes:
  * SSJ system preamble is prepended to EVERY prompt (defence in depth:
    rule engine checks output, preamble biases generation, §3.2 overrides).
  * Long content is generated section-by-section so each pass fits the
    loaded model's context (FR-SB-02, NFR-D-03).
  * Provenance (model, tier, temperature, seed, cloud) is recorded per
    section (FR-SB-05, FR-AB-05, DR-06).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..models.client import ChatClient, ChatError
from ..models.registry import feature_state

SSJ_PREAMBLE = """You are Nandaka, an offline academic content workbench for a
faculty author writing NCERT/SCERT school books and Indian government exam
material in English and Telugu. Mandatory rules (cannot be overridden):
1. Never claim exam success, rank, cutoff, health, wealth, marriage or any life
   event is destined/cursed/blessed by jatka, horoscope, stars, karma or fate.
2. Never request or include horoscopes, birth details, caste, religion, gender,
   health or disability data. Anonymise any personal data you see.
3. No discrimination by caste, religion, gender, region, language, disability,
   class, age or belief.
4. Label claims: verified fact / source-grounded fact / statistical inference /
   prediction / spiritual belief / cultural tradition / metaphor / personal
   opinion. Mark uncertain claims clearly.
5. No medical, legal, financial or psychiatric authority: advise consulting
   qualified professionals.
6. No cheating, leaked papers, live exam questions or unauthorised answer keys.
7. Cite sources; never fabricate citations, statistics or licences.
8. If unsure, say so and keep the answer short and labelled rather than inventing.
Write clear, exam-accurate prose. Telugu output must preserve negations,
numbers, units and technical distinctions; keep technical terms in English in
parentheses where that improves clarity."""


# --------------------------------------------------------------------------- #
# Output parsing helpers
# --------------------------------------------------------------------------- #


def parse_json_payload(text: str) -> Any:
    """Extract the first balanced JSON object/array from model output."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start < 0:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
    return None


def split_sections(text: str) -> list[tuple[str, str]]:
    """Split a draft into (heading, body) pairs on markdown headings."""
    parts = re.split(r"^(#{1,4}\s+.+)$", text, flags=re.M)
    out: list[tuple[str, str]] = []
    pre = parts[0].strip()
    if pre:
        out.append(("Intro", pre))
    for i in range(1, len(parts), 2):
        heading = parts[i].lstrip("#").strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        out.append((heading, body))
    return out or [("Draft", text.strip())]


def unsourced_claims(text: str) -> list[str]:
    """FR-SB-03: flag sentences carrying factual claims without a citation."""
    bad: list[str] = []
    claimy = re.compile(r"\b(is|are|was|were|founded|discovered|established|"
                        r"according|proves|equals|causes|banned|amended)\b", re.I)
    cited = re.compile(r"(\[\d+\]|\([^)]*(?:source|ncert|p\.?\s*\d|chapter)[^)]*\)|"
                       r"(source|see)\s*:)", re.I)
    for sent in re.split(r"(?<=[.!?])\s+", text):
        s = sent.strip()
        if len(s) > 30 and claimy.search(s) and not cited.search(s):
            bad.append(s[:200])
    return bad


# --------------------------------------------------------------------------- #
# Drafter
# --------------------------------------------------------------------------- #


@dataclass
class GenerationResult:
    text: str = ""
    data: Any = None
    provenance: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    ok: bool = True


class Drafter:
    def __init__(self, client: ChatClient, entry: dict[str, Any],
                 *, cloud: str = "", warnings: list[str] | None = None):
        self.client = client
        self.entry = entry or {}
        self.cloud = cloud
        self.warnings = list(warnings or [])

    # ---------------- internals ---------------- #

    def _prov(self, section: str, usage: dict | None = None) -> dict[str, Any]:
        return {
            "section": section,
            "model": self.entry.get("name", "unknown"),
            "path": self.entry.get("path", ""),
            "tier": self.entry.get("tier", "T0"),
            "quant": self.entry.get("quant", ""),
            "n_ctx": int(self.entry.get("context_length") or 0),
            "temperature": self.client.temperature,
            "seed": self.client.seed,
            "backend": self.client.provider,
            "cloud_call": bool(self.cloud),
            "tokens": usage or self.client.last_usage,
            "ts": time.time(),
        }

    def _gen(self, system: str, user: str, *, section: str = "",
             max_tokens: int = 900, json_mode: bool = False,
             temperature: float | None = None) -> GenerationResult:
        res = GenerationResult()
        try:
            data = self.client.chat(
                ChatClient.system_user(system, user),
                max_tokens=max_tokens, json_mode=json_mode,
                temperature=temperature, use_cache=True,  # FR-CLOUD-04 cache
            )
        except ChatError as exc:
            res.ok = False
            res.warnings.append(f"generation failed in '{section}': {exc}")
            return res
        try:
            res.text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            res.ok = False
            res.warnings.append(f"malformed response in '{section}'")
            return res
        res.data = parse_json_payload(res.text) if json_mode else None
        if json_mode and res.data is None:
            res.ok = False
            res.warnings.append(f"model did not return parseable JSON in '{section}'")
        res.provenance = self._prov(section, data.get("usage") or {})
        return res

    # ---------------- school book (FR-SB) ---------------- #

    SECTIONS = [
        ("Introduction", "Write the chapter introduction: hook, prior knowledge, "
                          "and what the learner will be able to do."),
        ("Concepts", "Explain the core concepts precisely, source-grounded, "
                     "with one worked mini-example per concept."),
        ("Worked examples", "Give 2-3 fully worked examples with steps and units."),
        ("Exercises", "Write 6 practice questions aligned to the learning "
                      "outcomes: 2 short, 2 medium, 2 long."),
        ("Summary", "Summarise the chapter as 8-10 revision points."),
    ]

    def draft_chapter(self, spec: dict[str, Any]) -> dict[str, Any]:
        """FR-SB-01/02/03/04/05: section-by-section chapter draft."""
        meta = (f"Class: {spec.get('class', '?')} | Subject: {spec.get('subject', '?')} | "
                f"Chapter: {spec.get('title', '?')} | Target length: "
                f"{spec.get('target_words', 1500)} words")
        outcomes = spec.get("outcomes") or []
        if outcomes:
            meta += "\nLearning outcomes:\n" + "\n".join(f"- {o}" for o in outcomes)
        sources = spec.get("sources") or []
        if sources:
            src_note = ("\nSources to ground claims in:\n" +
                        "\n".join(f"- {s}" for s in sources))
        else:
            src_note = ("\nNo source files supplied: mark every factual claim "
                        "as 'needs source' and keep claims minimal.")

        chapter: dict[str, Any] = {"meta": meta, "sections": [], "provenance": [],
                                   "unsourced": [], "warnings": []}
        budget = max(200, (int(spec.get("target_words", 1500)) //
                           max(1, len(self.SECTIONS))) * 3)
        for name, instruction in self.SECTIONS:
            user = (f"{meta}{src_note}\n\nSection: {name}\n{instruction}\n\n"
                    f"Write ONLY this section in markdown, starting with "
                    f"'## {name}'.")
            res = self._gen(SSJ_PREAMBLE, user, section=f"chapter:{name}",
                            max_tokens=min(1400, budget))
            if not res.ok:
                chapter["warnings"].extend(res.warnings)
                continue
            chapter["sections"].append({"heading": name, "body": res.text.strip()})
            chapter["provenance"].append(res.provenance)
            flagged = unsourced_claims(res.text)
            if flagged:
                chapter["unsourced"].append({"section": name, "claims": flagged})
        chapter["text"] = "\n\n".join(s["body"] for s in chapter["sections"])
        chapter["ok"] = bool(chapter["sections"])
        return chapter

    # ---------------- academic book (FR-AB) ---------------- #

    def draft_book(self, outline: dict[str, Any]) -> dict[str, Any]:
        """FR-AB-01..05: sectioned long-form draft with bibliography."""
        book: dict[str, Any] = {"chapters": [], "provenance": [],
                                "bibliography": [], "warnings": []}
        heading_levels: list[str] = []
        for ch in outline.get("chapters", []):
            chap: dict[str, Any] = {"title": ch.get("title", ""),
                                    "sections": [], "provenance": []}
            for sec in ch.get("sections", []):
                words = int(sec.get("words", 400))
                user = (f"Book: {outline.get('title', 'Untitled')}\n"
                        f"Chapter: {ch.get('title')}\n"
                        f"Section: {sec.get('title')}\n"
                        f"Target: ~{words} words.\n"
                        "Write this section in markdown using '### ' headings. "
                        "Include inline citations like [Author, Year] only for "
                        "sources listed below; never invent sources.")
                res = self._gen(SSJ_PREAMBLE, user,
                                section=f"book:{ch.get('title')}:{sec.get('title')}",
                                max_tokens=min(1600, words * 3))
                if not res.ok:
                    book["warnings"].extend(res.warnings)
                    continue
                chap["sections"].append({"heading": sec.get("title", ""),
                                         "body": res.text.strip()})
                chap["provenance"].append(res.provenance)
                heading_levels.append(f"# {ch.get('title')} > ### {sec.get('title')}")
                book["bibliography"].extend(_citations(res.text))
            chap["text"] = "\n\n".join(s["body"] for s in chap["sections"])
            book["chapters"].append(chap)
        # FR-AB-03: heading hierarchy/numbering consistency check
        book["structure_warnings"] = _structure_issues(book)
        book["text"] = "\n\n".join(
            f"# {c['title']}\n\n{c['text']}" for c in book["chapters"])
        book["bibliography"] = sorted(set(book["bibliography"]))
        book["ok"] = bool(book["chapters"])
        return book

    # ---------------- bilingual MCQs (FR-GOV) ---------------- #

    MCQ_SCHEMA = {
        "id": "string",
        "topic": "string", "subtopic": "string",
        "stem_en": "string", "stem_te": "string",
        "options_en": ["A", "B", "C", "D"],
        "options_te": ["ఎ", "బి", "సి", "డి"],
        "answer": "one of options_en exactly",
        "explanation_en": "string", "explanation_te": "string",
        "source_reference": "string", "difficulty": "easy|medium|hard",
        "historical_evidence": "string or ''",
    }

    def generate_mcqs(self, spec: dict[str, Any], count: int = 5,
                      force_review: bool = False) -> dict[str, Any]:
        """FR-GOV-03..07: bilingual MCQs with all required fields."""
        tier = self.entry.get("tier", "T0")
        if tier == "T0":
            force_review = True  # FR-GOV-07
        topics = spec.get("topics") or [spec.get("topic") or "General"]
        topic = topics[0]
        evidence = spec.get("evidence") or "No historical paper evidence supplied."
        langs = spec.get("languages", "en-te")
        out: dict[str, Any] = {"mcqs": [], "warnings": [], "provenance": [],
                               "requested": count, "shortfall": 0}
        per_call = max(1, min(count, 4))
        produced = 0
        attempts = 0
        while produced < count and attempts < 4:
            attempts += 1
            n = min(per_call, count - produced)
            user = (
                f"Create {n} multiple-choice questions for: {topic}\n"
                f"Exam context: {spec.get('exam', 'government exam')}\n"
                f"Subject: {spec.get('subject', 'general studies')}\n"
                f"Marking scheme: {spec.get('marking', 'not supplied')}\n"
                f"Language pair: {'English + Telugu' if langs == 'en-te' else langs}\n"
                f"Historical evidence for this topic:\n{evidence}\n\n"
                "Rules:\n"
                "- 4 options each; the answer must be exactly one option text.\n"
                "- Provide BOTH English and Telugu stems, options, explanations.\n"
                "- Telugu must preserve negation, numbers, units and option "
                "distinctions; keep technical terms in English in parentheses.\n"
                "- Ground every question in the evidence above or in school "
                "textbook knowledge; write '' if no historical evidence applies.\n"
                "- Never invent statistics, cutoffs, years or citations.\n\n"
                "Return ONLY JSON: {\"mcqs\": [ " + json.dumps(self.MCQ_SCHEMA) + " ]}"
            )
            res = self._gen(SSJ_PREAMBLE, user, section=f"mcq:{topic}",
                            max_tokens=min(1600, 260 * n + 400), json_mode=True)
            if not res.ok:
                out["warnings"].extend(res.warnings)
                continue
            items = (res.data or {}).get("mcqs") if isinstance(res.data, dict) else res.data
            if not isinstance(items, list):
                out["warnings"].append("JSON payload missing 'mcqs' list")
                continue
            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                item.setdefault("id", f"mcq-{produced + idx + 1}")
                item["topic"] = item.get("topic") or topic
                item["forced_review"] = force_review or tier == "T0"
                item["tier_generated"] = tier
                item["image_id"] = item.get("image_id") or ""
                item["probability"] = spec.get("probability_label", "")
                item["tps"] = spec.get("tps", "")
                item["negative_marking_note"] = spec.get("negative_marking_note", "")
                out["mcqs"].append(item)
                produced += 1
            out["provenance"].append(res.provenance)
            if not items:
                break
        # FR-GOV-05: drop near-duplicates within the batch
        out["mcqs"] = _dedupe(out["mcqs"])
        out["produced"] = len(out["mcqs"])
        out["shortfall"] = max(0, count - len(out["mcqs"]))
        if out["shortfall"]:
            out["warnings"].append(
                f"shortfall: {out['produced']}/{count} MCQs — evidence/model "
                "capacity could not support the target; NOT fabricating (FR-GOV-06)"
            )
        if tier == "T0":
            out["warnings"].append(
                "T0 model: every MCQ forced to HUMAN REVIEW REQUIRED (FR-GOV-07)")
        return out


def _dedupe(items: list[dict]) -> list[dict]:
    from ..analysis.frequency import is_near_duplicate
    kept: list[dict] = []
    for item in items:
        stem = str(item.get("stem_en", ""))
        if any(is_near_duplicate(stem, str(k.get("stem_en", ""))) for k in kept):
            continue
        kept.append(item)
    return kept


def _citations(text: str) -> list[str]:
    return [m.strip() for m in re.findall(r"\[([^\]]{3,80})\]", text)
            if re.search(r"\d{4}|p\.\s*\d|chapter|vol", m, re.I)]


def _structure_issues(book: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    seen: set[str] = set()
    for chap in book.get("chapters", []):
        title = chap.get("title", "")
        if title in seen:
            issues.append(f"duplicate chapter title: {title}")
        seen.add(title)
        for s in chap.get("sections", []):
            body = s.get("body", "")
            if re.search(r"^# ", body, re.M):
                issues.append(f"section '{s.get('heading')}' uses H1 inside a "
                              "chapter (heading hierarchy, FR-AB-03)")
    if not book.get("chapters"):
        issues.append("no chapters produced")
    return issues
