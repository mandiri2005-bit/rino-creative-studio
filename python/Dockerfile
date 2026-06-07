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

# Copy top-level .py files (laozhang_api.py etc.)
COPY *.py ./

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


RUN mkdir -p Veo_outputs Sora_outputs

EXPOSE 8000
CMD ["uvicorn", "laozhang_api:app", "--host", "0.0.0.0", "--port", "8000"]
