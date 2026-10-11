#!/bin/bash
set -euo pipefail

# Rapid-MLX is pinned so bench results stay comparable; bump deliberately with GWEN_RAPID_MLX=<version>.
TARGET="${GWEN_SSH:-sammy@sammys-macbook-air}"
KEY_FILE="${GWEN_KEY_FILE:-$HOME/.config/gwen/api-key}"
HTTP_HOST="${GWEN_HTTP_HOST:-100.86.30.90}"

if [ "$#" -eq 0 ]; then
  echo "Usage: $0 <command> [...]" >&2
  exit 2
fi

COMMAND="$1"
shift

case "$COMMAND" in
  install)
    # create ~/.config/gwen (700), ~/Library/Logs/gwen
    VERSION="${GWEN_RAPID_MLX:-0.15.7}"
    if [[ ! "$VERSION" =~ ^[0-9]+(\.[0-9]+)*$ ]]; then
      echo "Invalid GWEN_RAPID_MLX version" >&2
      exit 2
    fi
    # shellcheck disable=SC2029,SC2016
    ssh "$TARGET" 'export PATH="$HOME/.local/bin:$PATH"; uv tool install --force "rapid-mlx=='"$VERSION"'"; mkdir -p ~/.config/gwen ~/Library/Logs/gwen; chmod 700 ~/.config/gwen'
    # copy key (stdin, never argv) to ~/.config/gwen/api-key (600)
    ssh "$TARGET" 'cat > ~/.config/gwen/api-key; chmod 600 ~/.config/gwen/api-key' < "$KEY_FILE"
    # copy gwen-serve to ~/.local/bin/ (755)
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    ssh "$TARGET" 'cat > ~/.local/bin/gwen-serve; chmod 755 ~/.local/bin/gwen-serve' < "$SCRIPT_DIR/gwen-host/gwen-serve"
    # gwen-tap (public listener that logs the model's output) and gwen-watch (live stats)
    for tool in gwen-tap gwen-watch; do
      # shellcheck disable=SC2029
      ssh "$TARGET" "cat > ~/.local/bin/$tool; chmod 755 ~/.local/bin/$tool" < "$SCRIPT_DIR/gwen-host/$tool"
    done
    # rewrite plists and copy
    for agent in dev.vsn.gwen dev.vsn.gwen-tap; do
      # shellcheck disable=SC2029,SC2016
      ssh "$TARGET" "sed \"s|/Users/sammy|\$HOME|g\" > ~/Library/LaunchAgents/$agent.plist" < "$SCRIPT_DIR/gwen-host/$agent.plist"
    done
    # write model if absent
    ssh "$TARGET" 'if [ ! -f ~/.config/gwen/model ]; then echo "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit" > ~/.config/gwen/model; fi'
    # launchctl bootout and bootstrap
    ssh "$TARGET" 'for a in dev.vsn.gwen dev.vsn.gwen-tap; do launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/$a.plist 2>/dev/null || true; launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/$a.plist; done'
    ;;
  switch)
    if [ "$#" -eq 0 ]; then
      echo "Usage: $0 switch <model> [flag ...]" >&2
      exit 2
    fi
    MODEL="$1"
    shift
    if [[ ! "$MODEL" =~ ^[A-Za-z0-9._/-]+$ ]]; then
      echo "Invalid model name" >&2
      exit 2
    fi
    # validate every flag before any ssh call
    FLAG_TEXT=""
    for FLAG in "$@"; do
      if [[ ! "$FLAG" =~ ^[A-Za-z0-9._=/:,+-]+$ ]]; then
        echo "Invalid flag: $FLAG" >&2
        exit 2
      fi
      FLAG_TEXT+="$FLAG"$'\n'
    done
    # flags travel on stdin, one per line (empty input clears the file). The remote command
    # is passed as two words; ssh joins them with a space, so the model command stays intact.
    # shellcheck disable=SC2029
    printf '%s' "$FLAG_TEXT" | ssh "$TARGET" "cat > ~/.config/gwen/flags;" "echo '$MODEL' > ~/.config/gwen/model; launchctl kickstart -k gui/\$(id -u)/dev.vsn.gwen"
    ;;
  status)
    ssh "$TARGET" 'cat ~/.config/gwen/model; echo "flags:"; cat ~/.config/gwen/flags 2>/dev/null || echo "(none)"; launchctl print gui/$(id -u)/dev.vsn.gwen | grep state; tail -n 5 ~/Library/Logs/gwen/serve.log' || true
    # locally curl
    curl -sS -m 5 -K - "http://$HTTP_HOST:8642/v1/models" <<EOF
header = "Authorization: Bearer $(cat "$KEY_FILE")"
EOF
    ;;
  stop)
    ssh "$TARGET" 'launchctl bootout gui/$(id -u)/dev.vsn.gwen-tap; launchctl bootout gui/$(id -u)/dev.vsn.gwen'
    ;;
  logs)
    N="${1:-50}"
    if [[ ! "$N" =~ ^[0-9]+$ ]]; then
      echo "Usage: $0 logs [lines]" >&2
      exit 2
    fi
    # shellcheck disable=SC2029
    ssh "$TARGET" "tail -n $N ~/Library/Logs/gwen/serve.log"
    ;;
  *)
    echo "Usage: $0 {install|switch|status|stop|logs}" >&2
    exit 2
    ;;
esac
