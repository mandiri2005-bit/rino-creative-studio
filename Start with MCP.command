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
[ -f .env ] || cp .env.example .env 2>/dev/null || touch .env
echo "🎬 Starting Rino Creative Studio (+ MCP)…"
docker compose --profile mcp up -d --build
echo "✅ Running at http://localhost:8080 (MCP enabled)"
sleep 2; open http://localhost:8080
