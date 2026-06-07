#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/docker-compose.yml" ]; then
  PROJECT_DIR="$SCRIPT_DIR"
else
  for d in "$HOME/Desktop/rino-creative-studio" "$HOME/Documents/rino-creative-studio" "$HOME/Downloads/rino-creative-studio" "$HOME/rino-creative-studio"; do
    if [ -f "$d/docker-compose.yml" ]; then PROJECT_DIR="$d"; break; fi
  done
fi
[ -z "${PROJECT_DIR:-}" ] && echo "❌ Cannot find project folder." && exit 1
cd "$PROJECT_DIR"
echo "🛑 Stopping Rino Creative Studio…"
docker compose --profile mcp down
echo "✅ Stopped."
