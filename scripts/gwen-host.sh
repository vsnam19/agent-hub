#!/bin/bash
set -euo pipefail

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
    ssh "$TARGET" 'export PATH="$HOME/.local/bin:$PATH"; uv tool install rapid-mlx@latest; mkdir -p ~/.config/gwen ~/Library/Logs/gwen; chmod 700 ~/.config/gwen'
    # copy key (stdin, never argv) to ~/.config/gwen/api-key (600)
    ssh "$TARGET" 'cat > ~/.config/gwen/api-key; chmod 600 ~/.config/gwen/api-key' < "$KEY_FILE"
    # copy gwen-serve to ~/.local/bin/ (755)
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    ssh "$TARGET" 'cat > ~/.local/bin/gwen-serve; chmod 755 ~/.local/bin/gwen-serve' < "$SCRIPT_DIR/gwen-host/gwen-serve"
    # rewrite plist and copy
    ssh "$TARGET" 'sed "s|/Users/sammy|$HOME|g" > ~/Library/LaunchAgents/dev.vsn.gwen.plist' < "$SCRIPT_DIR/gwen-host/dev.vsn.gwen.plist"
    # write model if absent
    ssh "$TARGET" 'if [ ! -f ~/.config/gwen/model ]; then echo "qwen3.5-4b-4bit" > ~/.config/gwen/model; fi'
    # launchctl bootout and bootstrap
    ssh "$TARGET" 'launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/dev.vsn.gwen.plist 2>/dev/null || true; launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.vsn.gwen.plist'
    ;;
  switch)
    if [ "$#" -eq 0 ]; then
      echo "Usage: $0 switch <model>" >&2
      exit 2
    fi
    MODEL="$1"
    if [[ ! "$MODEL" =~ ^[A-Za-z0-9._/-]+$ ]]; then
      echo "Invalid model name" >&2
      exit 2
    fi
    # shellcheck disable=SC2029
    ssh "$TARGET" "echo '$MODEL' > ~/.config/gwen/model; launchctl kickstart -k gui/\$(id -u)/dev.vsn.gwen"
    ;;
  status)
    ssh "$TARGET" 'cat ~/.config/gwen/model; launchctl print gui/$(id -u)/dev.vsn.gwen | grep state; tail -n 5 ~/Library/Logs/gwen/serve.log' || true
    # locally curl
    curl -sS -m 5 -K - "http://$HTTP_HOST:8642/v1/models" <<EOF
header = "Authorization: Bearer $(cat "$KEY_FILE")"
EOF
    ;;
  stop)
    ssh "$TARGET" 'launchctl bootout gui/$(id -u)/dev.vsn.gwen'
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
