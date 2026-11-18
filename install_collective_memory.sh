#!/usr/bin/env bash
# Install SAD Collective Memory in a separate local venv, isolated from inference.
# Run only from the SAD dev checkout; setup deliberately refuses active workers.
set -euo pipefail

APP="$(cd "$(dirname "$0")" && pwd)"
REPO="$HOME/spore-decomp"
NO_MODEL=0
NO_START=0
PYTHON_BIN=python3

usage() {
  cat <<'HELP'
Usage: bash install_collective_memory.sh [--repo PATH] [--no-model] [--no-start]

Installs the dev-branch SAD engine into an idle Spore checkout, creates an
independent knowledge.sqlite3, installs a private ONNX CPU embedding environment,
downloads model weights into the checkout, and starts the memory daemon.
  --repo PATH   Spore checkout (default: ~/spore-decomp)
  --no-model    SQLite FTS5-only installation, no pip/model download
  --no-start    Download/install, but don't start the local memory server
No OpenRouter inference requests are made. No operational SQLite migrations.
The app's normal "setup" safety check prevents changing scripts while workers run.
HELP
}

while (($#)); do
  case "$1" in
    --repo) [[ $# -gt 1 ]] || { echo "Missing --repo value" >&2; exit 2; }; REPO="$2"; shift 2;;
    --no-model) NO_MODEL=1; shift;;
    --no-start) NO_START=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown option: $1" >&2; usage; exit 2;;
  esac
done

REPO="$(cd "$REPO" && pwd)"
[[ -f "$APP/spore_agentic.py" ]] || { echo "Run this from SAD repository." >&2; exit 2; }
[[ -f "$REPO/tools/contrib/task.py" ]] || { echo "Not a Spore checkout: $REPO" >&2; exit 2; }

echo "Installing dev engine scripts into idle Spore checkout..."
"$PYTHON_BIN" "$APP/spore_agentic.py" setup --repo "$REPO"

HOME_DIR="$REPO/work/scratch/codex-pass1/memory"
SCRIPT="$REPO/work/scratch/spore_collective_memory.py"
mkdir -p "$HOME_DIR"
chmod 700 "$HOME_DIR"
"$PYTHON_BIN" "$SCRIPT" --repo "$REPO" init

if ((NO_MODEL)); then
  echo "FTS5-only installation complete. No embedding server started."
  "$PYTHON_BIN" "$SCRIPT" --repo "$REPO" status
  exit 0
fi

VENV="$HOME_DIR/.venv"
if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --disable-pip-version-check \
    'fastembed==0.9.1' 'sqlite-vec==0.1.9'

echo "Caching ONNX embedding weights locally (first run needs internet)..."
"$VENV/bin/python" "$SCRIPT" --repo "$REPO" warmup

if ((NO_START)); then
  echo "Installed. Start service with:"
  echo "  $VENV/bin/python $SCRIPT --repo $REPO serve"
  exit 0
fi

if "$PYTHON_BIN" "$SCRIPT" --repo "$REPO" status | grep -q '"service_online": true'; then
  echo "Memory service already running."
  exit 0
fi

echo "Starting one local Unix-socket embedding service..."
nohup "$VENV/bin/python" "$SCRIPT" --repo "$REPO" serve \
  >"$HOME_DIR/service.log" 2>&1 </dev/null &
PID=$!
echo "$PID" >"$HOME_DIR/service.pid"

for _ in {1..30}; do
  if "$PYTHON_BIN" "$SCRIPT" --repo "$REPO" status | grep -q '"service_online": true'; then
    echo "Collective Memory service started, PID $PID"
    "$PYTHON_BIN" "$SCRIPT" --repo "$REPO" status
    exit 0
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "Memory server exited during startup; see $HOME_DIR/service.log" >&2
    tail -n 25 "$HOME_DIR/service.log" >&2
    exit 1
  fi
  sleep 1
done
echo "Memory service did not report ready, see $HOME_DIR/service.log" >&2
exit 1
