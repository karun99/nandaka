# NANDAKA

**Offline-first, GGUF-agnostic academic content & exam-preparation workbench**
for NCERT/SCERT school books and Indian government-exam material — bilingual
(English + Telugu), human-reviewed, and guarded by the SSJ rule engine.

Nandaka runs the whole workflow locally: drop a `.gguf` file into the model
directory and it handles ingestion → historical analysis → prioritisation →
MCQ/drafting → images → validation → SSJ → human review → export → metrics.

- **Offline by default.** No network is required for any core feature. Source
  portal connectors are cache-first and advisory; the run continues when the
  network is down and reports the gap instead of failing.
- **GGUF-agnostic.** Any llama.cpp-compatible GGUF works. Models are probed,
  tiered (T0 ≤0.6B → T3 >8B) and capability-gated; a tier too small for a task
  is refused honestly rather than producing silent garbage.
- **Stdlib-only core.** Zero compiled dependencies, so `pip install .` works on
  Termux without a compiler. Optional extras (`pypdf`, `openpyxl`) are lazy
  imports that degrade gracefully when absent.
- **Cloud fallback is opt-in.** Disabled by default, enabled only with
  `--cloud` / `NANDAKA_CLOUD=1`, token-budgeted, response-cached, and
  audit-logged. Providers: **NVIDIA NIM** and **OpenRouter** (OpenAI-compatible
  dialect; one shared client serves local llama-server and both providers).
- **SSJ guardrails on every output.** All generated text is screened by the
  rule engine, tagged (`SSJ-GUARDRAIL: PASS / REVIEW / EXCLUDED / NOT
  APPLICABLE`), and anything uncertain lands in a prioritised human-review
  queue — never silently shipped.

## Install

### Linux

```bash
git clone https://github.com/karun99/nandaka.git
cd nandaka
python3 -m pip install .          # stdlib-only core
python3 -m pip install ".[pdf,xlsx]"   # optional: PDF read + XLSX extras
python3 -m pip install ".[dev]"   # test suite
```

You also need a local inference server:

```bash
# llama.cpp (provides llama-server)
git clone https://github.com/ggml-org/llama.cpp && cd llama.cpp
cmake -B build && cmake --build build -j
export PATH="$PWD/build/bin:$PATH"
```

### Termux

```bash
pkg update && pkg install python llama-cpp-python llama.cpp git
git clone https://github.com/karun99/nandaka.git
cd nandaka
pip install .

# keep data on shared storage (survives app updates)
export NANDAKA_DIR="$HOME/storage/shared/nandaka"
nandaka init
```

If `pkg install llama.cpp` is unavailable on your Termux build, install a
prebuilt `llama-server` or set `NANDAKA_LLAMA_SERVER=/path/to/llama-server`.
`nandaka doctor` tells you exactly what is missing.

Prefer Ollama (models as tags, no `.gguf` files)? Set the backend purely
through config — Termux ships no Ollama package, so the endpoint is a machine
on your LAN or a `proot-distro` instance:

```bash
export NANDAKA_BACKEND=ollama
export NANDAKA_OLLAMA_HOST=http://192.168.1.24:11434
nandaka doctor                 # probes the endpoint only when selected
nandaka mcq --model llama3.2:1b --topic "Constitution" --count 10
```

More detail: [`docs/termux.md`](docs/termux.md) ·
[`scripts/install-termux.sh`](scripts/install-termux.sh)

## Quick start

```bash
nandaka init                    # create NANDAKA_DIR + config.toml
nandaka doctor                  # Termux-aware environment check (JSON)
nandaka models scan             # discover .gguf files, classify tiers

# drop a model in, then generate a batch
nandaka mcq --topic "Constitution" --exam "Group-2" --count 30 \
    --papers past_papers.csv --marking '{"R":1,"W":0.33,"U":0}' --p 0.6

# negative-marking maths standalone
nandaka negative --R 1 --W 0.33 --U 0 --p 0.6

# historical frequency + Topic Priority Scores
nandaka analyse --papers past_papers.csv

# official source portals (cache-first, robots.txt-aware)
nandaka sources list
nandaka sources fetch ncert --offline

# school / academic drafting
nandaka draft chapter --class 8 --subject Civics \
    --title "Fundamental Rights" --format md,pdf
nandaka draft book --outline outline.json
```

Outputs land in `$NANDAKA_DIR/nandaka-out/batch-*/`:

| Artefact | Purpose |
| --- | --- |
| `report.md` / `report.txt` / `report.pdf` | 16-section batch report |
| `mcqs.csv` / `mcqs.xlsx` | bilingual MCQ set + answer key |
| `validation.csv`, `review-queue.csv` | FR-VAL statuses, prioritised human review |
| `ssj-report.csv`, `images.csv` | SSJ compliance rows, image manifest (IR-01..17) |
| `frequency.csv`, `tps.csv` | historical frequency + TPS with occurrence probability |
| `provenance.json`, `metrics.json` | model/tier/seed/backend, quality & time metrics |

## Configuration

`nandaka init` writes a flat-TOML `config.toml` inside `NANDAKA_DIR`
(default `~/.nandaka`, Termux-friendly). Everything is overridable by env:

| Variable | Effect |
| --- | --- |
| `NANDAKA_DIR` | data root (config, models, cache, output) |
| `NANDAKA_MODEL_DIR`, `NANDAKA_WORK_DIR` | override model/output locations |
| `NANDAKA_RAM_BUDGET_MB`, `NANDAKA_N_CTX` | CO-01 RAM budget → auto `n_ctx` |
| `NANDAKA_LLAMA_SERVER` | path to `llama-server` binary |
| `NANDAKA_BACKEND` | `llama_cpp` \| `ollama` (FR-GGUF-13) |
| `NANDAKA_OLLAMA_HOST` | Ollama endpoint when `NANDAKA_BACKEND=ollama` |
| `NANDAKA_CLOUD` | `1` enables cloud fallback (default: off) |
| `NVIDIA_API_KEY`, `OPENROUTER_API_KEY` | provider credentials |
| `NANDAKA_CLOUD_PROVIDER` | `nvidia` \| `openrouter` |
| `NANDAKA_FONT` | TTF to embed for non-Latin PDF text |

Secrets are never printed (`nandaka config` masks them) and never logged to
the cloud audit file.

## Cloud fallback (opt-in)

```bash
export NVIDIA_API_KEY=...            # or OPENROUTER_API_KEY
nandaka mcq --topic "Economy" --cloud --count 20
```

Behaviour (FR-CLOUD-01..06):

1. **Off by default** — no key, no cloud; runs continue offline.
2. **Token budget** — requests that would exceed `cloud_token_budget` are
   refused *before* any network call.
3. **Response cache** — identical requests are served from disk.
4. **Audit log** — every call (including failures) appends to
   `cloud-audit.jsonl`; API keys are never written.

## Testing

```bash
python3 -m pytest tests/            # 191 tests, fully offline
```

The suite needs no network and no model: cloud behaviour is tested against a
local `http.server` stub, and the pipeline tests run end-to-end with a fake
backend.

## Architecture

```
nandaka/
├── config.py        env-driven config (Termux-aware, flat TOML)
├── cli.py           init doctor config ingest sources analyse negative
│                    models mcq draft batch bench
├── pipeline.py      FR-WF-01 orchestration (mcq batch, chapter, book)
├── models/          GGUF parser, registry & tier gates, ChatClient,
│                    llama-server lifecycle, NVIDIA/OpenRouter clients
├── ingest/          md txt pdf csv xlsx images → Document inventory
├── sources/         NCERT / DIKSHA / AP SCERT / TG SCERT connectors
├── analysis/        frequency, TPS + Wilson CI, negative-marking maths
├── drafting/        bilingual MCQ, school book, academic book (SSJ-preambled)
├── images/          SVG/PNG generators, manifest, IR-01..IR-17 checks
├── ssj/             rule engine + §3.8 compliance report
├── validation/      FR-VAL battery + prioritised ReviewQueue
├── output/          csv/xlsx/pdf/md/txt writers, report, metrics
└── output/pdfwriter.py  stdlib PDF: Helvetica + one embedded TTF
```

Design notes live in [`docs/SRS-IEEE-29148.md`](docs/SRS-IEEE-29148.md)
(requirements traceability against IEEE 29148).

## License

MIT — see [LICENSE](LICENSE).
