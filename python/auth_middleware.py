"""
auth_middleware.py — Clerk JWT verification for Rino Creative Studio
FastAPI dependency-injection pattern (NOT app.add_middleware) so
SSE StreamingResponse endpoints are never touched.

Dependencies: pip install python-jose[cryptography] httpx
"""
import os, time, logging
from dataclasses import dataclass, field
from contextvars import ContextVar
from typing import Optional

import httpx
from fastapi import Depends, HTTPException, Header
from jose import jwt, JWTError
from jose.utils import base64url_decode
import json, base64

log = logging.getLogger("auth_middleware")

# ── ContextVars — set per-request, readable anywhere in the call stack ────────
_tenant_id: ContextVar[str] = ContextVar("_tenant_id", default="")
_user_id:   ContextVar[str] = ContextVar("_user_id",   default="")

# ── TenantContext — full per-request tenant state (WS3: Tenant Isolation) ─────

@dataclass
class TenantContext:
    tenant_id:    str   = ""
    user_id:      str   = ""
    tier:         str   = "free"    # 'free' | 'starter' | 'pro' | 'enterprise'
    credits:      int   = 100       # remaining credit balance
    api_key:      str   = ""        # upstream LaoZhang key for this tenant
    deepseek_key: str   = ""        # per-tenant DeepSeek key (may be global fallback)
    gemini_key:   str   = ""        # per-tenant Gemini key (may be global fallback)

_tenant_ctx: ContextVar[TenantContext] = ContextVar("_tenant_ctx", default=TenantContext())

def get_tenant_ctx() -> TenantContext:
    """Return the TenantContext for the current request. Safe to call anywhere."""
    return _tenant_ctx.get()

# ── Config ─────────────────────────────────────────────────────────────────────
CLERK_JWT_ISSUER    = os.getenv("CLERK_JWT_ISSUER", "")
CLERK_SECRET_KEY    = os.getenv("CLERK_SECRET_KEY", "")
JWKS_CACHE_TTL      = 3600          # seconds — re-fetch JWKS every hour
_DEV_TENANT_ID      = "00000000-0000-0000-0000-000000000001"
_DEV_USER_ID        = "00000000-0000-0000-0000-000000000002"

# ── JWKS cache ─────────────────────────────────────────────────────────────────
_jwks_cache: dict  = {}          # kid -> public key dict
_jwks_fetched_at: float = 0.0

async def _fetch_jwks(force: bool = False) -> None:
    """Fetch Clerk's JWKS and cache by kid. Refreshes every JWKS_CACHE_TTL seconds."""
    global _jwks_cache, _jwks_fetched_at
    if not force and time.time() - _jwks_fetched_at < JWKS_CACHE_TTL and _jwks_cache:
        return
    issuer = CLERK_JWT_ISSUER.rstrip("/")
    url = f"{issuer}/.well-known/jwks.json"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
        _jwks_cache = {k["kid"]: k for k in data.get("keys", [])}
        _jwks_fetched_at = time.time()
        log.info("JWKS refreshed — %d key(s) cached", len(_jwks_cache))
    except Exception as exc:
        log.error("JWKS fetch failed: %s", exc)
        # Don't wipe existing cache on transient network error
        if not _jwks_cache:
            raise HTTPException(503, "Auth service unavailable — JWKS fetch failed")

def _get_kid(token: str) -> str:
    """Extract kid from JWT header without verifying signature."""
    try:
        header_b64 = token.split(".")[0]
        # Add padding if needed
        padding = 4 - len(header_b64) % 4
        if padding != 4:
            header_b64 += "=" * padding
        header = json.loads(base64.urlsafe_b64decode(header_b64))
        return header.get("kid", "")
    except Exception:
        raise HTTPException(401, "Invalid JWT format")

# ── JWT verification ───────────────────────────────────────────────────────────

async def verify_clerk_jwt(token: str) -> dict:
    """
    Verify a Clerk JWT token and return claims dict.
    Returns: {"tenant_id": str, "user_id": str, "plan": str}
    Raises:  HTTPException(401) on any failure.
    """
    if not CLERK_JWT_ISSUER:
        # Dev mode — no Clerk configured, return placeholder
        log.warning("CLERK_JWT_ISSUER not set — skipping JWT verification (dev mode)")
        return {"tenant_id": _DEV_TENANT_ID, "user_id": _DEV_USER_ID, "plan": "free"}

    kid = _get_kid(token)
    await _fetch_jwks()

    if kid not in _jwks_cache:
        # kid not in cache — might be a newly rotated key, force refresh once
        await _fetch_jwks(force=True)

    jwk = _jwks_cache.get(kid)
    if not jwk:
        raise HTTPException(401, "Unknown JWT key ID")

    try:
        claims = jwt.decode(
            token,
            jwk,
            algorithms=["RS256"],
            issuer=CLERK_JWT_ISSUER,
            options={"verify_aud": False},   # Clerk JWTs have no aud by default
        )
    except JWTError as exc:
        raise HTTPException(401, f"JWT verification failed: {exc}")

    tenant_id = claims.get("tenant_id") or ""
    user_id   = claims.get("user_id")   or claims.get("sub") or ""
    plan      = claims.get("plan")      or "free"

    if not user_id:
        raise HTTPException(401, "JWT missing user_id / sub claim")

    return {"tenant_id": tenant_id, "user_id": user_id, "plan": plan}

# ── CurrentUser dataclass ──────────────────────────────────────────────────────

@dataclass
class CurrentUser:
    tenant_id: str
    user_id:   str
    plan:      str
    tier:      str      # alias for plan, used by billing checks

# ── DB helpers (lazy import to avoid circular imports) ─────────────────────────

async def _get_or_provision(tenant_id: str, user_id: str, email: str, plan: str) -> str:
    """
    Race-condition safety net (Path B):
    Calls database.provision_tenant() which is idempotent and creates
    tenant + user + subscription in one transaction.
    Returns the resolved tenant_id string.
    """
    import database as _db

    import uuid as _uuidmod

    # Coerce tenant_id to a valid UUID or None. An org-less Clerk JWT yields
    # tenant_id="" (empty string), which must NOT be sent to a uuid column.
    valid_tid = None
    if tenant_id:
        try:
            valid_tid = str(_uuidmod.UUID(str(tenant_id)))
        except (ValueError, AttributeError, TypeError):
            valid_tid = None

    # Fast path — tenant already exists, skip provisioning.
    # The tenants table is RLS-protected: a bare query on a pooled connection
    # would evaluate the policy `id = current_setting('app.current_tenant_id')::UUID`
    # with an empty string and raise `invalid input syntax for type uuid: ""`.
    # Run the check under the tenant's OWN id as context so the cast is valid.
    if valid_tid:
        row = await _db._q_fetchrow(
            "SELECT id FROM tenants WHERE id=$1",
            _db._uid(valid_tid), tenant=valid_tid
        )
        if row:
            return valid_tid  # already provisioned

    # Provision via shared function (same as webhook Path A)
    tenant = await _db.provision_tenant(
        clerk_user_id=user_id,
        email=email,
        plan=plan,
        tenant_id=valid_tid,   # validated UUID or None
    )
    return str(tenant["id"])

# ── FastAPI dependency ─────────────────────────────────────────────────────────

async def get_current_user(
    authorization: Optional[str] = Header(default=None),
) -> CurrentUser:
    """
    FastAPI dependency for protected routes.
    Reads Authorization: Bearer <token>, verifies it, sets ContextVars,
    and returns a CurrentUser.

    Usage on a route:
        @app.post("/chat/stream")
        async def stream_chat(req: ChatRequest, user: CurrentUser = Depends(get_current_user)):
            ...
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")

    token = authorization.removeprefix("Bearer ").strip()
    claims = await verify_clerk_jwt(token)

    tenant_id = claims["tenant_id"]
    user_id   = claims["user_id"]
    plan      = claims["plan"]

    # If JWT carries no tenant_id (user hasn't joined an org yet), fall back to
    # provisioning a personal tenant keyed by user_id
    if not tenant_id:
        # Derive a deterministic UUID from the Clerk user_id
        import uuid as _uuid
        tenant_id = str(_uuid.uuid5(_uuid.NAMESPACE_DNS, f"clerk-user-{user_id}"))

    # Provision tenant + user on first request (race-condition safety net)
    # email is not in the JWT — use user_id as placeholder until webhook sets it
    email_placeholder = f"{user_id}@clerk.placeholder"
    try:
        tenant_id = await _get_or_provision(tenant_id, user_id, email_placeholder, plan)
    except Exception as exc:
        log.error("Tenant provisioning failed: %s", exc)
        # Don't block the request — let it through with what we have

    # Set ContextVars so any helper called downstream can read them
    _tenant_id.set(tenant_id)
    _user_id.set(user_id)

    # ── WS3: build and set full TenantContext ─────────────────────────────
    try:
        import database as _db
        ctx_data = await _db.get_tenant_context(tenant_id, user_id)
        ctx = TenantContext(
            tenant_id    = tenant_id,
            user_id      = user_id,
            tier         = ctx_data.get("tier", plan),
            credits      = ctx_data.get("credits", 100),
            api_key      = ctx_data.get("laozhang_key", ""),
            deepseek_key = ctx_data.get("deepseek_key", ""),
            gemini_key   = ctx_data.get("gemini_key", ""),
        )
    except Exception as exc:
        log.warning("get_tenant_context fallback: %s", exc)
        ctx = TenantContext(
            tenant_id = tenant_id,
            user_id   = user_id,
            tier      = plan,
            credits   = 100,
            api_key   = os.getenv("LAOZHANG_API_KEY", ""),
            deepseek_key = os.getenv("DEEPSEEK_API_KEY", ""),
            gemini_key   = os.getenv("GEMINI_API_KEY", ""),
        )
    _tenant_ctx.set(ctx)
    log.info("tenant_ctx: tenant=%s tier=%s credits=%d", ctx.tenant_id, ctx.tier, ctx.credits)

    return CurrentUser(
        tenant_id=tenant_id,
        user_id=user_id,
        plan=plan,
        tier=plan,
    )

# ── Public dependency — no auth, but sets dev placeholders ────────────────────

async def public_endpoint() -> CurrentUser:
    """Use this as Depends() on routes that must stay public."""
    _tenant_id.set(_DEV_TENANT_ID)
    _user_id.set(_DEV_USER_ID)
    _tenant_ctx.set(TenantContext(
        tenant_id=_DEV_TENANT_ID, user_id=_DEV_USER_ID, tier="free", credits=100,
        api_key=os.getenv("LAOZHANG_API_KEY", ""),
    ))
    return CurrentUser(tenant_id=_DEV_TENANT_ID, user_id=_DEV_USER_ID, plan="free", tier="free")
