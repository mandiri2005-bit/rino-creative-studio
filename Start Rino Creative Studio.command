#!/bin/bash
# Works whether this file is INSIDE the project folder OR on your Desktop

# ── Find project directory ────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/docker-compose.yml" ]; then
  PROJECT_DIR="$SCRIPT_DIR"
else
  # Search common locations
  for d in \
    "$HOME/Desktop/rino-creative-studio" \
    "$HOME/Documents/rino-creative-studio" \
    "$HOME/Downloads/rino-creative-studio" \
    "$HOME/rino-creative-studio"; do
    if [ -f "$d/docker-compose.yml" ]; then
      PROJECT_DIR="$d"; break
    fi
  done
fi

if [ -z "${PROJECT_DIR:-}" ]; then
  echo "❌  Cannot find rino-creative-studio folder."
  echo "    Searched Desktop, Documents, Downloads."
  echo "    Please move this script into the rino-creative-studio folder and try again."
  read -rp "Press Enter to close…"; exit 1
fi

cd "$PROJECT_DIR"
echo "📁 Project: $PROJECT_DIR"

# ── Create .env if missing ────────────────────────────────────────────────────
if ! [ -f .env ]; then
  if [ -f .env.example ]; then
    cp .env.example .env
  else
    cat > .env << 'ENVEOF'
# ── LaoZhang API (Chat · Image · Veo · Sora · Whisk · Flow)
LAOZHANG_API_KEY=

# ── Gemini (Batch Images · TTS · Imagen)
GEMINI_API_KEY=

# ── MCP File Search (optional, only needed with --profile mcp)
MCP_FOLDER=./mcp-folder
MCP_SEARCH_MODE=bm25
ENVEOF
  fi
  echo "⚠️  Created .env — edit it to add your API keys:"
  echo "    LAOZHANG_API_KEY=..."
  echo "    GEMINI_API_KEY=..."
fi

# ── Start ─────────────────────────────────────────────────────────────────────
echo ""
echo "🎬 Starting Rino Creative Studio…"
docker compose up -d --build

echo ""
echo "✅  Running at http://localhost:8080"
sleep 2
open http://localhost:8080
