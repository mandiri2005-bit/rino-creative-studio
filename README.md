# 🎬 Rino Creative Studio

All-in-one AI creative platform. 9 tools, one interface.

| Tool | Engine | Description |
|------|--------|-------------|
| 💬 Chat | LaoZhang · multi-model | SSE streaming chat, file upload, MCP file tools |
| 🖼️ Image Gen | LaoZhang | 10+ models (Flux, GPT-Image-2, Gemini native) |
| 🎬 Veo | Google Veo 3 | Text/image → video (720p to 4K) |
| 🎥 Sora | OpenAI Sora 2 | Text/image → video |
| ✨ Whisk | Flux Kontext | Subject + Scene + Style → remixed image |
| 🎬 Flow | Gemini | Script → cinematic storyboard (+ optional images) |
| 📦 Batch Images | Gemini Batch API | Async bulk generation (50% cheaper) |
| 🎙️ TTS | Gemini TTS | Multi-key rotation, per-paragraph WAV files |
| 🌄 Imagen | Google Imagen 4 | Real-time sequential generation |
| 📁 MCP Files | BM25/Semantic RAG | Local folder search (optional sidecar) |

## Quick start

```bash
cp .env.example .env
# fill in LAOZHANG_API_KEY and GEMINI_API_KEY

docker compose up -d --build
open http://localhost:8080
```

## With MCP file search

```bash
# Put files in ./mcp-folder (or set MCP_FOLDER= in .env)
docker compose --profile mcp up -d --build
```

## .env reference

```env
LAOZHANG_API_KEY=...       # Chat · Image · Veo · Sora · Whisk · Flow
LAOZHANG_IMAGE_API_KEY=    # Optional — defaults to LAOZHANG_API_KEY
GEMINI_API_KEY=...          # Batch · TTS · Imagen

MCP_FOLDER=./mcp-folder    # Folder to index (MCP mode)
MCP_SEARCH_MODE=bm25       # bm25 | hybrid | api | local
```

## Architecture

```
Browser :8080
  └── nginx
       └── /api/*  →  Node.js :3000
            ├── Batch · TTS · Imagen  (direct @google/genai)
            └── Chat · Image · Veo · Sora · Whisk · Flow
                 └── Python FastAPI :8000

  (optional)
  MCP sidecar :8001  ←  Node proxies /api/mcp/*
```

## API keys per tool

- **Chat / Image / Veo / Sora / Whisk / Flow** — set in Settings panel (browser localStorage) or `.env LAOZHANG_API_KEY`
- **Batch Images / TTS / Imagen** — `.env GEMINI_API_KEY`; TTS supports multiple keys (one per line in UI) for quota rotation
# rino-creative-studio
# Updated at Sun Jun  7 15:06:49 +01 2026
