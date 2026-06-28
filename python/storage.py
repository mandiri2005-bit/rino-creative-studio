# -*- coding: utf-8 -*-
"""
storage.py — Object-storage abstraction (Cloudflare R2 / any S3-compatible).

ONE module wraps the bucket so no service writes to local disk as the source of
truth. Backed by boto3 (R2 speaks the S3 API). Reads credentials from the same
STORAGE_* env the rest of the stack already declares (.env / docker-compose):

    STORAGE_ENDPOINT      https://<accountid>.r2.cloudflarestorage.com
    STORAGE_ACCESS_KEY    R2 API token — Access Key ID
    STORAGE_SECRET_KEY    R2 API token — Secret Access Key
    STORAGE_BUCKET        e.g. rino-assets
    STORAGE_REGION        "auto" for R2 (default)
    STORAGE_PUBLIC_URL    optional; unused while we serve via signed URLs only

Key convention (matches assets.s3_key in 0008_create_assets.sql):

    tenants/{tenant_id}/jobs/{job_id}/{asset_type}/{filename}

`is_configured()` is False until the access key + secret are filled in, so
callers can keep writing to local disk as a fallback until R2 is provisioned.

Sync core (boto3 is blocking) + async wrappers (asyncio.to_thread) so FastAPI
handlers don't block the event loop.
"""
from __future__ import annotations

import os
import asyncio
import logging
from typing import Optional

log = logging.getLogger("storage")

# ── Config from env ──────────────────────────────────────────────────────────
# Accept the STORAGE_* names first, then fall back to the R2_* names the Railway env
# actually uses (R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_BUCKET).
# Without this fallback is_configured() was False → every asset persist was silently
# skipped (image gen never reached R2). 2026-06-28.
ENDPOINT   = (os.getenv("STORAGE_ENDPOINT")   or os.getenv("R2_ENDPOINT")          or "").strip()
ACCESS_KEY = (os.getenv("STORAGE_ACCESS_KEY") or os.getenv("R2_ACCESS_KEY_ID")     or "").strip()
SECRET_KEY = (os.getenv("STORAGE_SECRET_KEY") or os.getenv("R2_SECRET_ACCESS_KEY") or "").strip()
BUCKET     = (os.getenv("STORAGE_BUCKET")     or os.getenv("R2_BUCKET")            or "").strip()
REGION     = (os.getenv("STORAGE_REGION")     or os.getenv("R2_REGION")            or "auto").strip() or "auto"

_DEFAULT_EXPIRY = 600  # 10 minutes — signed-URL lifetime


def is_configured() -> bool:
    """True only when every credential needed to talk to R2 is present."""
    return all([ENDPOINT, ACCESS_KEY, SECRET_KEY, BUCKET])


# ── Lazy singleton client ────────────────────────────────────────────────────
_client = None


def _c():
    global _client
    if _client is None:
        if not is_configured():
            raise RuntimeError(
                "storage not configured — set STORAGE_ENDPOINT/ACCESS_KEY/"
                "SECRET_KEY/BUCKET in .env"
            )
        import boto3
        from botocore.config import Config
        _client = boto3.client(
            "s3",
            endpoint_url=ENDPOINT,
            aws_access_key_id=ACCESS_KEY,
            aws_secret_access_key=SECRET_KEY,
            region_name=REGION,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},  # R2 works with path-style
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        log.info("storage client ready (bucket=%s endpoint=%s)", BUCKET, ENDPOINT)
    return _client


# ── Key builder ──────────────────────────────────────────────────────────────
def build_key(tenant_id: str, job_id: str, asset_type: str, filename: str) -> str:
    """tenants/{tenant_id}/jobs/{job_id}/{asset_type}/{filename}"""
    safe = str(filename).lstrip("/")
    job = str(job_id) if job_id else "_"
    return f"tenants/{tenant_id}/jobs/{job}/{asset_type}/{safe}"


# ── Sync core ────────────────────────────────────────────────────────────────
def upload_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Upload bytes to R2; returns the object KEY (NOT a URL). R2 = source of truth — callers
    persist the key (e.g. assets.s3_key) and mint a signed URL on read via signed_url/asigned_url.
    Never hand this return value to an HTTP fetch; it has no scheme."""
    _c().put_object(Bucket=BUCKET, Key=key, Body=data, ContentType=content_type)
    return key


def upload_file(key: str, path: str, content_type: Optional[str] = None) -> str:
    extra = {"ContentType": content_type} if content_type else None
    _c().upload_file(path, BUCKET, key, ExtraArgs=extra)
    return key


def download_bytes(key: str) -> bytes:
    resp = _c().get_object(Bucket=BUCKET, Key=key)
    return resp["Body"].read()


def signed_url(key: str, expiry_seconds: int = _DEFAULT_EXPIRY) -> str:
    return _c().generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": key},
        ExpiresIn=int(expiry_seconds),
    )


def exists(key: str) -> bool:
    from botocore.exceptions import ClientError
    try:
        _c().head_object(Bucket=BUCKET, Key=key)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def delete(key: str) -> None:
    _c().delete_object(Bucket=BUCKET, Key=key)


# ── Async wrappers (don't block the event loop) ──────────────────────────────
async def aupload_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Async upload_bytes — returns the object KEY (not a URL). See upload_bytes."""
    return await asyncio.to_thread(upload_bytes, key, data, content_type)


async def aupload_file(key: str, path: str, content_type: Optional[str] = None) -> str:
    return await asyncio.to_thread(upload_file, key, path, content_type)


async def adownload_bytes(key: str) -> bytes:
    return await asyncio.to_thread(download_bytes, key)


async def asigned_url(key: str, expiry_seconds: int = _DEFAULT_EXPIRY) -> str:
    return await asyncio.to_thread(signed_url, key, expiry_seconds)


async def aexists(key: str) -> bool:
    return await asyncio.to_thread(exists, key)
