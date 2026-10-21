"""Sahaja Soul Jatka (SSJ) guardrails (§3.2, CO-06).

Deterministic, offline, zero-cost rule engine applied to EVERY output
segment (text and image metadata). The engine:

  * classifies segments into the nine SSJ-04 evidence labels,
  * runs category detectors (fatalism, malpractice, discrimination,
    privacy, authority, copyright, imagery),
  * emits exactly one SSJ-GUARDRAIL tag per output (SSJ-15),
  * applies the SSJ-13 precedence order when rules conflict,
  * queues uncertain items as REVIEW REQUIRED (SSJ-14) with reason/evidence.

SSJ rules override user preference and cannot be disabled by config.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# Tags and labels (SSJ-04, SSJ-15)
# --------------------------------------------------------------------------- #

PASS = "SSJ-GUARDRAIL: PASS"
REVIEW = "SSJ-GUARDRAIL: REVIEW REQUIRED"
EXCLUDED = "SSJ-GUARDRAIL: EXCLUDED"
NA = "SSJ-GUARDRAIL: NOT APPLICABLE"
ALL_TAGS = (PASS, REVIEW, EXCLUDED, NA)

EVIDENCE_LABELS = (
    "verified fact", "source-grounded fact", "statistical inference",
    "prediction", "spiritual belief", "cultural tradition", "metaphor",
    "personal opinion", "image evidence",
)

# SSJ-13 precedence: safety > evidence > preference
PRECEDENCE = ["safety", "evidence", "preference"]


# --------------------------------------------------------------------------- #
# Detectors
# --------------------------------------------------------------------------- #


@dataclass
class Finding:
    category: str
    severity: str          # exclusion | review | note
    rule: str              # SSJ-xx
    reason: str
    evidence: str = ""     # matched snippet
    action: str = ""


def _rx(pattern: str, flags=re.I) -> re.Pattern:
    return re.compile(pattern, flags)


_DETECTIONS: list[tuple[str, str, str, re.Pattern, str]] = [
    # category, severity, rule, pattern, reason template
    ("fatalism", "exclusion", "SSJ-05",
     _rx(r"\b(jatka|jatakam|horoscope|astrolog\w+|zodiac|kundli|kundali|"
         r"birth\s*chart|palmistry|numerology\W*prediction)\b"
         r"|\b(destined|cursed|blessed|fixed|guaranteed)\s+(?:by|through|via)?\s*"
         r"(?:stars?|fate|destiny|karma|planets?|jatka)\b"
         r"|\b(?:stars?|fate|destiny|karma|jatka)\s+(?:decides?|controls?|guarantees?|ensures?)\b"
         r"|జాతకం|జన్మ రాశి|అదృష్టం|గ్రహ స్థితి|కర్మ ఫలితం"),
     "content links outcomes to jatka/astrology/fate (SSJ-05 prohibits "
     "claiming exam results, rank, health, wealth or life events are fixed by stars/karma)"),
    ("malpractice", "exclusion", "SSJ-11",
     _rx(r"\b(leaked?\s+(?:paper|questions?|answer\s*keys?)|"
         r"exam\s+leak\w*|live\s+exam\s+(?:questions?|answers?)|"
         r"proxy\s+(?:candidate|exam)|impersonat\w+|bypass\w*\s+(?:exam|security)|"
         r"unauthori[sz]ed\s+answer\s*keys?|secure\s+exam\s+server)\b"),
     "assists exam malpractice, impersonation, leaked papers or live-exam "
     "reconstruction (SSJ-11, CO-07, X1)"),
    ("discrimination", "exclusion", "SSJ-08",
     _rx(r"\b(upper\s+caste\s+(?:are\s+)?(?:superior|better|deserving)|"
         r"lower\s+caste\s+(?:are\s+)?(?:inferior|lesser|dirty)|untouchable|"
         r"(?:all|every)\s+(?:muslims?|hindus?|christians?|tribals?|"
         r"sikhs?|dalits?)\s+are\s+(?:terrorists?|criminals?|lazy|dishonest)|"
         r"(?:women|girls)\s+(?:cannot|can't|should not)\s+\w+|"
         r"(?:disabled|handicapped)\s+(?:people\s+)?(?:cannot|can't)\s+)"),
     "content discriminates by caste, religion, gender or disability (SSJ-08)"),
    ("privacy", "exclusion", "SSJ-06",
         _rx(r"\b(birth\s*(?:date|time|place|details)|horoscope|kundli|caste|"
         r"religion|disability|diagnosis|medical\s+history)\b\s*(?:of\s+the\s+)?(?:candidate|person|user)\b"
         r"|please\s+share\s+(?:your|the)\s+(?:horoscope|birth\s+details|caste)"),
     "requests or exposes sensitive personal data (SSJ-06/07, X3)"),
    ("authority", "review", "SSJ-10",
         _rx(r"\b(take\s+(?:this|these)\s+(?:medicine|drug|tablet)|"
         r"you\s+(?:have|have got)\s+(?:depression|diabetes|malaria)|"
         r"(?:guaranteed|sure-shot)\s+cure|legal\s+advice:\s*|"
         r"invest\s+(?:all|everything)\s+(?:in|on))\b"),
     "medical/psychiatric/legal/financial authority claimed (SSJ-10: refer to "
     "qualified professionals)"),
    ("copyright", "review", "SSJ-12",
         _rx(r"\b(from\s+the\s+(?:paid|premium)\s+(?:material|course|book)|"
         r"reproduce\s+(?:the\s+)?(?:full|entire)\s+(?:chapter|book|paper)|"
         r"copy\s+(?:the\s+)?whole\s+page)\b"),
     "possible copyrighted reproduction beyond fair use (SSJ-12, CO-08)"),
    ("spiritual_in_exam", "review", "SSJ-09",
         _rx(r"\b(answer|correct\s+option)\b.{0,80}\b(god|prayer|faith|divine|"
         r"spiritual\s+practice|puja|mantra)\b"),
     "spiritual content inside exam material — must be exam-relevant, "
     "source-grounded and explicitly labelled (SSJ-09)"),
    ("misleading_imagery", "exclusion", "IR-09",
         _rx(r"\b(actually\s+photograph(?:ed)?\s+in|real\s+photo(?:graph)?\s+of\s+"
         r"|official\s+(?:government\s+)?figure\s+showing\s+)"
         r"|\[pass(?:ed)?\s+off\s+as\s+authentic\]"),
     "misleading imagery claim: AI-generated image presented as real/"
     "photographic/official (IR-09, X4)"),
    ("image_rights", "review", "IR-04",
         _rx(r"\blicen[cs]e\s*[:=]\s*(unknown|unclear|n/?a|none)\b"
         r"|\b(rights|permission)\s*[:=]\s*(unknown|unclear)\b"),
     "image licence unclear — IMAGE-RIGHTS: REVIEW REQUIRED (IR-04)"),
    ("image_safety", "exclusion", "IR-13",
         _rx(r"\b(evil[- ]eye\s+diagram|horoscope\s+chart\s+image|"
         r"nadi\s+astrology\s+figure|graphically?\s+(?:violent|bloody)|"
         r"scary\s+(?:demon|ghost)\s+figure)\b"),
     "frightening/violent/superstitious imagery request (IR-13, X2)"),
    ("political", "review", "IR-15",
         _rx(r"\b(vote\s+for|party\s+manifesto\s+poster|election\s+campaign\s+"
         r"(?:image|poster)|political\s+propaganda)\b"),
     "political/election-related imagery or content (IR-15)"),
]

_UNKNOWN = _rx(r"\b(might|maybe|perhaps|they say|it is said|believed to be|"
               r"according to some|guaranteed|100%|always works)\b")


# --------------------------------------------------------------------------- #
# Core API
# --------------------------------------------------------------------------- #


@dataclass
class SSJResult:
    tag: str
    findings: list[Finding] = field(default_factory=list)
    evidence_label: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def excluded(self) -> bool:
        return self.tag == EXCLUDED

    @property
    def needs_review(self) -> bool:
        return self.tag == REVIEW

    def as_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "evidence_label": self.evidence_label,
            "findings": [vars(f) for f in self.findings],
            "notes": self.notes,
        }


def check_text(text: str, *, declared_label: str = "",
               allow_review: bool = True) -> SSJResult:
    """Apply SSJ rules to one text segment (SSJ-01..SSJ-15)."""
    findings: list[Finding] = []
    for category, severity, rule, pattern, reason in _DETECTIONS:
        m = pattern.search(text or "")
        if m:
            findings.append(Finding(
                category=category, severity=severity, rule=rule, reason=reason,
                evidence=(m.group(0)[:120]),
                action=("do not output; offer safe labelled alternative"
                        if severity == "exclusion"
                        else "queue for human review with evidence"),
            ))

    # SSJ-04: evidence labelling — infer if not declared, then validate.
    label = declared_label if declared_label in EVIDENCE_LABELS else _infer_label(text)
    if declared_label and declared_label not in EVIDENCE_LABELS:
        findings.append(Finding("evidence_label", "review", "SSJ-04",
                                f"unknown evidence label '{declared_label}'",
                                action="use one of the nine SSJ-04 labels"))
    if not declared_label:
        label = label  # inferred label is recorded, not flagged

    # SSJ-14: uncertainty without a finding still lands in review.
    uncertain = bool(_UNKNOWN.search(text or "")) and len(text or "") > 40
    if uncertain and allow_review:
        findings.append(Finding("uncertainty", "review", "SSJ-14",
                                "hedged/uncertain claim detected",
                                evidence=_UNKNOWN.search(text).group(0),
                                action="verify against source or label explicitly"))

    exclusions = [f for f in findings if f.severity == "exclusion"]
    reviews = [f for f in findings if f.severity == "review"]
    if exclusions:
        tag = EXCLUDED
    elif reviews:
        tag = REVIEW
    elif not (text or "").strip():
        tag = NA
    else:
        tag = PASS

    # SSJ-13 precedence is inherent: any exclusion outranks any review
    # outranks any pass, because safety precedes evidence precedes preference.
    notes = [] if tag == PASS else ["SSJ-13 precedence applied: "
                                    + " > ".join(PRECEDENCE)]
    return SSJResult(tag=tag, findings=findings, evidence_label=label, notes=notes)


def _infer_label(text: str) -> str:
    """Best-effort SSJ-04 evidence label for unlabelled segments."""
    t = (text or "").strip()
    if not t:
        return ""
    if re.search(r"\b(source|according to (?:the )?(?:textbook|ncert|report|act|census)\b|"
                 r"chapter \d+|section \d+)", t, re.I):
        return "source-grounded fact"
    if re.search(r"\b(\d+(\.\d+)?%|average|mean|median|survey|statistics)\b", t, re.I):
        return "statistical inference"
    if re.search(r"\b(will happen|likely to|expected to|may appear|forecast)\b", t, re.I):
        return "prediction"
    if re.search(r"\b(horoscope|astrology|jatka|karma|stars|divine|god's will)\b", t, re.I):
        return "spiritual belief"
    if re.search(r"\b(festival|tradition|customary|as per tradition)\b", t, re.I):
        return "cultural tradition"
    if re.search(r"\b(as if|like a|metaphorically)\b", t, re.I):
        return "metaphor"
    if re.search(r"\b(?:is|are|was|were)\s+(?:the\s+)?(?:capital|defined\s+as|"
                 r"equal\s+to|located|composed\s+of|founded|established|adopted|"
                 r"enacted|ratified|created|discovered|invented)\b|\bwas\s+\w+\s+in\s+"
                 r"\d{4}\b|\bis\s+equal\s+to\b", t, re.I):
        return "verified fact"
    return "personal opinion"


def check_image(meta: dict[str, Any]) -> SSJResult:
    """Apply SSJ + IR rules to image metadata (IR-09..IR-15, §3.8)."""
    blob = " ".join(str(v) for v in meta.values())
    res = check_text(blob)
    evidence = str(meta.get("IMAGE-EVIDENCE", meta.get("evidence", ""))) or None
    rights = str(meta.get("IMAGE-RIGHTS", meta.get("licence", ""))).lower()

    extra: list[Finding] = []
    if not evidence:
        extra.append(Finding("image_evidence", "review", "IR-02/03",
                             "missing IMAGE-EVIDENCE label",
                             action="label SOURCE-DERIVED / AI-GENERATED / ILLUSTRATIVE"))
    if rights in ("", "unknown", "unclear", "n/a", "none"):
        extra.append(Finding("image_rights", "review", "IR-04",
                             "licence unclear — IMAGE-RIGHTS: REVIEW REQUIRED",
                             action="clear rights before publishing; exclude from final PDF"))
    if str(meta.get("generated", "")).lower() in ("1", "true", "yes") and not evidence:
        extra.append(Finding("misleading_imagery", "review", "IR-09",
                             "generated image lacks evidence label",
                             action="add visible 'AI-generated' caption"))
    res.findings.extend(extra)
    if any(f.severity == "exclusion" for f in res.findings):
        res.tag = EXCLUDED
    elif res.findings and res.tag == PASS:
        res.tag = REVIEW
    return res


def overall_tag(results: list[SSJResult]) -> str:
    """Exactly one tag per output (SSJ-15): EXCLUDED > REVIEW > PASS > NA."""
    tags = [r.tag for r in results if r.tag]
    if EXCLUDED in tags:
        return EXCLUDED
    if REVIEW in tags:
        return REVIEW
    if PASS in tags:
        return PASS
    return NA


# --------------------------------------------------------------------------- #
# Compliance report (§3.8 schema)
# --------------------------------------------------------------------------- #

REPORT_CHECKS = [
    "Dignity and non-discrimination",
    "Privacy and consent",
    "No fatalism or fear prediction",
    "Evidence labelling of spiritual/jatka claims",
    "No medical/legal/financial/psychiatric advice",
    "Anti-malpractice compliance",
    "Cultural humility",
    "Image appropriateness and representation",
    "Image rights and attribution",
    "No misleading AI-generated imagery",
    "Human review completed where needed",
]

_CATEGORY_TO_CHECK = {
    "discrimination": "Dignity and non-discrimination",
    "privacy": "Privacy and consent",
    "fatalism": "No fatalism or fear prediction",
    "evidence_label": "Evidence labelling of spiritual/jatka claims",
    "spiritual_in_exam": "Evidence labelling of spiritual/jatka claims",
    "authority": "No medical/legal/financial/psychiatric advice",
    "malpractice": "Anti-malpractice compliance",
    "copyright": "Cultural humility",
    "uncertainty": "Human review completed where needed",
    "image_safety": "Image appropriateness and representation",
    "misleading_imagery": "No misleading AI-generated imagery",
    "image_rights": "Image rights and attribution",
    "image_evidence": "Image appropriateness and representation",
    "political": "Image appropriateness and representation",
}


def compliance_report(results: list[SSJResult]) -> list[dict[str, str]]:
    """Build the §3.8 SSJ compliance report rows."""
    rows: dict[str, dict[str, str]] = {
        name: {"check": name, "status": "PASS", "evidence": "", "action": ""}
        for name in REPORT_CHECKS
    }
    for res in results:
        for f in res.findings:
            check = _CATEGORY_TO_CHECK.get(f.category)
            if not check:
                continue
            row = rows[check]
            if f.severity == "exclusion":
                row["status"] = "EXCLUDED"
            elif f.severity == "review" and row["status"] != "EXCLUDED":
                row["status"] = "REVIEW"
            row["evidence"] = (row["evidence"] + " | " if row["evidence"] else "") + \
                              f"[{f.rule}] {f.evidence or f.reason}"
            row["action"] = f.action or row["action"]
    # human review completion is asserted by the HIL module, not inferred here
    return list(rows.values())
