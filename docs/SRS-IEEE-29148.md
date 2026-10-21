# Software Requirements Specification — NANDAKA

**Standard:** IEEE/IEC 29148:2018 (Systems and software engineering — Life
cycle processes — Requirements engineering), style: SRS structure of
§5.3 (information-content items).
**Project:** Nandaka — offline-first, GGUF-agnostic academic content and
exam-preparation workbench.
**Version:** 1.0.0 · **Status:** Baselined for release
**Stakeholders:** faculty authors (NCERT/SCERT), government-exam aspirants,
reviewers, maintainers.

> Every requirement carries a stable ID. Implementation and tests reference
> these IDs directly (e.g. `FR-CLOUD-01`, `NFR-P-07`, `TC-16`), so this
> document is the single source of truth for scope and the traceability
> target for the test suite in `tests/`.

---

## 1. Scope

### 1.1 Problem

Academic authors preparing school textbooks and Indian government-exam
material must: assemble sources, measure which topics actually recur in past
papers, generate bilingual (English + Telugu) practice material, verify it,
and export reviewable artefacts — while keeping human judgement in the loop
and avoiding superstitious, discriminatory or harmful content.

Existing tools assume a paid cloud LLM, a large machine, or both. Field use
(phone, intermittent or no connectivity) breaks them.

### 1.2 Context

Nandaka sits between **local GGUF models** (llama.cpp / Ollama), **official
education portals** (NCERT, DIKSHA, state SCERTs), **past-paper data** the
user supplies, and **human reviewers**. An optional, disabled-by-default cloud
path (NVIDIA NIM, OpenRouter) exists only as a fallback.

### 1.3 Goals (business/mission)

| # | Goal |
| --- | --- |
| G1 | Produce exam-accurate bilingual content without requiring cloud access |
| G2 | Ground prioritisation in measured historical evidence, not intuition |
| G3 | Keep a human accountable for every shipped item (queue, never silence) |
| G4 | Run unmodified on Termux and on Linux servers |
| G5 | Be model-agnostic: adding a GGUF is a file drop, never a code change |

### 1.4 Out of scope

Photographic image generation, live exam/leak access, unmoderated
auto-publishing, mobile GUI, GPU-cluster scheduling.

---

## 2. Overall description

### 2.1 Product perspective

A command-line workbench (`nandaka` console script) plus a Python package.
No daemon, no server component: the workflow is a pipeline executed per batch.

### 2.2 User classes

| Class | Needs |
| --- | --- |
| Author (primary) | generate, review, export; full control of sources |
| Reviewer | prioritised queue with evidence and required action |
| Aspirant (indirect) | accurate MCQ sets with explanations and provenance |
| Maintainer | clear module boundaries, stdlib-only core, testable offline |

### 2.3 Operating environment (CO-01, NFR-D-*)

- Python ≥ 3.10, POSIX (Linux, Android/Termux, macOS).
- **Core is stdlib-only** — no compiled dependency is required to install or
  run any core feature (CO-04).
- Optional, lazy: `pypdf` (PDF text extraction), `openpyxl` (advanced XLSX
  read). Absence is reported as a gap, never a crash (FR-IN-06).
- Local inference: `llama-server` from llama.cpp, or an Ollama endpoint.
- RAM budget configurable (default 900 MB); context length derives from it.

### 2.4 Design constraints

| ID | Constraint |
| --- | --- |
| CO-01 | Peak RSS must stay within the configured RAM budget |
| CO-03 | One download at a time; cache always wins over network |
| CO-04 | Offline is the default operating mode; network is optional |
| CO-06 | SSJ guardrails cannot be disabled by configuration |
| CO-08 | Secrets never printed, never logged, never committed |

### 2.5 Assumptions & dependencies (AD-01, AD-05)

- **AD-01** Exactly one GGUF in `models/` ⇒ auto-selected; zero or several ⇒
  an explicit `--model` is required.
- **AD-05** Portal access obeys `robots.txt`; violations are refused.
- Depends on `llama-server` availability for local generation; without it the
  product still analyses, validates, reports, and says so honestly.

---

## 3. Functional requirements

### 3.1 Ingestion (FR-IN-*)

| ID | Requirement | Tests |
| --- | --- | --- |
| FR-IN-01 | Read `.md`, `.txt`, `.pdf`, `.csv`, `.xlsx`, `.png/.jpg/.svg` | `test_ingest` |
| FR-IN-02 | Produce a source inventory (path, kind, chars, pages, tables, notes) | `test_ingest`, `test_cli` |
| FR-IN-03 | Portal connectors for NCERT, DIKSHA, AP SCERT, TG SCERT, YesOnlineBooks | `test_cli sources` |
| FR-IN-04 | Cache-first: cached content wins over the network | `test_cli sources --offline` |
| FR-IN-05 | Resumable, serial downloads (Range, `max_concurrent_downloads=1`) | — |
| FR-IN-06 | Missing input or failed fetch is reported as a gap and the run continues | `test_ingest`, `test_cli` |
| FR-IN-07 | CSV/XLSX readers support both shared strings and inline strings | `test_ingest` |

### 3.2 Model registry & tiers (FR-GGUF-*)

| ID | Requirement | Tests |
| --- | --- | --- |
| FR-GGUF-01 | Discover every `.gguf` under the model dir by file drop | `test_registry`, `test_cli` |
| FR-GGUF-03 | Read architecture, parameters, quant, context length, chat template | `test_gguf` |
| FR-GGUF-04 | Classify tiers: **T0 ≤ 0.6B**, **T1 0.6–2B** (low exclusive), **T2 2–8B**, **T3 > 8B** | `test_gguf` |
| FR-GGUF-05 | Derive `n_ctx` from the RAM budget (NFR-D-03) | `test_config` |
| FR-GGUF-06 | Reuse a warm server already hosting the requested model | — |
| FR-GGUF-09 | Capability probe (factual / reasoning / instruction) with per-item results | `test_cli models probe` |
| FR-GGUF-11 | Capability gate: `ok / partial / basic / off` per feature × tier; `off` refuses unless `--force`; `basic` forces human review | `test_registry` |
| FR-GGUF-12 | Integrity verification (magic, header, tensors, SHA-256) | `test_registry` |
| FR-GGUF-13 | Ollama backend via the same OpenAI dialect on `:11434` | `test_config` |
| FR-GGUF-14 | Registry persists at `registry.json` (mode 0600) and drops stale entries | `test_registry` |

**Feature matrix (§3.3.1.1):** `short_summary`, `short_draft`, `long_draft`,
`bilingual_te`, `reasoning_math`, `mcq`, `diagram_spec`.

### 3.3 Local execution (EX-10, NFR-P-*)

| ID | Requirement |
| --- | --- |
| EX-10 | One `llama-server` process serves one model; swapping unloads then reloads so peak RAM never exceeds the budget |
| NFR-P-07 | Model swap target ≤ 5 s; overruns are noted, never fatal |
| NFR-P-08 | Best-effort RSS reporting of the loaded process |

### 3.4 Historical analysis (FR-HA-*)

| ID | Requirement | Tests |
| --- | --- | --- |
| FR-HA-01 | Frequency table per topic: count, years covered, most recent, rewordings, diagram share | `test_analysis` |
| FR-HA-02 | Topic Priority Score (TPS) 0–100 from frequency, recency, syllabus relevance, kind weighting | `test_analysis` |
| FR-HA-03 | Scoring rules, windows and denominators disclosed in the report | `test_analysis`, `test_cli analyse` |
| FR-HA-04 | Distinct-paper counts (not raw question counts) as the probability basis | `test_analysis` |
| FR-HA-05 | Occurrence probability with Wilson confidence interval | `test_analysis` |
| FR-HA-06 | Below 5 papers or 2 hits ⇒ *"Not reliably estimable"* + provisional TPS — never a fabricated number | `test_analysis` |

### 3.5 Negative marking (FR-NM-*)

| ID | Requirement | Tests |
| --- | --- | --- |
| FR-NM-01 | Marking rules must actually be read (structured or free text) before advising | `test_analysis`, `test_cli` |
| FR-NM-02 | `E(attempt) = p·R − (1−p)·W`; `E(skip) = U`; compare and recommend | `test_analysis`, `test_cli` (TC-16) |
| FR-NM-03 | Break-even accuracy `p* = W/(R+W)` reported | `test_analysis` |
| FR-NM-04 | Unknown `p` ⇒ **decline** advice ("INSUFFICIENT DATA"), do not substitute historical frequency | `test_cli` |

### 3.6 Government-exam MCQs (FR-GOV-*)

| ID | Requirement | Tests |
| --- | --- | --- |
| FR-GOV-03 | Four options; answer is exactly one option text | `test_validation` |
| FR-GOV-04 | Bilingual EN+TE stems, options and explanations | `test_validation` |
| FR-GOV-05 | Near-duplicate filtering within the batch | `test_drafting`, `test_pipeline` |
| FR-GOV-06 | Shortfall is reported, never closed by fabrication | `test_pipeline` |
| FR-GOV-07 | T0-generated items are forced to *HUMAN REVIEW REQUIRED* | `test_drafting` |
| FR-GOV-08 | Provenance per item: model, tier, temperature, seed, backend, cloud flag | `test_pipeline` |

### 3.7 Drafting (FR-SB, FR-AB)

| ID | Requirement | Tests |
| --- | --- | --- |
| FR-SB-01 | Chapter drafting section-by-section so each pass fits `n_ctx` | `test_pipeline` |
| FR-SB-03 | Unsourced factual claims flagged with the sentence as evidence | `test_drafting`, `test_pipeline` |
| FR-SB-05 | Per-section provenance | `test_pipeline` |
| FR-AB-01 | Academic book: title > chapter > section structure from an outline | `test_pipeline` |
| FR-AB-03 | Heading-hierarchy consistency check | `test_drafting` |
| FR-AB-05 | Bibliography derived from real in-text citations only; no invented sources | `test_drafting` |

### 3.8 Images (IR-01 … IR-17)

| ID | Requirement | Tests |
| --- | --- | --- |
| IR-01 | Sourcing order: user-provided → generated diagram → structured description | `test_images` |
| IR-03 | Every image labelled with origin, evidence class and rights | `test_images` |
| IR-04 | Unclear licence ⇒ excluded from exports with an explicit disclosure | `test_images`, `test_pipeline` |
| IR-05 | Bilingual caption **and** bilingual alt-text required | `test_validation`, `test_images` |
| IR-07 | Size ceiling (`image_max_kb`) enforced | `test_images` |
| IR-16 | No photographic generation capability ⇒ disclose and provide a text brief instead | `test_images`, `test_pipeline` |
| IR-17 | Visual coverage metrics (topics with a figure, MCQs carrying an image) | `test_report` |

### 3.9 SSJ guardrails (§3.2, SSJ-01 … SSJ-15)

| ID | Requirement | Tests |
| --- | --- | --- |
| SSJ-01 | No fatalism: no claim that success/failure is destined by jatka, horoscope, stars, karma or fate | `test_ssj` |
| SSJ-02 | No horoscopes, birth details, caste, religion, gender, health or disability collection; personal data anonymised | `test_ssj` |
| SSJ-03 | No discrimination by caste, religion, gender, region, language, disability, class, age, belief | `test_ssj` |
| SSJ-04 | Claims labelled: verified fact / source-grounded / statistical inference / prediction / spiritual belief / cultural tradition / metaphor / opinion | `test_ssj` |
| SSJ-05 | No medical, legal, financial or psychiatric authority | `test_ssj` |
| SSJ-06 | No cheating, leaked papers, live exam questions or unauthorised keys | `test_ssj` |
| SSJ-07 | Citations required; never fabricate citations, statistics or licences | `test_ssj` |
| SSJ-10 | System preamble prepended to **every** LLM prompt (defence in depth) | `test_drafting` |
| SSJ-15 | Exactly one tag per output: `EXCLUDED > REVIEW > PASS > NOT APPLICABLE` | `test_ssj`, `test_pipeline` |
| §3.8 | Compliance report with one row per check and its action | `test_ssj`, `test_report` |

### 3.10 Validation & human-in-the-loop (FR-VAL-*, FR-HIL-*)

| ID | Requirement | Tests |
| --- | --- | --- |
| FR-VAL-01 | Battery: answer validity, language validity, ambiguity, source traceability, bilingual faithfulness (incl. negation + numbers), evidence reproducibility, syllabus alignment, negative-marking compliance, factual validity, currentness, duplication, images, SSJ | `test_validation` |
| FR-VAL-02 | Exactly one of four statuses: `VALIDATED`, `HUMAN REVIEW REQUIRED`, `NEEDS CORRECTION`, `EXCLUDED` | `test_validation` |
| FR-VAL-03 | Auto-checks never claim independent factual verification | `test_validation` |
| FR-HIL-01 | Prioritised review queue by harm × uncertainty × importance, each item with evidence and required action | `test_validation`, `test_pipeline` |

### 3.11 Cloud fallback (FR-CLOUD-*) — opt-in

| ID | Requirement | Tests |
| --- | --- | --- |
| FR-CLOUD-01 | **Disabled by default**; enabled only by explicit flag/env (`--cloud`, `NANDAKA_CLOUD=1`) | `test_cloud`, `test_config` |
| FR-CLOUD-02 | Token budget; a request that would exceed it is refused **before** any network call | `test_cloud` |
| FR-CLOUD-04 | Response cache keyed on the payload; identical requests cost no network and survive restarts | `test_cloud` |
| FR-CLOUD-05 | Every call (success or failure) appended to `cloud-audit.jsonl`; API keys never written | `test_cloud` |
| FR-CLOUD-06 | Missing credentials never break a run — print, continue offline | `test_cloud` |
| FR-CLOUD-07 | Providers are **NVIDIA NIM** (`https://integrate.api.nvidia.com/v1`) and **OpenRouter** (`https://openrouter.ai/api/v1`) only; single shared OpenAI-compatible client for local and cloud | `test_cloud` |

### 3.12 Outputs & reporting (FR-OUT-*, FR-MET-*, FR-WF-*)

| ID | Requirement | Tests |
| --- | --- | --- |
| FR-OUT-01 | 16-section batch report: executive summary, inventory & gaps, syllabus coverage, frequency, TPS & probability, negative marking, MCQ set, answer key, validation, visual assets, SSJ, review queue, automation, performance, quality, next-cycle prompt | `test_report` |
| FR-OUT-02 | Formats: `md`, `txt`, `pdf`, `csv`, `xlsx`, `svg`, `png` | `test_writers`, `test_images` |
| FR-OUT-03 | Every written file is re-opened and structurally validated; failures are surfaced | `test_report`, `test_pipeline` |
| DR-06 | `provenance.json`: model, tier, n_ctx, temperature, seed, backend, cloud, warnings, version | `test_report`, `test_pipeline` |
| FR-MET-01 | Quality metrics: duplication, translation defects, unsupported claims, human-intervention and SSJ rates | `test_report` |
| FR-MET-02 | Time saved vs. 120-minute manual baseline; automation coverage | `test_report` |
| FR-MET-04 | Estimated vs. measured timings labelled honestly | `test_report` |
| FR-WF-01 | One command runs the full workflow end-to-end | `test_pipeline` |
| FR-WF-02 | Any step that cannot run reports the gap and the batch continues | `test_pipeline`, `test_cli` |

---

## 4. Non-functional requirements

### 4.1 Performance

| ID | Requirement |
| --- | --- |
| NFR-P-07 | Model swap ≤ 5 s target; overrun noted, not fatal |
| NFR-P-08 | Peak RSS reported and bounded by `ram_budget_mb` |
| NFR-D-03 | `n_ctx` derived from RAM budget: ≤1 GB ⇒ 1024, else `min(8192, (MB/1024)·2048)` |
| NFR-D-05 | Streaming writers: CSV/XLSX/PDF emit row-by-row, bounded memory |

### 4.2 Usability

| ID | Requirement |
| --- | --- |
| NFR-U-01 | Every failure prints an actionable hint; tracebacks are suppressed unless `NANDAKA_DEBUG=1` |
| NFR-U-02 | `nandaka doctor` diagnoses the environment as machine-readable JSON |
| NFR-U-03 | No argument required to see help; examples in `--help` |

### 4.3 Security & privacy

| ID | Requirement |
| --- | --- |
| NFR-S-01 | `registry.json` mode 0600; caches mode 0700 |
| NFR-S-02 | Secrets masked in `nandaka config`; absent from logs and audit records |
| NFR-S-03 | No credential material may be committed (enforced in CI) |
| NFR-S-04 | Portal fetches are robots.txt-gated and read-only |

### 4.4 Portability & maintainability

| ID | Requirement |
| --- | --- |
| NFR-PT-01 | Identical CLI on Linux and Termux; all paths env-driven (`NANDAKA_DIR`) |
| NFR-PT-02 | Stdlib-only core; optional deps are lazy and removable |
| NFR-M-01 | Flat-TOML, flat-Config: unknown keys preserved in `extra`, never fatal |
| NFR-M-02 | Portal registry and feature matrix are data, extendable without code changes |

### 4.5 Reliability

| ID | Requirement |
| --- | --- |
| NFR-R-01 | Offline degradation is the normal path, not an error state |
| NFR-R-02 | A crash in one command never exits with an unhandled traceback |

---

## 5. External interfaces & data

- **Inference:** OpenAI-compatible `POST /v1/chat/completions` (llama-server,
  NVIDIA, OpenRouter share one client); `GET /health` for liveness.
- **Sources:** HTTPS GET with `User-Agent`, `If-Modified-Since`, `Range`.
- **Files:** GGUF (binary, read-only), flat-TOML config, JSON registry,
  JSONL audit, CSV past-paper schema
  `year,paper,topic,subtopic,kind,text`.
- **Outputs:** see §3.12.

### Data-model invariants

1. Exactly one of four validation statuses per item (FR-VAL-02).
2. Exactly one SSJ tag per output segment (SSJ-15).
3. A probability claim without a disclosed method/sample/CI fails validation.
4. No artefact is exported while its image licence is uncleared (IR-04).

---

## 6. Verifications

| Level | Location | Notes |
| --- | --- | --- |
| Unit | `tests/test_config`, `test_gguf`, `test_registry`, `test_analysis`, `test_ssj`, `test_validation`, `test_drafting`, `test_writers`, `test_images`, `test_ingest` | no network, no model |
| Integration | `tests/test_pipeline`, `test_report`, `test_cloud`, `test_cli` | local `http.server` stub for cloud; fake backend for the pipeline |
| System / CI | `.github/workflows/ci.yml` | py3.10–3.13 + ARM runner, wheel build, secret scan |

Suite status at baseline: **147 passed**, fully offline.

---

## 7. Traceability matrix (summary)

| Requirement group | Implemented in | Verified by |
| --- | --- | --- |
| FR-IN-* | `nandaka/ingest`, `nandaka/sources` | `test_ingest`, `test_cli` |
| FR-GGUF-*, EX-10 | `nandaka/models/{gguf,registry,server}` | `test_gguf`, `test_registry`, `test_cli` |
| FR-HA-*, FR-NM-* | `nandaka/analysis` | `test_analysis`, `test_cli` |
| FR-GOV-*, FR-SB, FR-AB | `nandaka/drafting` | `test_drafting`, `test_pipeline` |
| IR-* | `nandaka/images` | `test_images` |
| SSJ-*, §3.8 | `nandaka/ssj` | `test_ssj` |
| FR-VAL-*, FR-HIL-* | `nandaka/validation` | `test_validation` |
| FR-CLOUD-* | `nandaka/models/{client,server}` | `test_cloud` |
| FR-OUT-*, FR-MET-* | `nandaka/output` | `test_writers`, `test_report` |
| FR-WF-* | `nandaka/pipeline.py` | `test_pipeline` |
| NFR-U-*, NFR-R-02 | `nandaka/cli.py` | `test_cli` |

---

## 8. Appendices

### A. Open issues / future work

| ID | Item |
| --- | --- |
| FU-01 | Continuous timing capture to replace estimated baselines (FR-MET-04) |
| FU-02 | Additional portals as pure data entries in `PORTALS` |
| FU-03 | OCR for scanned PDFs when `pypdf` yields no text layer |
| FU-04 | Optional ONNX/TFLite backend for on-device inference |

### B. Glossary

| Term | Meaning |
| --- | --- |
| GGUF | llama.cpp's memory-mappable model format |
| T0–T3 | Nandaka capability tiers by parameter count |
| TPS | Topic Priority Score (0–100) |
| SSJ | the content-safety/guardrail rule set of §3.2 |
| Wilson CI | Wilson score interval for a binomial proportion |
| Termux | Android terminal environment providing a Linux userland |
