# -*- coding: utf-8 -*-
"""
redis_client.py — live job-progress layer.

Progress strings live in Redis under 'job_progress:{job_id}' with a short TTL.
The jobs table (PostgreSQL) remains the durable source of truth for
status/result/error; Redis only fronts the fast-changing progress string so the
status endpoint stays cheap and the DB isn't written on every progress tick.
"""
import os
import re
import logging

import redis.asyncio as aioredis

log = logging.getLogger("redis_client")

_REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
_redis: "aioredis.Redis | None" = None


def _key(job_id: str) -> str:
    return f"job_progress:{job_id}"


async def init_redis() -> None:
    """Create the shared client and ping it. Safe to call once at startup."""
    global _redis
    _redis = aioredis.from_url(_REDIS_URL, decode_responses=True)
    try:
        await _redis.ping()
        _safe = re.sub(r"://([^:/@]+):[^@]+@", r"://\1:****@", _REDIS_URL)
        log.info("Redis connected: %s", _safe)
    except Exception as e:                       # don't kill startup if Redis is down
        log.error("Redis ping failed (progress will fall back to DB): %s", e)


async def close_redis() -> None:
    if _redis is not None:
        try:
            await _redis.aclose()
        except Exception as e:
            log.error("Redis close failed: %s", e)


async def set_progress(job_id: str, text: str, ttl: int = 300) -> None:
    """Write a progress string with TTL. Never raises — progress is best-effort."""
    if _redis is None:
        return
    try:
        await _redis.set(_key(job_id), text, ex=ttl)
    except Exception as e:
        log.warning("set_progress(%s) failed: %s", job_id, e)


async def get_progress(job_id: str) -> "str | None":
    """Return the live progress string, or None if absent / Redis unavailable."""
    if _redis is None:
        return None
    try:
        return await _redis.get(_key(job_id))
    except Exception as e:
        log.warning("get_progress(%s) failed: %s", job_id, e)
        return None


async def delete_progress(job_id: str) -> None:
    if _redis is None:
        return
    try:
        await _redis.delete(_key(job_id))
    except Exception as e:
        log.warning("delete_progress(%s) failed: %s", job_id, e)

# ── Cancel flags ─────────────────────────────────────────────────────────────
# Cross-container cancellation. 'cancel:{key}' = '1' means cancelled; absent
# means not cancelled. TTL auto-cleans orphaned flags. Like progress, these are
# best-effort: a Redis outage must never wedge a running job.
def _cancel_key(key: str) -> str:
    return f"cancel:{key}"


async def set_cancel(key: str) -> None:
    """Mark a job cancelled (TTL 600s). Best-effort — never raises."""
    if _redis is None:
        return
    try:
        await _redis.set(_cancel_key(key), "1", ex=600)
    except Exception as e:
        log.warning("set_cancel(%s) failed: %s", key, e)


async def is_cancelled(key: str) -> bool:
    """True if the cancel flag exists. On Redis error returns False so work
    is never blocked by an outage."""
    if _redis is None:
        return False
    try:
        return await _redis.exists(_cancel_key(key)) == 1
    except Exception as e:
        log.warning("is_cancelled(%s) failed: %s", key, e)
        return False


async def clear_cancel(key: str) -> None:
    if _redis is None:
        return
    try:
        await _redis.delete(_cancel_key(key))
    except Exception as e:
        log.warning("clear_cancel(%s) failed: %s", key, e)