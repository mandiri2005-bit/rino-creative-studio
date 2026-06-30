"""
batch_engine.py — Genuine async Google Batch API for the Image "Batch" tool.

NATIVE GOOGLE ONLY (no aggregators). One submit = one Gemini Batch job built from
INLINED requests (no JSONL upload, no GCS) — results come back inline on
`job.dest.inlined_responses`, parallel to the input order.

Auth failover (both first-party Google): Vertex OAuth first, Developer API key
second. The OAuth client is injected by laozhang_api (`_genai_client()`, which
owns the Vertex creds); the API-key client is built here from GEMINI_API_KEY. The
winning path is recorded as auth_mode so polling re-acquires the same client (a
Vertex job name is only resolvable on the Vertex client, an API-key job only on
the API-key client).

All blocking SDK calls run via asyncio.to_thread so they never stall the loop.
"""
import os
import base64
import asyncio
import logging

from google import genai
from google.genai import types as gt

log = logging.getLogger("batch_engine")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# Server-side cap on images per batch (admission guard; the UI caps lower).
BATCH_MAX_ROWS = int(os.getenv("BATCH_MAX_ROWS", "10"))

# Google JobState buckets (terminal = stop polling).
_TERMINAL_OK  = {"JOB_STATE_SUCCEEDED", "JOB_STATE_PARTIALLY_SUCCEEDED"}
_TERMINAL_BAD = {"JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}

_apikey_singleton = None

# Explicit safety thresholds on native-Google batch image gen (D). Default BLOCK_ONLY_HIGH = block only
# high-confidence harmful content: a strong floor without nuking legit creative prompts (the pre-dispatch
# text gate in laozhang_api catches egregious prompts before we even hold credits; this is the model-level
# backstop). Rino-tunable via IMAGE_SAFETY_THRESHOLD on the Python service.
_SAFETY_THRESHOLD = os.getenv("IMAGE_SAFETY_THRESHOLD", "BLOCK_ONLY_HIGH")
_SAFETY_CATEGORIES = (
    "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT",
)


def _safety_settings():
    """[SafetySetting] for GenerateContentConfig, or None if the SDK version rejects the shape
    (graceful: an older SDK simply generates without the explicit overrides → provider defaults)."""
    try:
        return [gt.SafetySetting(category=c, threshold=_SAFETY_THRESHOLD) for c in _SAFETY_CATEGORIES]
    except Exception as e:  # pragma: no cover — SDK shape drift
        log.warning("[batch] safety_settings unavailable on this SDK: %s", e)
        return None


def _apikey_client():
    """Cached Developer-API (API-key) google-genai client, or None if no key set."""
    global _apikey_singleton
    if _apikey_singleton is not None:
        return _apikey_singleton
    if not GEMINI_API_KEY:
        return None
    _apikey_singleton = genai.Client(api_key=GEMINI_API_KEY)
    return _apikey_singleton


def have_batch_auth(oauth_client) -> bool:
    """True iff at least one native-Google auth path is available (OAuth or API key).
    Checked BEFORE holding credits so a misconfigured deploy 503s instead of stranding a hold."""
    return bool(oauth_client) or bool(GEMINI_API_KEY)


def state_name(state) -> str:
    """JobState enum / str → its canonical 'JOB_STATE_*' name."""
    if state is None:
        return "JOB_STATE_UNSPECIFIED"
    return getattr(state, "name", None) or str(state)


def is_terminal(name: str) -> bool:
    return name in _TERMINAL_OK or name in _TERMINAL_BAD


def is_ok_terminal(name: str) -> bool:
    return name in _TERMINAL_OK


def _build_inline_requests(prompts, aspect):
    """One InlinedRequest per prompt — image-out config, optional aspect ratio. No
    per-request model (inherits the batch-level model). Responses return in this order."""
    image_cfg = gt.ImageConfig(aspect_ratio=aspect) if aspect else None
    _safety = _safety_settings()
    _cfg_kw = dict(response_modalities=["IMAGE"], image_config=image_cfg)
    if _safety:
        _cfg_kw["safety_settings"] = _safety
    try:
        cfg = gt.GenerateContentConfig(**_cfg_kw)
    except Exception:                              # SDK rejects safety_settings shape → drop it, keep gen
        cfg = gt.GenerateContentConfig(response_modalities=["IMAGE"], image_config=image_cfg)
    reqs = []
    for p in prompts:
        reqs.append(gt.InlinedRequest(
            contents=[gt.Content(role="user", parts=[gt.Part(text=str(p or ""))])],
            config=cfg,
        ))
    return reqs


async def submit(*, oauth_client, vertex_model, developer_model, prompts, aspect, display_name):
    """Submit one inline batch, trying Vertex OAuth then Developer API key.
    Returns (gemini_job_name, auth_mode, state_name). Raises RuntimeError if BOTH
    native-Google paths fail (caller refunds the hold)."""
    errs = []
    reqs = _build_inline_requests(prompts, aspect)
    cfg = gt.CreateBatchJobConfig(display_name=display_name)

    # 1) Vertex OAuth (preferred)
    if oauth_client is not None:
        try:
            job = await asyncio.to_thread(
                oauth_client.batches.create, model=vertex_model, src=reqs, config=cfg)
            return job.name, "oauth", state_name(getattr(job, "state", None))
        except Exception as e:
            errs.append(f"oauth({vertex_model}): {e}")
            log.warning("[batch] Vertex OAuth submit failed: %s", e)

    # 2) Developer API key (failover)
    ak = _apikey_client()
    if ak is not None:
        try:
            dev_reqs = _build_inline_requests(prompts, aspect)   # fresh objects per client
            job = await asyncio.to_thread(
                ak.batches.create, model=developer_model, src=dev_reqs, config=cfg)
            return job.name, "apikey", state_name(getattr(job, "state", None))
        except Exception as e:
            errs.append(f"apikey({developer_model}): {e}")
            log.warning("[batch] Developer API-key submit failed: %s", e)

    raise RuntimeError("native Google batch submit failed — " + " | ".join(errs or ["no auth"]))


async def poll(*, oauth_client, auth_mode, gemini_job_name):
    """Fetch the BatchJob via the SAME client family that submitted it."""
    client = oauth_client if auth_mode == "oauth" else _apikey_client()
    if client is None:
        raise RuntimeError(f"no native-Google client for auth_mode={auth_mode!r}")
    return await asyncio.to_thread(client.batches.get, name=gemini_job_name)


def _err_str(err) -> str:
    try:
        msg = getattr(err, "message", None) or getattr(err, "status", None)
        return str(msg or err)[:200]
    except Exception:
        return "request failed"


def _first_image(resp):
    """First inline image (bytes, mime) in a GenerateContentResponse, or None."""
    try:
        for cand in (getattr(resp, "candidates", None) or []):
            content = getattr(cand, "content", None)
            for part in (getattr(content, "parts", None) or []):
                inline = getattr(part, "inline_data", None)
                data = getattr(inline, "data", None) if inline is not None else None
                if data:
                    if isinstance(data, str):
                        data = base64.b64decode(data)
                    mime = getattr(inline, "mime_type", None) or "image/png"
                    return data, mime
    except Exception as e:
        log.warning("[batch] image extract error: %s", e)
    return None


def extract_results(job):
    """Map a terminal BatchJob to a list PARALLEL to the input prompts. Each element is
    ('ok', bytes, mime) for a produced image or ('err', reason, None) for a failed slot.
    Inline batch responses preserve request order (incl. failed slots), so index i here
    corresponds to prompt i."""
    dest = getattr(job, "dest", None)
    responses = list(getattr(dest, "inlined_responses", None) or []) if dest else []
    out = []
    for r in responses:
        err = getattr(r, "error", None)
        if err:
            out.append(("err", _err_str(err), None))
            continue
        img = _first_image(getattr(r, "response", None))
        if img is None:
            out.append(("err", "no image in response", None))
        else:
            out.append(("ok", img[0], img[1]))
    return out
