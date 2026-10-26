#!/data/data/com.termux/files/usr/bin/bash
# NANDAKA Termux installer — stdlib-only core, no compiler required.
#
# Usage:
#   pkg install curl && curl -fsSL <raw-url>/scripts/install-termux.sh | bash
#   # or, from a checkout:
#   bash scripts/install-termux.sh
#
# Everything is written under NANDAKA_DIR (default: shared storage so data
# survives Termux app updates).

set -euo pipefail

fail() { printf '[nandaka] ERROR: %s\n' "$1" >&2; exit 1; }
note() { printf '[nandaka] %s\n' "$1"; }

# ---------------------------------------------------------------- prerequisites
if ! command -v python3 >/dev/null 2>&1; then
  if command -v pkg >/dev/null 2>&1; then
    note "installing python…"
    pkg update -y && pkg install -y python
  else
    fail "python3 not found and 'pkg' unavailable (run inside Termux)"
  fi
fi

PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || fail "Python 3.10+ required (found $PYV). Run: pkg install python"

# ---------------------------------------------------------------- locate repo
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-0}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
if [ -f "$REPO_DIR/pyproject.toml" ] && [ -d "$REPO_DIR/nandaka" ]; then
  SRC="$REPO_DIR"
elif [ -f "$SCRIPT_DIR/pyproject.toml" ]; then
  SRC="$SCRIPT_DIR"
else
  note "cloning nandaka…"
  command -v git >/dev/null 2>&1 || pkg install -y git
  SRC="${TMPDIR:-/tmp}/nandaka-src"
  rm -rf "$SRC"
  git clone --depth 1 https://github.com/karun99/nandaka.git "$SRC"
fi
note "source: $SRC"

# ---------------------------------------------------------------- install pkg
note "installing nandaka (stdlib-only core)…"
python3 -m pip install --upgrade pip >/dev/null 2>&1 || true
python3 -m pip install "$SRC"

# Optional extras — each is a lazy import, so a failure is a warning, not fatal
# (FR-IN-06: report the gap, continue).
for extra in pdf xlsx; do
  if python3 -m pip install "$SRC[$extra]" >/dev/null 2>&1; then
    note "optional extra '$extra' installed"
  else
    note "optional extra '$extra' skipped (pure-stdlib fallback will be used)"
  fi
done

# ---------------------------------------------------------------- backend
# "llama_cpp" (default, fully on-device) or "ollama" (FR-GGUF-13).
# Select with:  NANDAKA_BACKEND=ollama bash scripts/install-termux.sh
BACKEND="${NANDAKA_BACKEND:-llama_cpp}"
case "$BACKEND" in
  llama_cpp|ollama) ;;
  *) fail "unknown backend '$BACKEND' (use llama_cpp or ollama)" ;;
esac
note "model backend: $BACKEND"

# ---------------------------------------------------------------- llama.cpp
# pkg name differs across Termux builds; try both, never abort on failure.
if [ "$BACKEND" = "llama_cpp" ] && ! command -v llama-server >/dev/null 2>&1; then
  note "looking for llama-server…"
  if pkg install -y llama.cpp >/dev/null 2>&1 \
     || pkg install -y llama-cpp >/dev/null 2>&1; then
    note "llama.cpp installed via pkg"
  else
    note "llama-server not found. Options:"
    note "  1) pkg install llama.cpp   (if your repo carries it)"
    note "  2) build from source: https://github.com/ggml-org/llama.cpp"
    note "  3) export NANDAKA_LLAMA_SERVER=/path/to/llama-server"
  fi
fi

# ---------------------------------------------------------------- ollama
# Termux has no official Ollama package, so the endpoint has to live
# somewhere else — a PC on the same LAN, or an instance inside proot-distro.
# NANDAKA just points at it; nothing here downloads an Ollama binary.
if [ "$BACKEND" = "ollama" ]; then
  # Accept either spelling of the host and pin it for this install.
  if [ -z "${NANDAKA_OLLAMA_HOST:-}" ] && [ -n "${OLLAMA_HOST:-}" ]; then
    NANDAKA_OLLAMA_HOST="$OLLAMA_HOST"
  fi
  export NANDAKA_BACKEND=ollama
  export NANDAKA_OLLAMA_HOST="${NANDAKA_OLLAMA_HOST:-http://127.0.0.1:11434}"
  note "ollama endpoint: $NANDAKA_OLLAMA_HOST"
  if [ "$NANDAKA_OLLAMA_HOST" = "http://127.0.0.1:11434" ] \
     && command -v curl >/dev/null 2>&1 \
     && ! curl -fsS --max-time 2 "$NANDAKA_OLLAMA_HOST/api/tags" >/dev/null 2>&1; then
    note "no Ollama answering on this phone. Termux ships no Ollama package;"
    note "  run one elsewhere, then point Nandaka at it:"
    note "    a) PC on the same LAN:  ollama serve  (binds 0.0.0.0:11434), then"
    note "       export NANDAKA_OLLAMA_HOST=http://<pc-ip>:11434"
    note "    b) inside proot-distro: pkg install proot-distro &&"
    note "       proot-distro install ubuntu, then install Ollama there"
  fi
  note "persist in ~/.bashrc:"
  note "  export NANDAKA_BACKEND=ollama"
  note "  export NANDAKA_OLLAMA_HOST=\"$NANDAKA_OLLAMA_HOST\""
fi

# ---------------------------------------------------------------- data dir
# Shared storage survives Termux updates; fall back to $HOME.
if [ -z "${NANDAKA_DIR:-}" ]; then
  if [ -d "$HOME/storage/shared" ]; then
    export NANDAKA_DIR="$HOME/storage/shared/nandaka"
  else
    export NANDAKA_DIR="$HOME/.nandaka"
  fi
  note "NANDAKA_DIR not set; using $NANDAKA_DIR"
  note "  add to ~/.bashrc:  export NANDAKA_DIR=\"$NANDAKA_DIR\""
fi

# ---------------------------------------------------------------- verify
command -v nandaka >/dev/null 2>&1 || fail "nandaka not on PATH (try: pip install --force-reinstall $SRC)"

nandaka init
echo
nandaka doctor | sed -n '1,60p'

note "done. next steps:"
if [ "$BACKEND" = "ollama" ]; then
  note "  1) pull a model on the ollama host:  ollama pull llama3.2:1b"
  note "  2) nandaka doctor            # shows 'ollama' reachable + its tags"
  note "  3) nandaka mcq --model llama3.2:1b --topic 'Constitution' --count 10"
  note "     (--model takes the Ollama tag; no .gguf needed on the phone)"
else
  note "  1) drop a .gguf into \$NANDAKA_DIR/models"
  note "  2) nandaka models scan"
  note "  3) nandaka mcq --topic 'Constitution' --count 10 --offline"
fi
