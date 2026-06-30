FROM python:3.11-slim
WORKDIR /app

# ffmpeg/ffprobe — system binaries required by recipe_assembly.py (the Product Ad recipe stitches,
# crops, ducks/muxes VO+music, and burns caption/CTA/logo overlays via ffmpeg). python:3.11-slim ships
# without them, so without this every recipe assembly raises RuntimeError. Cleaned up to keep the layer small.
# Fonts: Debian's ffmpeg has drawtext (libfreetype) + subtitles (libass), but BOTH need real font files
# to render — drawtext caption/CTA text AND the Spokesperson karaoke (\kf word-fill ASS via libass).
# With --no-install-recommends, no fonts are pulled, so install them explicitly (mirrors backend image);
# _font_file() resolves Liberation/DejaVu and libass finds the family by name. Without this, captions
# fail (drawtext: no default font) and karaoke renders blank.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-liberation fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Install CPU-only PyTorch first to avoid downloading CUDA (~500MB)
RUN pip install --no-cache-dir \
    torch --index-url https://download.pytorch.org/whl/cpu

RUN pip install --no-cache-dir \
    fastapi \
    python-multipart \
    "uvicorn[standard]" \
    openai \
    requests \
    numpy \
    pdfplumber \
    python-docx \
    openpyxl \
    # ── RAG dependencies ──────────────────────────────────────────────────
    qdrant-client \
    sentence-transformers \
    tqdm \
    python-jose[cryptography] \
    httpx \
    asyncpg \
    python-dotenv

# Copy top-level .py files (laozhang_api.py, narration_api.py etc.)
COPY *.py ./
# Project Dalang packages — subdirectories are NOT matched by `COPY *.py`, so they
# must be copied explicitly or the orchestrator/pakem engine is missing at runtime
# (narration_api import fails → /narration routes silently unavailable).
COPY orchestrator/ ./orchestrator/
COPY pakem/ ./pakem/
COPY eval/ ./eval/
COPY review_personas.json ./
# Wimba Image provider registry (model meta + COGS + failover chains). NOT matched by `COPY *.py`,
# so without this `import image_providers` raises FileNotFoundError → /image/catalog + /image/<op> 500.
COPY image_registry.json ./
# Wimba Video provider registry (same story as image_registry.json). NOT matched by `COPY *.py`, so
# without this `import video_providers` raises FileNotFoundError → every /video-tools + recipe video
# dispatch 500s. (Same class of bug already fixed once for image_registry.json.)
COPY video_registry.json ./
# Wimba unified pricing catalog (per-provider COGS/slugs → pricing.py resolver: cheapest-first failover +
# worst-case-hold billing). NOT matched by `COPY *.py`, so without this pricing._load() returns {} and the
# resolver SILENTLY degrades to the legacy registry order + cheapest-cogs pricing (no crash, but the
# cheapest-first reorder + option-A boncos guard do nothing). Same class as the registry COPYs above.
COPY pricing_catalog.json ./
# Nusantara Corpus seed + ref images. NOT matched by `COPY *.py`, so without this
# data/nusantara_seed.json is absent at runtime → _load_seed() empty → BM25 retrieval
# returns nothing (the corpus silently does nothing on the BM25 path).
COPY data/ ./data/

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


RUN mkdir -p Veo_outputs Sora_outputs

EXPOSE 8000
CMD ["uvicorn", "laozhang_api:app", "--host", "0.0.0.0", "--port", "8000"]
