FROM python:3.11-slim
WORKDIR /app

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

# Copy top-level .py files (laozhang_api.py, narration_api.py etc.) + server-side data
COPY *.py ./
# Project Dalang packages — subdirectories are NOT matched by `COPY *.py`, so they
# must be copied explicitly or the orchestrator/pakem engine is missing at runtime
# (narration_api import fails → /narration routes silently unavailable).
COPY orchestrator/ ./orchestrator/
COPY pakem/ ./pakem/
COPY eval/ ./eval/
COPY review_personas.json ./

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


RUN mkdir -p Veo_outputs Sora_outputs

EXPOSE 8000
CMD ["uvicorn", "laozhang_api:app", "--host", "0.0.0.0", "--port", "8000"]
