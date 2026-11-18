#!/usr/bin/env bash
# Explicitly opt in to paid, low-priority automatic DeepSeek consolidation.
# systemd user service when available; session-only nohup fallback for WSL.
set -euo pipefail

REPO="$HOME/spore-decomp"
ACTION=enable
DAILY_USD=0.25
INTERVAL=180
BATCH=3
UNIT=sad-technique-consolidator.service

usage() {
  cat <<'HELP'
Usage: bash enable_automatic_consolidation.sh --repo ~/spore-decomp --daily-usd 0.25
       bash enable_automatic_consolidation.sh --repo ~/spore-decomp --status
       bash enable_automatic_consolidation.sh --repo ~/spore-decomp --disable

Explicitly enables paid DeepSeek consolidation, with a separate daily
estimated admission cap (NOT a provider-enforced hard account limit).
Under systemd --user this persists across user login/WSL restarts. If systemd
is unavailable, a background process runs for the current WSL lifetime.
No SAD workers or operational SQLite records are modified.
HELP
}
while (($#)); do
  case "$1" in
    --repo) [[ $# -gt 1 ]] || exit 2; REPO="$2"; shift 2;;
    --daily-usd) [[ $# -gt 1 ]] || exit 2; DAILY_USD="$2"; shift 2;;
    --interval) [[ $# -gt 1 ]] || exit 2; INTERVAL="$2"; shift 2;;
    --batch) [[ $# -gt 1 ]] || exit 2; BATCH="$2"; shift 2;;
    --status) ACTION=status; shift;;
    --disable) ACTION=disable; shift;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown argument: $1" >&2; exit 2;;
  esac
done

REPO="$(cd "$REPO" && pwd -P)"
SCRIPT="$REPO/work/scratch/spore_technique_consolidator.py"
ROOT="$REPO/work/scratch/codex-pass1/memory"
PIDFILE="$ROOT/consolidator.pid"
LOGFILE="$ROOT/consolidator.log"
PYTHON="$(command -v python3)"
USER_UNIT_DIR="$HOME/.config/systemd/user"
UNITFILE="$USER_UNIT_DIR/$UNIT"

# Do not interpolate untrusted quotes, percent escapes or shell expressions
# into a systemd unit. Current SAD installation paths are simple Linux paths.
case "$REPO" in *[!a-zA-Z0-9_./-]*)
  echo "Automatic service requires a checkout path without spaces/metacharacters." >&2
  exit 2;;
esac
[[ "$DAILY_USD" =~ ^[0-9]+([.][0-9]+)?$ ]] &&
  awk -v x="$DAILY_USD" 'BEGIN{exit !(x>0 && x<=100)}' ||
  { echo "--daily-usd must be a positive amount <=100." >&2; exit 2; }
[[ "$INTERVAL" =~ ^[0-9]+$ && "$INTERVAL" -ge 30 ]] ||
  { echo "--interval must be at least 30 seconds." >&2; exit 2; }
[[ "$BATCH" =~ ^[0-9]+$ && "$BATCH" -ge 1 && "$BATCH" -le 100 ]] ||
  { echo "--batch must be between 1 and 100." >&2; exit 2; }

systemd_ready() {
  command -v systemctl >/dev/null 2>&1 &&
    systemctl --user show-environment >/dev/null 2>&1
}
own_pid() {
  [[ -f "$PIDFILE" ]] || return 1
  local pid command_line
  pid="$(cat "$PIDFILE")"
  [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/cmdline" ]] || return 1
  command_line="$(tr '\0' ' ' <"/proc/$pid/cmdline")"
  [[ "$command_line" == *"$SCRIPT"* && "$command_line" == *" serve"* ]]
}

if [[ "$ACTION" == status ]]; then
  if systemd_ready && [[ -f "$UNITFILE" ]]; then
    systemctl --user status --no-pager "$UNIT" || true
  elif own_pid; then
    echo "WSL-session daemon running, PID $(cat "$PIDFILE")"
  else
    echo "No automatic consolidator running"
  fi
  [[ ! -f "$SCRIPT" ]] || "$PYTHON" "$SCRIPT" --repo "$REPO" status
  exit 0
fi

if [[ "$ACTION" == disable ]]; then
  if systemd_ready && [[ -f "$UNITFILE" ]]; then
    systemctl --user disable --now "$UNIT" || true
    rm -f "$UNITFILE"
    systemctl --user daemon-reload
  fi
  if own_pid; then
    kill -TERM "$(cat "$PIDFILE")"
  fi
  rm -f "$PIDFILE"
  echo "Automatic paid consolidation disabled"
  exit 0
fi

[[ -f "$SCRIPT" ]] || { echo "Install the updated consolidator first: $SCRIPT" >&2; exit 1; }
mkdir -p "$ROOT"
chmod 700 "$ROOT"

# Preflight, no API calls made.
KEYFILE="$HOME/.config/spore-agentic-decompiler/openrouter.key"
if [[ -z "${OPENROUTER_API_KEY:-}" && ! -s "$KEYFILE" ]]; then
  echo "No OpenRouter key found. Configure SAD first." >&2
  exit 1
fi

echo "Enabling paid consolidation with USD $DAILY_USD estimated daily cap."
if systemd_ready; then
  mkdir -p "$USER_UNIT_DIR"
  chmod 700 "$USER_UNIT_DIR"
  cat >"$UNITFILE" <<EOF
[Unit]
Description=SAD DeepSeek Compiler Technique Consolidator

[Service]
Type=simple
WorkingDirectory=$REPO
ExecStart=$PYTHON $SCRIPT --repo $REPO serve --daily-usd $DAILY_USD --interval $INTERVAL --batch $BATCH
Restart=on-failure
RestartSec=45
Nice=10
UMask=0077

[Install]
WantedBy=default.target
EOF
  chmod 600 "$UNITFILE"
  systemctl --user daemon-reload
  systemctl --user enable --now "$UNIT"
  systemctl --user restart "$UNIT"
  echo "Systemd user service enabled: $UNIT"
  echo "Inspect via: systemctl --user status $UNIT"
else
  if own_pid; then
    echo "Session-only consolidator already running (PID $(cat "$PIDFILE"))"
  else
    nohup "$PYTHON" "$SCRIPT" --repo "$REPO" serve \
      --daily-usd "$DAILY_USD" --interval "$INTERVAL" --batch "$BATCH" \
      >"$LOGFILE" 2>&1 </dev/null &
    echo "$!" >"$PIDFILE"
    echo "Started WSL-session background consolidator PID $(cat "$PIDFILE")"
  fi
  echo "systemd --user unavailable, background daemon ends when WSL shuts down."
  echo "Logs: $LOGFILE"
fi
echo "Disable via: bash enable_automatic_consolidation.sh --repo $REPO --disable"
