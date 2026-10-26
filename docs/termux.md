# Running Nandaka on Termux

Nandaka is designed to run on a phone with no cloud account: the core is
**stdlib-only** (no compiled Python extensions), every path is env-driven, and
the optional dependencies are lazy imports that degrade gracefully.

## 1. Install packages

```bash
pkg update && pkg install -y python git
```

`llama.cpp` provides the local `llama-server` binary. The package name varies
by Termux repository generation — try:

```bash
pkg install -y llama.cpp      # current
# or, on older repositories:
pkg install -y llama-cpp
```

If neither exists on your build, either build llama.cpp from source inside
Termux (`git clone https://github.com/ggml-org/llama.cpp && cmake -B build &&
cmake --build build -j`) or point Nandaka at a binary you trust:

```bash
export NANDAKA_LLAMA_SERVER=/path/to/llama-server
```

Prefer Ollama? It needs no `.gguf` files and no `llama-server`, but Termux has
no official Ollama package — see [§9](#9-using-ollama-instead-of-llama-cpp-fr-gguf-13).

`pkg install llama-cpp-python` is **not** required — Nandaka talks to
llama-server over its OpenAI-compatible HTTP dialect, not through Python
bindings.

## 2. Install Nandaka

```bash
git clone https://github.com/karun99/nandaka.git
cd nandaka
pip install .                 # core: no compiler needed
pip install ".[pdf]"          # optional: PDF text extraction (pypdf)
```

Or run the installer, which does all of the above and runs `doctor`:

```bash
bash scripts/install-termux.sh
```

## 3. Put data on shared storage

Termux's private `$HOME` is wiped on app update. Keep Nandaka's state on
shared storage:

```bash
pkg install -y storage          # once, then approve the storage permission
export NANDAKA_DIR="$HOME/storage/shared/nandaka"
echo 'export NANDAKA_DIR="$HOME/storage/shared/nandaka"' >> ~/.bashrc
```

Layout under `NANDAKA_DIR`:

| Path | Contents |
| --- | --- |
| `config.toml` | flat-TOML config written by `nandaka init` |
| `models/` | drop `.gguf` files here (file-drop = discovered on next scan) |
| `registry.json` | model metadata, tiers, probe results (mode 0600) |
| `cache/` | portal + cloud response caches (mode 0700) |
| `nandaka-out/` | batch output directories |
| `cloud-audit.jsonl` | cloud call audit log (only if `--cloud` was used) |

## 4. First run

```bash
nandaka init        # directories + config template
nandaka doctor      # JSON diagnosis: paths, llama-server, fonts, RAM budget
nandaka models scan # discover + tier your GGUF files
nandaka mcq --topic "Constitution" --count 10 --offline
```

`doctor` on a healthy Termux install looks like:

```json
{
  "profile": {"termux": true, "machine": "aarch64", "python": "3.12"},
  "llama_server": "/data/data/com.termux/files/usr/bin/llama-server",
  "cloud": {"enabled": false, "provider": "nvidia"},
  "fonts": {"selected": "/system/fonts/NotoSansTelugu-VF.ttf",
            "telugu_capable": true},
  "offline_ready": true
}
```

## 5. RAM budget → context length (CO-01, NFR-D-03)

Phones are memory-constrained, so Nandaka derives `n_ctx` from the budget
instead of guessing:

```bash
nandaka mcq --topic "Economy" --ram-budget 1024   # forces n_ctx = 1024
```

| `ram_budget_mb` | `n_ctx` |
| --- | --- |
| ≤ 1024 | 1024 |
| 2048 | 2048 |
| 4096 | 4096 |
| 8192 | 8192 (cap) |

Set it permanently in `config.toml` or via `NANDAKA_RAM_BUDGET_MB`.

A model swap that exceeds the 5-second target (NFR-P-07) prints a note — it
never fails the run.

## 6. Model tiers on a phone (FR-GGUF-04)

| Tier | Parameters | Realistic use on-device |
| --- | --- | --- |
| T0 | ≤ 0.6B | short summaries, basic MCQs — every item forced to human review |
| T1 | 0.6–2B | bilingual drafts (partial), basic MCQs |
| T2 | 2–8B | full MCQ generation, reasoning, long drafts |
| T3 | > 8B | everything, but likely exceeds a phone RAM budget |

Drop a GGUF in `models/` — no code changes, no config edits. A feature that
needs a larger tier is refused with an actionable message (`--force`
overrides, at the cost of a mandatory-review warning).

## 7. Networking on a phone

- **Default is fully offline.** Portal connectors (`nandaka sources fetch`)
  are cache-first and robots.txt-aware; on failure they fall back to cache and
  report the gap — never fatal.
- **Cloud fallback stays off** unless you export `NANDAKA_CLOUD=1` *and* a
  provider key. On mobile data this matters: every call is budgeted and
  audited.

```bash
export NVIDIA_API_KEY=...        # or OPENROUTER_API_KEY
nandaka mcq --topic "Economy" --cloud --count 10
```

## 8. Telugu PDF output

Android ships Telugu-capable fonts in `/system/fonts`, which Nandaka finds
automatically; Latin text uses built-in Helvetica. See
[`../nandaka/assets/fonts/README.md`](../nandaka/assets/fonts/README.md) for
the search order and the disclosure warning when no font covers the script.

## 9. Using Ollama instead of llama.cpp (FR-GGUF-13)

Nandaka speaks the same OpenAI-compatible dialect to both backends, so
switching is configuration, not code:

| | `llama_cpp` (default) | `ollama` |
| --- | --- | --- |
| Weights live in | `$NANDAKA_DIR/models/*.gguf` | Ollama's own tag store |
| Process started by Nandaka | yes (`llama-server`) | no — Ollama runs itself |
| `--model` argument | path to a `.gguf` | an Ollama tag, e.g. `llama3.2:1b` |
| On a phone | runs on-device | usually a machine on the LAN |

**Termux ships no official Ollama package.** The endpoint therefore has to
live somewhere else, and Nandaka just points at it:

- **A PC on the same LAN** — run `ollama serve` there (it binds `0.0.0.0:11434`
  when configured to), then export the PC's address.
- **Inside proot-distro** — `pkg install proot-distro && proot-distro install
  ubuntu`, then install Ollama in the distro and point at its port.

### Configure it

Any one of these is enough; env vars win over `config.toml`:

```bash
# 1) environment (simplest on Termux — put it in ~/.bashrc)
export NANDAKA_BACKEND=ollama
export NANDAKA_OLLAMA_HOST=http://192.168.1.24:11434

# 2) or config.toml, written by `nandaka init`
backend = "ollama"
ollama_host = "http://192.168.1.24:11434"

# 3) or bake it in during install
NANDAKA_BACKEND=ollama NANDAKA_OLLAMA_HOST=http://192.168.1.24:11434 \
  bash scripts/install-termux.sh
```

### Verify and run

```bash
nandaka doctor    # "ollama": {"host": ..., "reachable": true, "models": [...]}
nandaka mcq --model llama3.2:1b --topic "Constitution" --count 10
```

`doctor` probes the endpoint **only when the backend is selected**, so it stays
offline by default. With Ollama in use it also reports the tags the endpoint
holds, and `offline_ready` reflects the endpoint answering rather than a local
`llama-server` binary.

Notes:

- No `.gguf` file is needed on the phone, and `nandaka models scan` (which
  scans a local GGUF directory) is not part of this path.
- SSJ guardrails, validation, the human review queue and every output format
  behave identically — the backend only changes where completions come from.
- Cloud fallback (NVIDIA NIM / OpenRouter) is a separate, still opt-in switch.
  Ollama on your LAN is local inference, not a cloud call, so `--cloud` stays
  off.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `nandaka: command not found` | `pip install --force-reinstall .`, or `python3 -m nandaka.cli` |
| `llama-server binary not found` | `pkg install llama.cpp` or set `NANDAKA_LLAMA_SERVER` — or switch to the Ollama backend (§9) |
| `ollama` shows `reachable: false` | Termux has no Ollama package: run `ollama serve` on a LAN PC or inside proot-distro, then fix `NANDAKA_OLLAMA_HOST` |
| `no GGUF model selected` while using Ollama | this backend takes tags, not files: pass `--model llama3.2:1b` |
| `no GGUF model selected` | put exactly one `.gguf` in `models/`, or pass `--model` |
| PDF shows placeholder glyphs | install a covering TTF or set `NANDAKA_FONT` |
| cloud says "no API key" | export the key *and* `NANDAKA_CLOUD=1`, or drop `--cloud` |
| `pip` tries to compile something | core is stdlib-only — ensure you installed `.` and not an extra that needs a build |
