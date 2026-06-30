"""
batch_engine.py — Vertex AI Batch prediction over GCS for the Image "Batch" tool.

NATIVE GOOGLE ONLY (no aggregators). One submit = one Vertex batch job:
  build JSONL (one request line per prompt) → upload to
  gs://BUCKET/PREFIX/<job_id>/input.jsonl → batches.create(model, src=gs://…input.jsonl,
  dest=gs://…/output/) on a REGIONAL Vertex client → poll JOB_STATE_* → on a terminal
  state download the output predictions.jsonl and map each produced image back to its
  prompt by the ECHOED request text (Vertex does NOT guarantee output order).

Why GCS and not inline: the Vertex Batch API REQUIRES a GCS/BigQuery input source
("Exactly one of gcs_uri/bigquery_uri/vertex_dataset_name must be set") — it rejects
inline requests outright. And the Developer-API-key batch path is dead for us (the key
is API_KEY_SERVICE_BLOCKED for BatchService, AND that API is inline-only, incompatible
with GCS). So batch is Vertex-OAuth + GCS only — proven end-to-end by the live probe
(gemini-2.5-flash-image, us-central1, image extracted from output JSONL).

Auth: an OAuth refresh-token (GCP_REFRESH_TOKEN/CLIENT_ID/SECRET) minted with the
cloud-platform scope — covers BOTH Vertex aiplatform AND GCS read/write, so no service
account is needed. The Vertex BATCH endpoint is PER-MODEL: gemini-2.5-flash-image runs on the
REGIONAL endpoint (GCP_BATCH_LOCATION, default us-central1, region-matched to the bucket); the
gemini-3.x image models are served ONLY on the GLOBAL endpoint (a regional endpoint 404s
"PublisherModel ... does not exist"). See _location_for_model — the same OAuth creds + the same
GCS bucket serve both.

All blocking SDK / GCS calls run via asyncio.to_thread so they never stall the loop.
"""
import os
import json
import base64
import asyncio
import logging
from collections import deque

log = logging.getLogger("batch_engine")

# ── config (env) ──────────────────────────────────────────────────────────────
GCP_PROJECT_ID     = os.environ.get("GCP_PROJECT_ID", "")
GCP_REFRESH_TOKEN  = os.environ.get("GCP_REFRESH_TOKEN", "")
GCP_CLIENT_ID      = os.environ.get("GCP_CLIENT_ID", "")
GCP_CLIENT_SECRET  = os.environ.get("GCP_CLIENT_SECRET", "")
# Vertex BATCH needs a REGIONAL endpoint (location != 'global') that MATCHES the bucket region.
GCP_BATCH_LOCATION = (os.environ.get("GCP_BATCH_LOCATION") or "us-central1").strip().lower()
# GCS bucket for batch JSONL I/O (REGIONAL, same region as GCP_BATCH_LOCATION).
BATCH_GCS_BUCKET   = os.environ.get("BATCH_GCS_BUCKET", "").strip().replace("gs://", "").strip("/")
BATCH_GCS_PREFIX   = (os.environ.get("BATCH_GCS_PREFIX") or "image-batch").strip("/")
# Best-effort delete of a settled job's transient I/O objects (bucket lifecycle TTL is the real backstop).
BATCH_GCS_CLEANUP  = os.getenv("BATCH_GCS_CLEANUP", "1") not in ("0", "false", "False", "")
# Server-side cap on images per batch (admission guard; the UI caps lower).
BATCH_MAX_ROWS     = int(os.getenv("BATCH_MAX_ROWS", "10"))

# Google JobState buckets (terminal = stop polling).
_TERMINAL_OK  = {"JOB_STATE_SUCCEEDED", "JOB_STATE_PARTIALLY_SUCCEEDED"}
_TERMINAL_BAD = {"JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}

# Explicit safety thresholds on native-Google batch image gen (D). Default BLOCK_ONLY_HIGH = block only
# high-confidence harmful content: a strong floor without nuking legit creative prompts (the pre-dispatch
# text gate in laozhang_api catches egregious prompts before we even hold credits; this is the model-level
# backstop). REST shape (sibling of generationConfig in the request). Rino-tunable via IMAGE_SAFETY_THRESHOLD.
_SAFETY_THRESHOLD = os.getenv("IMAGE_SAFETY_THRESHOLD", "BLOCK_ONLY_HIGH")
_SAFETY_CATEGORIES = (
    "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT",
)


# ── lazy, cached clients (mirror the proven probe: own creds w/ cloud-platform scope) ──
_creds_singleton = None
_genai_clients = {}          # location -> genai.Client (per-endpoint: gemini-3 = 'global', 2.5 = regional)
_storage_singleton = None


def _credentials():
    """OAuth user credentials from the refresh token, scoped cloud-platform (covers Vertex + GCS).
    Cached. Built lazily so import never touches the network."""
    global _creds_singleton
    if _creds_singleton is not None:
        return _creds_singleton
    from google.oauth2.credentials import Credentials
    _creds_singleton = Credentials(
        token=None, refresh_token=GCP_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GCP_CLIENT_ID, client_secret=GCP_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    return _creds_singleton


def _location_for_model(model: str) -> str:
    """Which Vertex endpoint serves `model` for batch. Gemini-3.x image models exist ONLY on the
    GLOBAL endpoint (a regional endpoint 404s "PublisherModel does not exist"); gemini-2.5-flash-image
    (nano-banana) is regional. Proven by live probe (gemini-3-pro-image-preview /
    gemini-3.1-flash-image-preview reach JOB_STATE on 'global', 404 on us-central1)."""
    return "global" if (model or "").lower().startswith("gemini-3") else GCP_BATCH_LOCATION


def _location_from_job_name(name: str) -> str:
    """The location segment of a batch-job resource name
    (projects/.../locations/<loc>/batchPredictionJobs/...), so poll hits the endpoint the job
    was created on. Falls back to the regional default if the name is unparseable."""
    try:
        parts = (name or "").split("/")
        return parts[parts.index("locations") + 1] or GCP_BATCH_LOCATION
    except (ValueError, IndexError):
        return GCP_BATCH_LOCATION


def _genai_for(location):
    """google.genai Vertex client pinned to `location`, cached per-location. None if unconfigured.
    gemini-3 batch needs location='global', gemini-2.5 needs the regional endpoint — both share the
    same OAuth creds and the same GCS bucket."""
    if not have_batch_auth():
        return None
    loc = (location or GCP_BATCH_LOCATION).strip().lower()
    client = _genai_clients.get(loc)
    if client is None:
        from google import genai
        client = genai.Client(vertexai=True, project=GCP_PROJECT_ID, location=loc,
                              credentials=_credentials())
        _genai_clients[loc] = client
    return client


def _storage():
    """GCS client (same OAuth creds). None if unconfigured."""
    global _storage_singleton
    if _storage_singleton is not None:
        return _storage_singleton
    if not have_batch_auth():
        return None
    from google.cloud import storage
    _storage_singleton = storage.Client(project=GCP_PROJECT_ID, credentials=_credentials())
    return _storage_singleton


def have_batch_auth() -> bool:
    """True iff Vertex GCS batch is FULLY configured: OAuth creds + project + a GCS bucket.
    A capability check (not mere presence), so a misconfigured deploy 503s on submit instead of
    holding credits it can never settle. (The old inline design only checked auth presence and
    so shipped broken — it 502'd at submit after the hold; this gate prevents that class.)"""
    return bool(GCP_PROJECT_ID and GCP_REFRESH_TOKEN and GCP_CLIENT_ID
                and GCP_CLIENT_SECRET and BATCH_GCS_BUCKET)


def state_name(state) -> str:
    """JobState enum / str → its canonical 'JOB_STATE_*' name."""
    if state is None:
        return "JOB_STATE_UNSPECIFIED"
    return getattr(state, "name", None) or str(state)


def is_terminal(name: str) -> bool:
    return name in _TERMINAL_OK or name in _TERMINAL_BAD


def is_ok_terminal(name: str) -> bool:
    return name in _TERMINAL_OK


def _err_str(err) -> str:
    try:
        msg = getattr(err, "message", None) or getattr(err, "status", None)
        return str(msg or err)[:200]
    except Exception:
        return "request failed"


# ── GCS object paths (deterministic per job_id) ───────────────────────────────
def _job_prefix(job_id) -> str:
    return f"{BATCH_GCS_PREFIX}/{job_id}"


def _input_blob(job_id) -> str:
    return f"{_job_prefix(job_id)}/input.jsonl"


def _output_prefix(job_id) -> str:
    return f"{_job_prefix(job_id)}/output/"


# ── JSONL request file (Vertex batch GCS line shape; camelCase REST, NOT SDK snake_case) ──
def _safety_settings_rest():
    """REST safetySettings array (sibling of generationConfig), or None to omit."""
    if not _SAFETY_THRESHOLD:
        return None
    return [{"category": c, "threshold": _SAFETY_THRESHOLD} for c in _SAFETY_CATEGORIES]


def _build_jsonl(prompts, aspect) -> str:
    """One JSON line per prompt. Each line is {"request": <GenerateContentRequest>}. Image models
    need responseModalities to include IMAGE (TEXT kept too — they may emit a text part alongside).
    Optional aspect ratio via generationConfig.imageConfig.aspectRatio; optional safetySettings
    backstop. Output order is NOT guaranteed — we re-associate by the echoed text, not position."""
    gen_cfg = {"responseModalities": ["TEXT", "IMAGE"]}
    if aspect:
        gen_cfg["imageConfig"] = {"aspectRatio": aspect}
    safety = _safety_settings_rest()
    lines = []
    for p in prompts:
        req = {"contents": [{"role": "user", "parts": [{"text": str(p or "")}]}],
               "generationConfig": gen_cfg}
        if safety:
            req["safetySettings"] = safety
        lines.append(json.dumps({"request": req}))
    return "\n".join(lines) + "\n"


async def submit(*, model, prompts, aspect, job_id, display_name):
    """Upload the JSONL request file to GCS, then create a Vertex batch over it. Returns
    (gemini_job_name, auth_mode, state_name). auth_mode is always 'oauth' (kept for the DB column
    + poll signature). Raises RuntimeError on any failure (caller refunds the hold)."""
    location = _location_for_model(model)
    client = _genai_for(location)
    gcs = _storage()
    if client is None or gcs is None:
        raise RuntimeError("Vertex GCS batch not configured (OAuth creds or bucket missing)")
    from google.genai import types as gt

    jsonl   = _build_jsonl(prompts, aspect)
    in_blob = _input_blob(job_id)
    in_uri  = f"gs://{BATCH_GCS_BUCKET}/{in_blob}"
    out_uri = f"gs://{BATCH_GCS_BUCKET}/{_output_prefix(job_id)}"

    def _do():
        # bucket() is a lazy ref (no metadata GET); upload writes the input file.
        gcs.bucket(BATCH_GCS_BUCKET).blob(in_blob).upload_from_string(
            jsonl, content_type="application/json")
        return client.batches.create(
            model=model, src=in_uri,
            config=gt.CreateBatchJobConfig(dest=out_uri, display_name=display_name))

    try:
        job = await asyncio.to_thread(_do)
    except Exception as e:
        raise RuntimeError(f"Vertex GCS batch submit failed ({model}@{location}): {e}")
    return job.name, "oauth", state_name(getattr(job, "state", None))


async def poll(*, gemini_job_name, auth_mode=None):
    """Fetch the Vertex BatchJob. auth_mode is ignored (always Vertex/OAuth now) — kept in the
    signature so the existing reconcile call site doesn't have to special-case it. The endpoint is
    derived from the job name's location segment so a 'global' (gemini-3) job is polled on the
    global endpoint and a regional (gemini-2.5) job on the regional one."""
    client = _genai_for(_location_from_job_name(gemini_job_name))
    if client is None:
        raise RuntimeError("Vertex GCS batch not configured")
    return await asyncio.to_thread(client.batches.get, name=gemini_job_name)


def _image_from_response_json(resp: dict):
    """First inline image (bytes, mime) in a REST GenerateContentResponse dict, or None.
    A part whose base64 is corrupt/undecodable is SKIPPED (try the next part), never raised — so one
    malformed inlineData can't abort extraction of the rest of the batch. A line where nothing decodes
    returns None (its slot degrades to 'err'); a genuinely-transient GCS *read* failure is a different
    thing entirely and still raises from _download_texts so the caller defers."""
    for cand in (resp.get("candidates") or []):
        for part in ((cand.get("content") or {}).get("parts") or []):
            inl = part.get("inlineData") or part.get("inline_data") or {}
            data = inl.get("data")
            if not data:
                continue
            try:
                raw = base64.b64decode(data) if isinstance(data, str) else data
            except Exception:
                continue
            mime = inl.get("mimeType") or inl.get("mime_type") or "image/png"
            return raw, mime
    return None


def _echoed_text(row: dict) -> str:
    """The prompt text echoed back in an output line's request, for re-association."""
    req = row.get("request") or {}
    for c in (req.get("contents") or []):
        for part in (c.get("parts") or []):
            t = part.get("text")
            if t:
                return t
    return ""


def _output_prefix_for(gjob, job_id) -> str:
    """The GCS prefix to scan for output. Prefer the job's reported dest; fall back to the
    deterministic prefix we requested (robust if the SDK doesn't surface dest.gcs_uri)."""
    dest = getattr(gjob, "dest", None)
    gcs_out = getattr(dest, "gcs_uri", None) if dest is not None else None
    pfx = f"gs://{BATCH_GCS_BUCKET}/"
    if gcs_out and isinstance(gcs_out, str) and gcs_out.startswith(pfx):
        return gcs_out[len(pfx):].lstrip("/")
    return _output_prefix(job_id)


async def extract_results(gjob, prompts, job_id):
    """Download the batch's output predictions.jsonl from GCS and map each produced image back to
    its INPUT prompt index. Vertex does NOT guarantee output order, so we re-associate by the echoed
    request text (a multiset keyed by prompt text — handles reordering AND duplicate prompts). Any
    produced image whose echoed text doesn't match a remaining slot (e.g. whitespace normalization)
    fills the earliest still-unfilled slot, so a produced image is NEVER lost (billing is by delivered
    COUNT, and the user still gets every image). Returns a list PARALLEL to `prompts`:
    ('ok', bytes, mime) for a produced image | ('err', reason, None) for a failed/missing slot.
    Raises on a GCS read failure (caller defers — does not burn the terminal transition)."""
    gcs = _storage()
    if gcs is None:
        raise RuntimeError("Vertex GCS batch not configured")
    total = len(prompts)
    results = [("err", "no image in batch output", None) for _ in range(total)]
    if total == 0:
        return results

    # prompt text → FIFO queue of input indices (handles duplicate prompts)
    by_text = {}
    for i, p in enumerate(prompts):
        by_text.setdefault(str(p or ""), deque()).append(i)

    out_prefix = _output_prefix_for(gjob, job_id)

    def _download_texts():
        texts = []
        for b in gcs.list_blobs(BATCH_GCS_BUCKET, prefix=out_prefix):
            if b.name.endswith(".jsonl") or "prediction" in b.name:
                texts.append(b.download_as_text())
        return texts

    try:
        texts = await asyncio.to_thread(_download_texts)
    except Exception as e:
        raise RuntimeError(f"GCS output read failed for {job_id}: {e}")

    unmatched_imgs = deque()        # produced images whose echoed text didn't map to a slot
    for text in texts:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            # The whole per-line body is guarded: a malformed/corrupt line (bad JSON, weird shape,
            # undecodable image) degrades to "skip / leave slot err" and we move on — it must NEVER
            # abort the loop and discard the rest of an already-downloaded shard. (Transient GCS READ
            # failures are caught earlier in _download_texts and re-raised so the caller defers.)
            try:
                row = json.loads(line)
                resp = row.get("response") or {}
                img = _image_from_response_json(resp) if resp else None
                q = by_text.get(_echoed_text(row))
                idx = q.popleft() if (q and len(q)) else None
                if img is None:
                    if idx is not None:
                        status = row.get("status")
                        results[idx] = ("err", (str(status)[:160] if status else None)
                                        or "no image in response", None)
                    continue
                if idx is not None:
                    results[idx] = ("ok", img[0], img[1])
                else:
                    unmatched_imgs.append(img)
            except Exception as e:
                log.warning("[batch] skipped malformed output line for %s: %s", job_id, e)
                continue

    # fallback: place any leftover produced images into the earliest still-unfilled slots in order
    if unmatched_imgs:
        for i in range(total):
            if not unmatched_imgs:
                break
            if results[i][0] != "ok":
                img = unmatched_imgs.popleft()
                results[i] = ("ok", img[0], img[1])

    return results


async def cleanup(job_id) -> None:
    """Best-effort delete of a settled job's GCS input+output objects (transient I/O). Never raises —
    a leftover is harmless (a bucket lifecycle TTL rule is the real backstop). Only the winning
    settler calls this, and only after the row is terminal, so no live reconcile re-reads the output."""
    if not BATCH_GCS_CLEANUP:
        return
    gcs = _storage()
    if gcs is None:
        return

    def _do():
        for b in list(gcs.list_blobs(BATCH_GCS_BUCKET, prefix=f"{_job_prefix(job_id)}/")):
            try:
                b.delete()
            except Exception:
                pass

    try:
        await asyncio.to_thread(_do)
    except Exception as e:
        log.warning("[batch] gcs cleanup failed for %s: %s", job_id, e)
