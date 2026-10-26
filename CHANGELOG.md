# Changelog

All notable changes to NANDAKA are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

**Termux via Ollama configuration (FR-GGUF-13)** — the Ollama backend can now
be selected and pointed at a host entirely through configuration, with no
code changes:

- `NANDAKA_OLLAMA_HOST` joins `NANDAKA_BACKEND` as an environment override
  (env wins over `config.toml`), so a phone can aim at an Ollama instance on
  the LAN or inside `proot-distro` without editing any file.
- `Config.using_ollama()` — one spelling-tolerant predicate used by every call
  site instead of comparing the raw string.
- `probe_ollama()` — best-effort reachability check that reports the tags the
  endpoint holds and degrades instead of raising when the host is down.
- `nandaka init` writes `ollama_host` into the generated `config.toml` and, on
  the Ollama backend, replaces the "install llama.cpp" advice with the
  endpoint it will use.
- `nandaka doctor` gains an `ollama` block (`host`, `in_use`, `reachable`,
  `models`, `reason`). The endpoint is probed **only when the backend is
  selected**, so doctor stays offline by default. With Ollama in use,
  `offline_ready` reflects the endpoint answering and `llama_server` reports
  `"not used (backend = ollama)"`.
- `scripts/install-termux.sh` accepts `NANDAKA_BACKEND=ollama`, skips the
  `llama.cpp` install, resolves the host from `NANDAKA_OLLAMA_HOST` or
  `OLLAMA_HOST`, and prints honest guidance — Termux has no official Ollama
  package, so the endpoint must live on a LAN machine or in `proot-distro`.
  Next steps adapt to pulling an Ollama tag instead of dropping a `.gguf`.
- `--model` help text now names the Ollama case, since it takes a tag on this
  backend.
- `docs/termux.md` §9 documents the backend comparison, the three ways to
  configure it, verification and troubleshooting rows.

Verified by 12 new tests (config precedence, `doctor` against a live local
stub and a refused endpoint, `init` output, `probe_ollama`) plus an
end-to-end smoke of `init` → `doctor` against a stub endpoint.

## [1.0.0] — 2026-10-21

First release. Offline-first academic content and exam-preparation workbench,
specification-complete against `docs/SRS-IEEE-29148.md`.

### Added

**Ingestion (FR-IN-*)**
- Readers for `.md`, `.txt`, `.pdf`, `.csv`, `.xlsx`, `.png/.jpg/.svg` with a
  source inventory (path, kind, chars, pages, tables, notes).
- Portal connectors for NCERT, DIKSHA, AP SCERT, TG SCERT and YesOnlineBooks,
  each a single data entry in `PORTALS`.
- Cache-first fetching (FR-IN-04): a cached page means zero network.
- Resumable downloads (FR-IN-05) with a stored validator, so a resume is
  proven safe instead of assumed; one connection at a time.
- robots.txt gate (AD-05) before any portal request, and a declared
  `User-Agent` on every fetch.

**Models (FR-GGUF-*, EX-10, NFR-P-*)**
- GGUF registry by file drop, with header probing (architecture, parameters,
  quant, context length, chat template), SHA-256 integrity verification and
  tiering (**T0 ≤ 0.6B**, **T1 0.6–2B**, **T2 2–8B**, **T3 > 8B**).
- `llama-server` lifecycle: auto `n_ctx` from the RAM budget (NFR-D-03),
  warm-process reuse for the same model (FR-GGUF-06), unload-then-reload swaps
  so peak RAM never exceeds `ram_budget_mb` (EX-10), and RSS reporting
  (NFR-P-08).
- Capability probe (FR-GGUF-09) across factual / reasoning / instruction
  prompts, persisted in the registry.
- Ollama backend via the same OpenAI dialect on `:11434` (FR-GGUF-13).
- Capability gate: `ok / partial / basic / off` per feature × tier, refusing
  `off` unless `--force` and forcing human review on `basic` (FR-GGUF-11).

**Analysis (FR-HA-*, FR-NM-*)**
- Topic frequency tables with years covered, most recent occurrence,
  rewordings and diagram share (FR-HA-01).
- Topic Priority Score 0–100 from frequency, recency, syllabus relevance and
  kind weighting, with all scoring rules disclosed in the report (FR-HA-02/03).
- Distinct-paper occurrence probability with Wilson confidence interval
  (FR-HA-04/05); below 5 papers or 2 hits reports *"Not reliably estimable"*
  with a provisional TPS rather than a fabricated number (FR-HA-06).
- Negative-marking expectation: `E(attempt)`, `E(skip)`, break-even accuracy
  and an ATTEMPT / SKIP recommendation with its reasoning shown (FR-NM-*).

**Content (FR-GOV-*, FR-SB, FR-AB, IR-*)**
- Bilingual EN–TE MCQ generation with answer keys and Telugu rendering.
- Chapter drafting in summary, abstract and long-form modes.
- Image sourcing and SVG diagram generation, each carrying a manifest
  (source, licence, topic, sha256).

**Guardrails (SSJ-*)**
- Saffronisation, scientific-temper and constitutional-fidelity checks
  (SSJ-01…SSJ-15) applied to every generated item, with a compliance report.

**Validation & review (FR-VAL-*, FR-HIL-*)**
- Structural validation of every MCQ plus a human review queue, so anything
  uncertain is routed to a person rather than shipped (FR-HIL-*).

**Outputs & reporting (FR-OUT-*, FR-MET-*, FR-WF-*)**
- Writers for Markdown, plain text, PDF, CSV, XLSX, SVG and PNG.
- Hand-rolled stdlib PDF: Helvetica (WinAnsi) for Latin plus one embedded
  system TTF (CIDFontType2/Identity-H with ToUnicode) for non-Latin scripts,
  disclosed as a placeholder when no font covers the text.
- XLSX read/write implemented on `zipfile` + XML, with no compiled
  dependencies.
- 16-section batch report including provenance, metrics and a next-cycle
  prompt (FR-OUT-*).

**Cloud fallback (FR-CLOUD-*) — opt-in**
- Disabled by default; enabled only with `--cloud`, token-budgeted, cached and
  audit-logged.
- Two providers behind one shared OpenAI-compatible client:
  **NVIDIA NIM** (`https://integrate.api.nvidia.com/v1`) and
  **OpenRouter** (`https://openrouter.ai/api/v1`).

**Packaging**
- stdlib-only core so it installs and runs on Termux with no compiled
  dependencies; optional extras `pdf`, `xlsx`, `full`, `dev`.
- `scripts/install-termux.sh` for on-device setup (tries `llama.cpp`, then
  `llama-cpp`).
- CI across py3.10–3.13 on ubuntu-latest plus an ARM runner, wheel build and a
  secret scan.

### Fixed

Found by the offline test suite (`tests/test_server.py`) during release
hardening:

- **Resumed downloads could be silently corrupted.** A file edited between an
  interrupted pull and its resume was spliced into an old-prefix/new-tail
  mixture and cached as valid. Resumes are now validated against a stored
  ETag / length before a single body byte is read; on any doubt the partial is
  discarded and the file is fetched whole.
- **The resume path was unreachable in normal operation.** Complete and
  partial files shared one cache path, so the cache-first check short-circuited
  every `Range` request. Partials now live at a sibling `.part` path with a
  `.part.meta` validator.
- **A download truncated by `max_bytes` was promoted into the cache** and
  served as a valid, silently-short document forever. Incomplete downloads are
  now kept as resumable partials and never cached.
- **Progress was discarded when a connection dropped mid-transfer.** Bytes
  already received are now kept and resumed.
- **A partial larger than the remote file produced HTTP 416 on every retry.**
  416 now falls back to a clean full download.
- **A capability probe that failed completely reported `ok: true`** — `all()`
  over an empty filtered list is vacuously true. Success now requires every
  configured probe to answer, and a failed probe is recorded as failed.

### Known limitations

- Warm-process reuse (FR-GGUF-06) and the resume path against a live portal
  (FR-IN-05) are verified against local stubs; neither has been exercised
  against real weights or a real portal, since both need hardware and network
  the test suite deliberately avoids.
- FR-MET-02 "time saved" uses a 120-minute estimated baseline, labelled as an
  estimate (FR-MET-04). It should be replaced with measured authoring data —
  tracked as FU-01.
- OCR for scanned PDFs that yield no text layer is not implemented (FU-03).

[1.0.0]: https://github.com/karun99/nandaka/releases/tag/v1.0.0
