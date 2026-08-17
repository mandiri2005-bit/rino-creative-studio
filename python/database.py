"""
database.py — Async PostgreSQL data layer for Rino Creative Studio
Pure asyncpg · No ORM · Every function takes tenant_id first
"""
import json, logging, os, ssl as _ssl
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID
import asyncpg

log = logging.getLogger("database")
_pool: Optional[asyncpg.Pool] = None

# ── Helpers ──────────────────────────────────────────────────────────────────

def _uid(v) -> Optional[UUID]:
    """str | UUID | None → UUID | None."""
    return UUID(v) if isinstance(v, str) else v

def _row(r: asyncpg.Record) -> dict:
    """Record → dict, UUIDs stringified for JSON safety."""
    return {k: str(v) if isinstance(v, UUID) else v for k, v in dict(r).items()}

async def _init_conn(conn: asyncpg.Connection):
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

# ═════════════════════════════════════════════════════════════════════════════
# Pool lifecycle — call from FastAPI lifespan
# ═════════════════════════════════════════════════════════════════════════════

def _pool_url() -> str:
    env = os.getenv("NODE_ENV", "development")
    url = {"production": os.getenv("DATABASE_POOL_URL"),
           "staging":    os.getenv("DATABASE_POOL_URL_STAGING"),
           }.get(env, os.getenv("DATABASE_POOL_URL_DEV"))
    if not url:
        raise RuntimeError(f"No DATABASE_POOL_URL for NODE_ENV={env}")
    return url

async def init_db() -> None:
    """Create the asyncpg pool. Call once at startup."""
    global _pool
    _pool = await asyncpg.create_pool(
        _pool_url(), min_size=2, max_size=10,
        ssl=_ssl.create_default_context(), init=_init_conn)
    log.info("DB pool ready (min=2 max=10 branch=%s)",
             os.getenv("NODE_ENV", "development"))

async def close_db() -> None:
    """Drain pool. Call once at shutdown."""
    global _pool
    if _pool:
        await _pool.close(); _pool = None; log.info("DB pool closed")

def _db() -> asyncpg.Pool:
    if not _pool: raise RuntimeError("Call init_db() first")
    return _pool

from auth_middleware import _tenant_ctx   # add at top with the other imports
 
def _current_tenant() -> str:
    """Read the tenant_id set by auth_middleware for this request."""
    try:
        return _tenant_ctx.get().tenant_id or ""
    except Exception:
        return ""
 
# RLS-safe query helpers: open a txn, set the tenant, run the query.
# These REPLACE direct _db().fetch / .fetchrow / .fetchval / .execute calls.
async def _q_fetch(sql, *args, tenant=None):
    async with _db().acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)",
                           tenant or _current_tenant())
        return await conn.fetch(sql, *args)
 
async def _q_fetchrow(sql, *args, tenant=None):
    async with _db().acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)",
                           tenant or _current_tenant())
        return await conn.fetchrow(sql, *args)
 
async def _q_fetchval(sql, *args, tenant=None):
    async with _db().acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)",
                           tenant or _current_tenant())
        return await conn.fetchval(sql, *args)
 
async def _q_exec(sql, *args, tenant=None):
    async with _db().acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)",
                           tenant or _current_tenant())
        return await conn.execute(sql, *args)

# ═════════════════════════════════════════════════════════════════════════════
# USERS — just-in-time provisioning (webhook backfills real details later)
# ═════════════════════════════════════════════════════════════════════════════

async def upsert_user(tenant_id, external_id, email=None,
                      display_name=None, role="member"):
    """Create a minimal users row keyed by (tenant_id, external_id) so
    chat_sessions.user_id / usage_logs.user_id FKs resolve when the Clerk
    webhook hasn't fired (e.g. local dev). Returns the users.id string or None.
    The user.updated webhook later backfills the real email."""
    email = email or f"{external_id}@clerk.placeholder"
    try:
        # Re-check by external_id first to avoid email-collision churn
        row = await _q_fetchrow(
            "SELECT id FROM users WHERE tenant_id=$1 AND external_id=$2",
            _uid(tenant_id), external_id, tenant=str(tenant_id))
        if row:
            return str(row["id"])
        row = await _q_fetchrow(
            """INSERT INTO users (tenant_id, external_id, email, display_name, role)
               VALUES ($1,$2,$3,$4,$5)
               ON CONFLICT (tenant_id, email) DO UPDATE SET updated_at=now()
               RETURNING id""",
            _uid(tenant_id), external_id, email, display_name, role,
            tenant=str(tenant_id))
        return str(row["id"]) if row else None
    except Exception as e:
        log.error("upsert_user: %s", e); raise

# ═════════════════════════════════════════════════════════════════════════════
# SESSIONS — replaces `sessions: dict[str, Conversation]`
# ═════════════════════════════════════════════════════════════════════════════

async def get_or_create_session(
    tenant_id, user_id, session_id, model, system_prompt, **kw) -> dict:
    """Return existing session or create. Extra kw: temperature, max_tokens,
    use_tools, mcp_paths."""
    tid, sid = _uid(tenant_id), _uid(session_id)
    # user_id references users.id (a UUID). The middleware may hand us a Clerk
    # user string instead; only use it if it's a real UUID that exists, else NULL
    # (the column is nullable — the session still belongs to the correct tenant).
    uid = None
    if user_id:
        try:
            _cand = _uid(user_id)
            # Only reference it if such a user actually exists (FK is on users.id).
            _exists = await _q_fetchval(
                "SELECT 1 FROM users WHERE id=$1", _cand, tenant=str(tenant_id))
            uid = _cand if _exists else None
        except Exception:
            uid = None
    try:
        row = await _q_fetchrow(
            "SELECT * FROM chat_sessions WHERE id=$1 AND tenant_id=$2", sid, tid)
        if row: return _row(row)
        row = await _q_fetchrow(
            """INSERT INTO chat_sessions
                   (id,tenant_id,user_id,model,system_prompt,
                    temperature,max_tokens,use_tools,mcp_paths)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
               ON CONFLICT (id) DO UPDATE SET updated_at=now()
               RETURNING *""",
            sid, tid, uid, model, system_prompt or "",
            kw.get("temperature", 0.9), kw.get("max_tokens", 8192),
            kw.get("use_tools", False), kw.get("mcp_paths"))
        return _row(row)
    except Exception as e:
        log.error("get_or_create_session: %s", e); raise

async def get_session_history(tenant_id, session_id) -> list[dict]:
    """Messages in sequence_number order (OpenAI-style history)."""
    try:
        rows = await _q_fetch(
            """SELECT role, content, tokens_in, tokens_out, created_at
               FROM chat_messages WHERE tenant_id=$1 AND session_id=$2
               ORDER BY sequence_number""",
            _uid(tenant_id), _uid(session_id))
        return [_row(r) for r in rows]
    except Exception as e:
        log.error("get_session_history: %s", e); raise

async def append_message(
    tenant_id, session_id, role, content,
    model=None, tokens_in=0, tokens_out=0, cost_usd=0) -> None:
    """Append one turn and bump last_message_at.
    `model`/`cost_usd` are accepted for symmetry — use log_usage() for billing."""
    tid, sid = _uid(tenant_id), _uid(session_id)
    try:
        async with _db().acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT set_config('app.current_tenant_id', $1, true)", str(tid))
            seq = await conn.fetchval(
                "SELECT COALESCE(MAX(sequence_number),0)+1 "
                "FROM chat_messages WHERE session_id=$1", sid)
            await conn.execute(
                """INSERT INTO chat_messages
                       (tenant_id,session_id,role,content,
                        tokens_in,tokens_out,sequence_number,finish_reason)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                tid, sid, role, content,
                tokens_in or None, tokens_out or None, seq,
                "stop" if role == "assistant" else None)
            await conn.execute(
                "UPDATE chat_sessions SET last_message_at=now() WHERE id=$1", sid)
    except Exception as e:
        log.error("append_message: %s", e); raise

async def delete_session(tenant_id, session_id) -> None:
    """Hard-delete session + messages (CASCADE)."""
    try:
        await _q_exec(
            "DELETE FROM chat_sessions WHERE id=$1 AND tenant_id=$2",
            _uid(session_id), _uid(tenant_id))
    except Exception as e:
        log.error("delete_session: %s", e); raise

async def list_sessions(tenant_id, user_id=None, limit=50) -> list[dict]:
    """Most-recent-first session list, optionally filtered by user."""
    try:
        q = ("SELECT id,title,model,last_message_at,is_archived,created_at "
             "FROM chat_sessions WHERE tenant_id=$1")
        args: list = [_uid(tenant_id)]
        if user_id:
            q += " AND user_id=$2"; args.append(_uid(user_id))
        q += f" ORDER BY last_message_at DESC NULLS LAST LIMIT {int(limit)}"
        return [_row(r) for r in await _q_fetch(q, *args)]
    except Exception as e:
        log.error("list_sessions: %s", e); raise

# ═════════════════════════════════════════════════════════════════════════════
# JOBS — replaces _oneshot_jobs, jobs.json, tts-jobs.json, imagen-jobs.json
# ═════════════════════════════════════════════════════════════════════════════

async def create_job(tenant_id, user_id, job_type, file_name=None) -> str:
    """Insert job in 'processing' state. Returns job_id string."""
    try:
        jid = await _q_fetchval(
            """INSERT INTO jobs
                   (tenant_id,user_id,job_type,status,
                    progress_message,output_prefix,started_at)
               VALUES ($1,$2,$3::job_type_enum,'processing',
                       'Memulai analisis...',$4,now()) RETURNING id""",
            _uid(tenant_id), _uid(user_id), job_type, file_name)
        return str(jid)
    except Exception as e:
        log.error("create_job: %s", e); raise

# asset_type → modality (text|image|video|audio). document/archive/other → None.
_ASSET_MODALITY = {"image": "image", "video": "video", "audio": "audio"}

async def insert_asset(tenant_id, *, bucket, s3_key, content_type, size_bytes,
                       asset_type, user_id=None, job_id=None,
                       source_job_type=None, original_filename=None,
                       metadata=None, modality=None, source_prompt=None,
                       project_id=None) -> Optional[str]:
    """Record one object-storage file in `assets` (storage metadata + moat capture).
    Idempotent on (bucket, s3_key). Mirrors db.js insertAsset.
      asset_type     ∈ video|audio|image|document|archive|other   (required)
      source_job_type∈ batch_image|tts|imagen|veo|sora | None      (job_type_enum)
    Step 1 (moat): `modality` is auto-derived from asset_type when not given, and
    `source_prompt` is the generating prompt (falls back to metadata['prompt']) —
    both let one query span narration + image + video signal.
    `project_id` (the folder the asset lands in) is SELF-GUARDING: it only takes
    effect when it is one of THIS tenant's projects, else the asset stays
    Unassigned — a forged/foreign/stale id can never mis-file. Mirrors db.js.
    Tenant is passed explicitly (callers may run as background tasks with no ctx)."""
    try:
        md = metadata or {}
        modality = modality or _ASSET_MODALITY.get(asset_type)
        if source_prompt is None:
            source_prompt = md.get("prompt")
        # Normalize project_id → UUID | None WITHOUT raising: a malformed id must
        # degrade to Unassigned, never crash a best-effort persist (dropping the
        # whole asset row). The SQL self-guard then also nulls foreign ids.
        _pid: Optional[UUID] = None
        if project_id:
            try:
                _pid = project_id if isinstance(project_id, UUID) else UUID(str(project_id))
            except (ValueError, TypeError, AttributeError):
                _pid = None
        aid = await _q_fetchval(
            """INSERT INTO assets
                   (tenant_id,user_id,job_id,bucket,s3_key,original_filename,
                    content_type,size_bytes,asset_type,source_job_type,metadata,
                    modality,source_prompt,project_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::job_type_enum,$11,$12,$13,
                       -- self-guarding project_id: resolves to the id only when it
                       -- is one of THIS tenant's projects, else NULL (Unassigned).
                       (SELECT p.id FROM projects p
                          WHERE p.id = $14::uuid AND p.tenant_id = $1))
               ON CONFLICT (bucket,s3_key) DO UPDATE SET
                   size_bytes=EXCLUDED.size_bytes,
                   content_type=EXCLUDED.content_type,
                   modality=COALESCE(EXCLUDED.modality, assets.modality),
                   source_prompt=COALESCE(EXCLUDED.source_prompt, assets.source_prompt),
                   project_id=COALESCE(EXCLUDED.project_id, assets.project_id),
                   updated_at=now()
               RETURNING id""",
            _uid(tenant_id), _uid(user_id), _uid(job_id), bucket, s3_key,
            original_filename, content_type, int(size_bytes), asset_type,
            source_job_type, md, modality, source_prompt, _pid,
            tenant=str(tenant_id))
        return str(aid) if aid else None
    except Exception as e:
        log.error("insert_asset: %s", e); raise

async def save_media_task(tenant_id, user_id, job_type, task_id, prompt=None) -> Optional[str]:
    """Create a veo/sora job row recording the upstream provider task_id in
    result_payload, so the UNauthenticated /stream endpoint can later resolve the
    owning tenant via job_tenant_by_task(). The generating `prompt` is stored too
    (Step 1 moat) so the captured video asset can keep the prompt that made it.
    user_id is stored only if it's a real users.id UUID, else NULL (CurrentUser.user_id
    is a Clerk id, not a UUID)."""
    try:
        uid = None
        try:
            uid = _uid(user_id) if user_id else None
        except Exception:
            uid = None
        jid = await _q_fetchval(
            """INSERT INTO jobs (tenant_id,user_id,job_type,status,
                                 progress_message,result_payload,started_at)
               VALUES ($1,$2,$3::job_type_enum,'processing','Submitted',
                       jsonb_build_object('task_id',$4::text,'prompt',$5::text),now())
               RETURNING id""",
            _uid(tenant_id), uid, job_type, task_id, prompt,
            tenant=str(tenant_id))
        return str(jid) if jid else None
    except Exception as e:
        log.error("save_media_task: %s", e); raise

async def log_sync_job(tenant_id, job_type, result=None) -> Optional[str]:
    """Insert an already-'done' jobs row for a SYNCHRONOUS one-shot AI flow
    (generate_image/whisk/flow_image/flow_storyboard/script_tts) so the jobs table
    is a complete activity ledger. user_id left NULL (CurrentUser.user_id is a
    Clerk id, not a users.id UUID). Returns job id or None."""
    try:
        jid = await _q_fetchval(
            """INSERT INTO jobs (tenant_id,job_type,status,progress_message,
                                 result_payload,started_at,completed_at)
               VALUES ($1,$2::job_type_enum,'done','Selesai',$3,now(),now())
               RETURNING id""",
            _uid(tenant_id), job_type, result or {}, tenant=str(tenant_id))
        return str(jid) if jid else None
    except Exception as e:
        log.error("log_sync_job: %s", e); return None

# ═════════════════════════════════════════════════════════════════════════════
# IMAGE ASYNC JOBS — submit+poll for the Image page (see 0048_image_jobs.sql)
# ═════════════════════════════════════════════════════════════════════════════

async def create_image_job(tenant_id, *, job_id, user_id, op_id, op, feature, model) -> None:
    """Insert a 'running' image_jobs row at /image/<op>/submit time. FK-safe on
    user_id: _resolve_user_uuid can hand back a DETERMINISTIC fallback uuid for a
    user not yet in `users` (Clerk webhook lag); inserting it raises 23503 which
    would drop the WHOLE job row → the poll then 404s and the held credit leaks.
    Retry once unattributed so the job ALWAYS persists (poll keys on id+tenant,
    not user). Raises only on a non-FK error (caller releases the hold)."""
    async def _ins(uid):
        await _q_exec(
            """INSERT INTO image_jobs (id,tenant_id,user_id,op_id,op,feature,model,status)
               VALUES ($1,$2,$3,$4,$5,$6,$7,'running')""",
            _uid(job_id), _uid(tenant_id), uid, op_id, op, feature, model,
            tenant=str(tenant_id))
    uid = None
    try:
        uid = _uid(user_id) if user_id else None
    except Exception:
        uid = None
    try:
        await _ins(uid)
    except Exception as e:
        is_fk = getattr(e, "sqlstate", "") == "23503" or "user_id_fkey" in str(e)
        if uid is not None and is_fk:
            log.warning("create_image_job: user_id FK miss for %s — unattributed", job_id)
            await _ins(None)
        else:
            log.error("create_image_job: %s", e); raise

async def get_image_job(tenant_id, job_id) -> Optional[dict]:
    """Read one job row, tenant-scoped. Returns a dict incl. age_secs (now -
    updated_at) for the poll's lazy stale-reap, or None if absent / wrong tenant.
    The `AND tenant_id=$2` is explicit (NOT just the RLS predicate): the prod
    runtime role is BYPASSRLS, so RLS alone wouldn't stop a cross-tenant id probe
    (IDOR). Belt-and-suspenders with the 0048 RLS policy."""
    try:
        r = await _q_fetchrow(
            """SELECT id, status, op, feature, model, credits, result_key, result_mime,
                      error, op_id,
                      EXTRACT(EPOCH FROM (now() - updated_at))::float8 AS age_secs
                 FROM image_jobs WHERE id=$1 AND tenant_id=$2""",
            _uid(job_id), _uid(tenant_id), tenant=str(tenant_id))
        return _row(r) if r else None
    except Exception as e:
        log.error("get_image_job: %s", e); return None

async def finish_image_job(tenant_id, job_id, *, status, result_key=None,
                           result_mime=None, credits=0, error=None) -> bool:
    """Terminal transition running→success|failed. Flips ONLY a row still
    'running', so the background task and a lazy/sweep reap can both call it and
    exactly one wins (idempotent). Returns True iff THIS call did the transition.
    `AND tenant_id=$7` is explicit (prod role is BYPASSRLS — RLS alone wouldn't
    scope it); a wrong-tenant call can never flip another tenant's row."""
    try:
        won = await _q_fetchval(
            """UPDATE image_jobs
                  SET status=$2, result_key=COALESCE($3,result_key),
                      result_mime=COALESCE($4,result_mime), credits=$5, error=$6
                WHERE id=$1 AND tenant_id=$7 AND status='running'
                RETURNING 1""",
            _uid(job_id), status, result_key, result_mime,
            int(credits or 0), error, _uid(tenant_id), tenant=str(tenant_id))
        return won is not None
    except Exception as e:
        log.error("finish_image_job: %s", e); return False

async def sweep_stale_image_jobs(older_than_secs: int) -> list:
    """Cross-tenant orphan sweep: mark stale 'running' jobs failed and return
    their [{tenant_id, op_id}] so the app refunds each hold. Routes through the
    SECURITY DEFINER fn (the UPDATE spans tenants → impossible under app_user RLS
    otherwise). Each job is returned at most once (the UPDATE only matches still-
    'running' rows), so refunds never double-fire."""
    try:
        # build the interval server-side from an int: asyncpg binds `interval` via the
        # binary codec which requires a datetime.timedelta — a string like "720 seconds"
        # raises "'str' object has no attribute 'days'". make_interval(secs => $1) sidesteps it.
        rows = await _q_fetch(
            "SELECT tenant_id, op_id FROM image_jobs_sweep_stale(make_interval(secs => $1::int))",
            int(older_than_secs), tenant="")
        return [_row(r) for r in rows]
    except Exception as e:
        log.error("sweep_stale_image_jobs: %s", e); return []

# ═════════════════════════════════════════════════════════════════════════════
# IMAGE BATCH JOBS — async Google Batch API (see 0050_image_batch_jobs.sql)
# ═════════════════════════════════════════════════════════════════════════════

async def create_image_batch_job(tenant_id, *, job_id, user_id, op_id, model,
                                 vertex_model, total, price_each, held_credits,
                                 aspect, prompts) -> None:
    """Insert a 'submitting' batch row BEFORE the Google call. FK-safe on user_id
    (mirrors create_image_job): a deterministic fallback uuid for a not-yet-provisioned
    user raises 23503, which would drop the WHOLE row → the poll 404s on a held credit.
    Retry once unattributed so the row ALWAYS persists (reconcile keys on id+tenant).
    `prompts` is a Python list → bound as jsonb by the pool's type codec (NEVER json.dumps
    it first — that double-encodes). Raises only on a non-FK error (caller refunds)."""
    async def _ins(uid):
        await _q_exec(
            """INSERT INTO image_batch_jobs
                 (id, tenant_id, user_id, op_id, model, vertex_model, status,
                  total, price_each, held_credits, aspect, prompts)
               VALUES ($1,$2,$3,$4,$5,$6,'submitting',$7,$8,$9,$10,$11)""",
            _uid(job_id), _uid(tenant_id), uid, op_id, model, vertex_model,
            int(total), int(price_each), int(held_credits), aspect, list(prompts or []),
            tenant=str(tenant_id))
    try:
        uid = _uid(user_id) if user_id else None
    except Exception:
        uid = None
    try:
        await _ins(uid)
    except Exception as e:
        is_fk = getattr(e, "sqlstate", "") == "23503" or "user_id_fkey" in str(e)
        if uid is not None and is_fk:
            log.warning("create_image_batch_job: user_id FK miss for %s — unattributed", job_id)
            await _ins(None)
        else:
            log.error("create_image_batch_job: %s", e); raise

async def set_batch_submitted(tenant_id, job_id, *, gemini_job_name, auth_mode) -> bool:
    """Record the Google batch resource name + winning auth path and flip
    'submitting'→'processing'. Win-gated on status='submitting' so a duplicate
    submit can't re-arm a row. Returns True iff THIS call transitioned it."""
    try:
        won = await _q_fetchval(
            """UPDATE image_batch_jobs
                  SET gemini_job_name=$3, auth_mode=$4, status='processing'
                WHERE id=$1 AND tenant_id=$2 AND status='submitting'
                RETURNING 1""",
            _uid(job_id), _uid(tenant_id), gemini_job_name, auth_mode,
            tenant=str(tenant_id))
        return won is not None
    except Exception as e:
        log.error("set_batch_submitted: %s", e); return False

async def get_image_batch_job(tenant_id, job_id) -> Optional[dict]:
    """One batch row, tenant-scoped (explicit AND tenant_id — prod role is BYPASSRLS,
    so RLS alone wouldn't stop a cross-tenant id probe). Adds age_secs (now - created_at,
    the IMMUTABLE hard-expire anchor) and since_update_secs (now - updated_at)."""
    try:
        r = await _q_fetchrow(
            """SELECT id, tenant_id, user_id, op_id, gemini_job_name, auth_mode,
                      model, vertex_model, status, total, delivered, failed,
                      price_each, held_credits, aspect, prompts, result_keys, error,
                      EXTRACT(EPOCH FROM (now() - created_at))::float8 AS age_secs,
                      EXTRACT(EPOCH FROM (now() - updated_at))::float8 AS since_update_secs
                 FROM image_batch_jobs WHERE id=$1 AND tenant_id=$2""",
            _uid(job_id), _uid(tenant_id), tenant=str(tenant_id))
        return _row(r) if r else None
    except Exception as e:
        log.error("get_image_batch_job: %s", e); return None

async def touch_image_batch_job(tenant_id, job_id) -> None:
    """Bump updated_at on a still-running batch (the reconcile loop spaces work off it).
    No-op on a terminal row."""
    try:
        await _q_exec(
            """UPDATE image_batch_jobs SET updated_at=now()
                WHERE id=$1 AND tenant_id=$2 AND status IN ('submitting','processing')""",
            _uid(job_id), _uid(tenant_id), tenant=str(tenant_id))
    except Exception as e:
        log.error("touch_image_batch_job: %s", e)

async def finish_image_batch_job(tenant_id, job_id, *, status, delivered=0, failed=0,
                                 result_keys=None, error=None) -> bool:
    """Terminal transition (submitting|processing)→succeeded|partial|failed|expired.
    Flips ONLY a still-running row, so the reconcile loop and a lazy poll can both call
    it and exactly one wins → the winner alone settles credits (no double-charge). Returns
    True iff THIS call did the transition. `result_keys` (a Python list, possibly with
    null entries) binds as jsonb via the codec; None leaves the column unchanged."""
    try:
        won = await _q_fetchval(
            """UPDATE image_batch_jobs
                  SET status=$3, delivered=$4, failed=$5,
                      result_keys=COALESCE($6, result_keys),
                      error=COALESCE($7, error), completed_at=now()
                WHERE id=$1 AND tenant_id=$2 AND status IN ('submitting','processing')
                RETURNING 1""",
            _uid(job_id), _uid(tenant_id), status, int(delivered or 0), int(failed or 0),
            (list(result_keys) if result_keys is not None else None), error,
            tenant=str(tenant_id))
        return won is not None
    except Exception as e:
        log.error("finish_image_batch_job: %s", e); return False

async def list_batch_jobs(tenant_id, limit: int = 20) -> list:
    """Recent batch jobs for the tenant (newest first) for the status rail."""
    try:
        rows = await _q_fetch(
            """SELECT id, model, status, total, delivered, failed, price_each,
                      aspect, error,
                      EXTRACT(EPOCH FROM (now() - created_at))::float8 AS age_secs
                 FROM image_batch_jobs WHERE tenant_id=$1
                ORDER BY created_at DESC LIMIT $2""",
            _uid(tenant_id), int(limit), tenant=str(tenant_id))
        return [_row(r) for r in rows]
    except Exception as e:
        log.error("list_batch_jobs: %s", e); return []

async def get_due_batch_jobs(max_age_secs: int) -> list:
    """Cross-tenant: ids of batches still running and untouched for max_age_secs, so the
    reconcile loop can poll them even if the owner never opens the page. Routes through the
    0050 SECURITY DEFINER fn (cross-tenant SELECT is impossible under app_user RLS). The
    loop re-reads each row tenant-scoped and does all writes under the owning tenant."""
    try:
        rows = await _q_fetch(
            "SELECT id, tenant_id FROM image_batch_jobs_due(make_interval(secs => $1::int))",
            int(max_age_secs), tenant="")
        return [_row(r) for r in rows]
    except Exception as e:
        log.error("get_due_batch_jobs: %s", e); return []

async def job_tenant_by_task(task_id) -> Optional[str]:
    """Resolve the owning tenant of a veo/sora job by upstream task_id. Uses the
    SECURITY DEFINER fn job_tenant_by_task() (migration 0018), so it is safe to
    call from the /stream endpoint which has no tenant context. Returns None if
    unknown (job never recorded, e.g. submit was unauthenticated)."""
    try:
        tid = await _q_fetchval("SELECT job_tenant_by_task($1::text)", str(task_id))
        return str(tid) if tid else None
    except Exception as e:
        log.error("job_tenant_by_task: %s", e); return None

async def asset_key_by_task(tenant_id, task_id) -> Optional[str]:
    """R2 s3_key of the video captured for this upstream task_id (or None). Lets
    the /stream endpoint serve from R2 when the local disk cache is gone after a
    redeploy — the whole point of Step 2."""
    try:
        return await _q_fetchval(
            """SELECT s3_key FROM assets
                WHERE tenant_id=$1 AND asset_type='video'
                  AND metadata->>'task_id'=$2 AND is_deleted=false
                ORDER BY created_at DESC LIMIT 1""",
            _uid(tenant_id), str(task_id), tenant=str(tenant_id))
    except Exception as e:
        log.error("asset_key_by_task: %s", e); return None

async def media_job_id_by_task(tenant_id, task_id) -> Optional[str]:
    """jobs.id of the veo/sora job for this upstream task_id (or None), so the
    captured asset row can be linked to its job."""
    try:
        jid = await _q_fetchval(
            """SELECT id FROM jobs
                WHERE result_payload->>'task_id'=$1 AND job_type IN ('veo','sora')
                ORDER BY created_at DESC LIMIT 1""",
            str(task_id), tenant=str(tenant_id))
        return str(jid) if jid else None
    except Exception as e:
        log.error("media_job_id_by_task: %s", e); return None

async def media_prompt_by_task(tenant_id, task_id) -> Optional[str]:
    """The generating prompt recorded for a veo/sora job (or None), so the
    captured video asset can keep the prompt that produced it (Step 1 moat)."""
    try:
        return await _q_fetchval(
            """SELECT result_payload->>'prompt' FROM jobs
                WHERE result_payload->>'task_id'=$1 AND job_type IN ('veo','sora')
                ORDER BY created_at DESC LIMIT 1""",
            str(task_id), tenant=str(tenant_id))
    except Exception as e:
        log.error("media_prompt_by_task: %s", e); return None

async def complete_media_task(tenant_id, task_id) -> None:
    """Mark a veo/sora job 'done' once its video has been saved (from /stream).
    Without this the submit-time job would sit in 'processing' forever."""
    try:
        await _q_exec(
            """UPDATE jobs SET status='done', progress_message='Selesai',
                   completed_at=now(), updated_at=now()
               WHERE result_payload->>'task_id'=$1 AND job_type IN ('veo','sora')
                 AND status<>'done'""",
            str(task_id), tenant=str(tenant_id))
    except Exception as e:
        log.error("complete_media_task: %s", e)

async def update_job_progress(job_id, progress) -> None:
    """Set progress_message and append to logs JSONB array."""
    try:
        await _q_exec(
            """UPDATE jobs SET progress_message=$2, status='processing',
               logs=logs||to_jsonb($2::text) WHERE id=$1""",
            _uid(job_id), progress)
    except Exception as e:
        log.error("update_job_progress: %s", e); raise

async def complete_job(job_id, result: dict) -> None:
    """Mark done with result_payload."""
    try:
        await _q_exec(
            """UPDATE jobs SET status='done', result_payload=$2,
               progress_message='Selesai', completed_at=now() WHERE id=$1""",
            _uid(job_id), result)
    except Exception as e:
        log.error("complete_job: %s", e); raise

async def fail_job(job_id, error: str) -> None:
    try:
        await _q_exec(
            "UPDATE jobs SET status='error', error_message=$2 WHERE id=$1",
            _uid(job_id), error)
    except Exception as e:
        log.error("fail_job: %s", e); raise

async def get_job(tenant_id, job_id) -> Optional[dict]:
    try:
        row = await _q_fetchrow(
            "SELECT * FROM jobs WHERE id=$1 AND tenant_id=$2",
            _uid(job_id), _uid(tenant_id))
        return _row(row) if row else None
    except Exception as e:
        log.error("get_job: %s", e); raise

async def create_narasi_job(tenant_id, user_id, external_id, topic, total_chapters=0, op_id=None) -> str:
    """Create a jobs row for a narasi run. The narasi 8-char id goes in
    external_job_id (cancel/stitch keep using it); the row's UUID is the PK.

    Dalang v2 Slice 2 (crash-safe billing): when op_id is given, stash the billing
    checkpoint {op_id, actual:0} in the (otherwise-unused) input_payload._meter, so an
    orphan sweep (narasi_jobs_sweep_stale / 0054) can settle the hold after a crash.
    When None (crash-safety off / BYOK / no hold) input_payload stays NULL —
    byte-identical to pre-Slice-2. Pass the dict raw: the pool's jsonb codec encodes it."""
    try:
        _meter = {"_meter": {"op_id": op_id, "actual": 0}} if op_id else None
        jid = await _q_fetchval(
            """INSERT INTO jobs
                   (tenant_id,user_id,job_type,status,progress_message,
                    progress_current,progress_total,external_job_id,output_prefix,started_at,input_payload)
               VALUES ($1,$2,'narasi'::job_type_enum,'processing','Starting narration…',
                       0,$3,$4,$5,now(),$6) RETURNING id""",
            _uid(tenant_id), _uid(user_id), int(total_chapters or 0),
            external_id, (topic or "")[:200], _meter,
            tenant=str(tenant_id or ""))
        return str(jid)
    except Exception as e:
        log.error("create_narasi_job: %s", e); raise


async def checkpoint_narasi_meter(tenant_id, job_id, meter_actual) -> None:
    """Dalang v2: durably bump this run's input_payload._meter.actual, scoped to jobs.id
    (per-run, never the shared external id). Used for POST-LOOP cost (the whole-book critic)
    that the per-chapter save_narasi_chapter checkpoint can't capture. No-op when the row
    lacks _meter (crash-safety off). Best-effort / non-fatal."""
    try:
        await _q_exec(
            """UPDATE jobs SET input_payload = jsonb_set(input_payload, '{_meter,actual}',
                                                        to_jsonb($2::int), false)
                WHERE id=$1 AND tenant_id=$3 AND input_payload ? '_meter'""",
            _uid(job_id), int(meter_actual), _uid(tenant_id), tenant=str(tenant_id))
    except Exception as e:
        log.warning("checkpoint_narasi_meter (non-fatal): %s", e)


async def get_narasi_job_status(tenant_id, job_id):
    """Read a narasi run's current status by its jobs.id (tenant-scoped). Used by the crash-safe
    loop to self-abort (F1) if a premature orphan sweep reaped it to 'error' — after which the
    sweep has already committed the partial checkpoint on our op_id, so continuing would deliver
    later chapters for free. Best-effort: returns None on any error / missing row."""
    try:
        return await _q_fetchval("SELECT status::text FROM jobs WHERE id=$1 AND tenant_id=$2",
                                 _uid(job_id), _uid(tenant_id), tenant=str(tenant_id))
    except Exception as e:
        log.warning("get_narasi_job_status (non-fatal): %s", e); return None


async def sweep_stale_narasi_jobs(older_than_secs: int) -> list:
    """Cross-tenant orphan sweep (Dalang v2 Slice 2): mark stale 'processing'/'running'
    narasi jobs 'error' and return [{tenant_id, op_id, meter_actual, user_id}] so the app
    settles each orphaned hold — commit the delivered checkpoint (attributed to user_id),
    else refund. Routes through the SECURITY DEFINER fn (the UPDATE spans tenants →
    impossible under app_user RLS). Only jobs carrying a checkpoint are swept; each is
    returned at most once (only still-stale rows match), so settlements never double-fire."""
    try:
        rows = await _q_fetch(
            "SELECT tenant_id, op_id, meter_actual, user_id "
            "FROM narasi_jobs_sweep_stale(make_interval(secs => $1::int))",
            int(older_than_secs), tenant="")
        return [_row(r) for r in rows]
    except Exception as e:
        log.error("sweep_stale_narasi_jobs: %s", e); return []

async def update_narasi_progress(tenant_id, external_id, current, total, message) -> None:
    """Update progress_current/total + message for a narasi job (by external id).
    tenant= explicit — see get_job_by_external (worker-process RLS context)."""
    try:
        await _q_exec(
            """UPDATE jobs SET progress_current=$3, progress_total=$4,
               progress_message=$5, status='processing',
               logs=logs||to_jsonb($5::text)
               WHERE external_job_id=$2 AND tenant_id=$1""",
            _uid(tenant_id), external_id, int(current), int(total), message,
            tenant=str(tenant_id or ""))
    except Exception as e:
        log.error("update_narasi_progress: %s", e); raise

async def get_job_by_external(tenant_id, external_id) -> Optional[dict]:
    """Look up the most recent job by external_job_id (narasi 8-char id, etc.).
    tenant= is passed EXPLICITLY: this runs in the narration-worker too, where the
    auth-middleware contextvar (_current_tenant) is empty and RLS set_config('')
    would blow up the ''::uuid cast."""
    try:
        row = await _q_fetchrow(
            "SELECT * FROM jobs WHERE external_job_id=$1 AND tenant_id=$2 "
            "ORDER BY created_at DESC LIMIT 1",
            external_id, _uid(tenant_id), tenant=str(tenant_id or ""))
        return _row(row) if row else None
    except Exception as e:
        log.error("get_job_by_external: %s", e); raise

async def finish_narasi_job(tenant_id, external_id, status, result=None, error=None) -> None:
    """Terminal status for a narasi job (done/cancelled/error), by external id.
    tenant= explicit — see get_job_by_external (worker-process RLS context).

    F7 — CHAPTER-BALANCE TELEMETRY, ATTACHED HERE AND NOWHERE ELSE.
    `result["markdown"]` at this point is the manuscript actually being persisted and
    delivered. The pre-F6 counter report is NOT a substitute: `_apply_v3_gates()` measures
    before `_f6_finalize()`, and F6 repair may rewrite chapters afterwards, so a balance
    measured there can describe a book that was never sent. Measuring at the last statement
    before the write is what makes the number true of the delivered bytes.

    It is telemetry only — one additive JSONB field, no schema change, no threshold, no
    verdict, and no effect on what is delivered. A measurement that fails is logged as a fixed
    code and dropped, because telemetry may never cost a delivery — the raw exception and the
    manuscript are deliberately kept out of the log line.

    🔴 THE GUARD IS A JOB DISCRIMINATOR, NOT JUST A STATUS CHECK. `finish_narasi_job` is shared:
    the async OUTLINE path finishes `done` with a dict that has no manuscript in it. Keying
    only on `done` therefore stamped `chapter_balance: UNMEASURED/no_markdown` onto payloads
    that have nothing to do with chapters. Requiring a non-empty `markdown` is what makes
    "this job delivered a book" the actual condition, and leaves every other payload
    byte-identical. Failed and cancelled jobs never reach it for the same reason."""
    _f7_markdown = (result or {}).get("markdown") if (
        status == "done" and isinstance(result, dict)) else None
    if isinstance(_f7_markdown, str) and _f7_markdown.strip():
        try:
            import narasi_f7 as _f7
            result = _f7.attach_chapter_balance(result)
        except Exception:
            log.error("finish_narasi_job: f7_chapter_balance_unavailable")
    try:
        await _q_exec(
            """UPDATE jobs SET status=$3::job_status_enum,
               result_payload=$4, error_message=$5,
               progress_message=CASE WHEN $3='done' THEN 'Selesai' ELSE progress_message END,
               completed_at=now()
               WHERE external_job_id=$2 AND tenant_id=$1""",
            _uid(tenant_id), external_id, status, result, error,
            tenant=str(tenant_id or ""))
    except Exception as e:
        log.error("finish_narasi_job: %s", e); raise

async def save_narasi_chapter(tenant_id, job_id, chapter_index, content,
                              word_count, source_prompt, retrieved_ids,
                              version=1, approved=False, *, meter_checkpoint=None):
    """Upsert one chapter into narasi_chapters (durable read-back + capture).
    job_id is the jobs.id UUID (NOT the external 8-char id). Idempotent on
    (job_id, chapter_index): a retry of the same chapter overwrites in place and
    bumps version. retrieved_ids is a list of Qdrant passage_id strings → stored
    as a real jsonb array (pass the list, let the codec encode once).

    Dalang v2 Slice 2 (crash-safe billing): when meter_checkpoint is given, ATOMICALLY
    bump this run's jobs.input_payload._meter.actual to it in the SAME transaction as the
    chapter upsert — so a crash can't land between the chapter persist and the cost
    checkpoint (H4), and the checkpoint is scoped to THIS run's jobs.id (never the shared
    external_job_id → a client reusing pre_job_id can't cross-corrupt another run's
    checkpoint). No-op checkpoint when the row lacks _meter (crash-safety off)."""
    try:
        async with _db().acquire() as conn, conn.transaction():
            await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)",
                               str(tenant_id))   # bg task has no request ctx → set tenant
            cid = await conn.fetchval(
                """INSERT INTO narasi_chapters
                       (tenant_id, job_id, chapter_index, content, word_count,
                        version, source_prompt, retrieved_ids, approved)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                   ON CONFLICT (job_id, chapter_index) DO UPDATE SET
                       content       = EXCLUDED.content,
                       word_count    = EXCLUDED.word_count,
                       version       = narasi_chapters.version + 1,
                       source_prompt = EXCLUDED.source_prompt,
                       retrieved_ids = EXCLUDED.retrieved_ids,
                       approved      = EXCLUDED.approved,
                       updated_at    = now()
                   RETURNING id""",
                _uid(tenant_id), _uid(job_id), int(chapter_index),
                content or "", int(word_count or 0), int(version or 1),
                source_prompt or "", list(retrieved_ids or []), bool(approved))
            if meter_checkpoint is not None:
                # Same txn, scoped to this run's jobs.id (UUID) — atomic + per-run.
                await conn.execute(
                    """UPDATE jobs
                          SET input_payload = jsonb_set(input_payload, '{_meter,actual}',
                                                        to_jsonb($2::int), false)
                        WHERE id=$1 AND tenant_id=$3 AND input_payload ? '_meter'""",
                    _uid(job_id), int(meter_checkpoint), _uid(tenant_id))
            return str(cid)
    except Exception as e:
        log.error("save_narasi_chapter: %s", e); raise

async def get_narasi_chapters(tenant_id, job_id) -> list:
    """Read all chapters for a job (ordered by chapter_index) from narasi_chapters.
    job_id is the internal jobs.id UUID. Durable read-back path (Step 1.3): open an
    old job → read from DB, never regenerate. Returns [] if none."""
    try:
        rows = await _q_fetch(
            "SELECT chapter_index, content, word_count, version "
            "FROM narasi_chapters WHERE job_id=$1 ORDER BY chapter_index ASC",
            _uid(job_id), tenant=str(tenant_id))
        return [_row(r) for r in rows]
    except Exception as e:
        log.error("get_narasi_chapters: %s", e); raise

async def list_narasi_jobs(tenant_id, limit=15) -> list:
    """Recent narasi jobs for a tenant that are reopenable from DB (Step 1.3 UI):
    those with persisted chapters OR a stored stitched markdown. Newest first.
    topic is stored in jobs.output_prefix by create_narasi_job."""
    try:
        rows = await _q_fetch(
            """SELECT j.external_job_id, j.output_prefix AS topic, j.status,
                      j.progress_total AS chapters, j.created_at
                 FROM jobs j
                WHERE j.job_type='narasi' AND j.tenant_id=$1
                  AND (EXISTS (SELECT 1 FROM narasi_chapters c WHERE c.job_id=j.id)
                       OR (j.result_payload->>'markdown') IS NOT NULL)
                ORDER BY j.created_at DESC
                LIMIT $2""",
            _uid(tenant_id), int(limit), tenant=str(tenant_id))
        return [_row(r) for r in rows]
    except Exception as e:
        log.error("list_narasi_jobs: %s", e); raise

# ═════════════════════════════════════════════════════════════════════════════
# SERIES STATE — Dalang v2 Slice 3 (fact ledger + rolling summary; see 0055).
# For Dalang a series.id == the narasi jobs.id (a book). All best-effort: the v2
# loop degrades gracefully (no continuity) if any of these fail.
# ═════════════════════════════════════════════════════════════════════════════

async def create_series(tenant_id, series_id, kind, spec, user_id=None) -> None:
    """Create the series row (idempotent on id). kind ∈ book|channel; spec is a
    free-form dict (topic/style/language/…). Pass spec raw — the jsonb codec encodes."""
    try:
        await _q_exec(
            """INSERT INTO series (id, tenant_id, user_id, kind, spec)
               VALUES ($1,$2,$3,$4,$5) ON CONFLICT (id) DO NOTHING""",
            _uid(series_id), _uid(tenant_id), _uid(user_id) if user_id else None,
            str(kind), spec or {}, tenant=str(tenant_id))
    except Exception as e:
        log.warning("create_series (non-fatal): %s", e)


async def upsert_series_facts(tenant_id, series_id, facts, episode=None) -> int:
    """Upsert canonical entity facts (§3.1). facts = [{entity_key, entity_type, value}].
    Merges value jsonb on conflict (enrich, latest wins per key); keeps the original
    introduced_episode. Returns the count written. Best-effort."""
    n = 0
    for f in (facts or []):
        key = (f.get("entity_key") or f.get("name") or "").strip()
        if not key:
            continue
        etype = (f.get("entity_type") or f.get("type") or "other").strip()
        val = f.get("value")
        if not isinstance(val, dict):
            val = {"note": val} if val is not None else {}
        try:
            await _q_exec(
                """INSERT INTO series_facts
                       (series_id, tenant_id, entity_key, entity_type, value, introduced_episode)
                   VALUES ($1,$2,$3,$4,$5,$6)
                   ON CONFLICT (series_id, entity_key) DO UPDATE SET
                       entity_type        = EXCLUDED.entity_type,
                       value              = series_facts.value || EXCLUDED.value,
                       introduced_episode = COALESCE(series_facts.introduced_episode,
                                                     EXCLUDED.introduced_episode),
                       updated_at         = now()""",
                _uid(series_id), _uid(tenant_id), key[:200], etype[:60], val,
                (int(episode) if episode is not None else None),
                tenant=str(tenant_id))
            n += 1
        except Exception as e:
            log.warning("upsert_series_facts (non-fatal): %s", e)
    return n


async def get_series_facts(tenant_id, series_id, limit=60) -> list:
    """Read the fact ledger for a series (most-recently-updated first). Returns
    [{entity_key, entity_type, value, introduced_episode}]."""
    try:
        rows = await _q_fetch(
            """SELECT entity_key, entity_type, value, introduced_episode
                 FROM series_facts WHERE series_id=$1 AND tenant_id=$2
                ORDER BY updated_at DESC LIMIT $3""",
            _uid(series_id), _uid(tenant_id), int(limit), tenant=str(tenant_id))
        return [_row(r) for r in rows]
    except Exception as e:
        log.warning("get_series_facts (non-fatal): %s", e); return []


async def upsert_series_summary(tenant_id, series_id, summary, episode=None) -> None:
    """Write/replace the rolling 'story so far' summary (§3.2), 1 row per series."""
    try:
        await _q_exec(
            """INSERT INTO series_summary (series_id, tenant_id, rolling_summary, updated_episode)
               VALUES ($1,$2,$3,$4)
               ON CONFLICT (series_id) DO UPDATE SET
                   rolling_summary = EXCLUDED.rolling_summary,
                   updated_episode = EXCLUDED.updated_episode,
                   updated_at      = now()""",
            _uid(series_id), _uid(tenant_id), (summary or "")[:4000],
            (int(episode) if episode is not None else None),
            tenant=str(tenant_id))
    except Exception as e:
        log.warning("upsert_series_summary (non-fatal): %s", e)


async def get_series_summary(tenant_id, series_id) -> str:
    """Read the rolling summary for a series (or '' if none). Best-effort."""
    try:
        val = await _q_fetchval(
            "SELECT rolling_summary FROM series_summary WHERE series_id=$1 AND tenant_id=$2",
            _uid(series_id), _uid(tenant_id), tenant=str(tenant_id))
        return val or ""
    except Exception as e:
        log.warning("get_series_summary (non-fatal): %s", e); return ""

async def get_known_bad_claims(project_id=None) -> list:
    """CC v3 R-FG4 + refactor §6: GLOBAL rows plus (when project_id given) that project's
    rows. Falls back to the pre-0058 shape if the scope columns don't exist yet. [] on any
    error (the gate carries an in-process seed)."""
    try:
        try:
            rows = await _q_fetch(
                """SELECT name, bad_pattern, correct_value, action, source
                     FROM narasi_known_bad_claims
                    WHERE enabled AND (COALESCE(scope,'global')='global'
                          OR (scope='project' AND project_id = $1))
                    ORDER BY created_at""",
                (_uid(project_id) if project_id else None))
        except Exception:
            rows = await _q_fetch(
                """SELECT name, bad_pattern, correct_value, action, source
                     FROM narasi_known_bad_claims WHERE enabled ORDER BY created_at""")
        return [_row(r) for r in rows]
    except Exception as e:
        log.warning("get_known_bad_claims (non-fatal): %s", e); return []


async def add_known_bad_claim(name, bad_pattern, correct_value, *, action="replace",
                              source="", scope="global", project_id=None) -> None:
    """R-FG4: a human review flag becomes a permanent rule. Idempotent on bad_pattern.
    scope='project' (+project_id) confines it to one project (refactor §6)."""
    try:
        await _q_exec(
            """INSERT INTO narasi_known_bad_claims
               (name, bad_pattern, correct_value, action, source, scope, project_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (bad_pattern) DO NOTHING""",
            (name or "claim")[:120], bad_pattern, correct_value or "",
            ("flag" if str(action).lower() == "flag" else "replace"), (source or "")[:300],
            ("project" if str(scope).lower() == "project" and project_id else "global"),
            (_uid(project_id) if project_id else None))
    except Exception as e:
        log.warning("add_known_bad_claim (non-fatal): %s", e)


async def get_known_good_claims(project_id=None) -> list:
    """R-FG8 §4 cache: global + this project's verified-claim patterns (sweep-exempt)."""
    try:
        rows = await _q_fetch(
            """SELECT claim_pattern, verified_value, epistemic_class, source
                 FROM narasi_known_good_claims
                WHERE enabled AND (scope='global' OR (scope='project' AND project_id=$1))
                ORDER BY created_at""",
            (_uid(project_id) if project_id else None))
        return [_row(r) for r in rows]
    except Exception as e:
        log.warning("get_known_good_claims (non-fatal): %s", e); return []


async def add_known_good_claim(claim_pattern, verified_value, *, epistemic_class="world",
                               source="", scope="global", project_id=None) -> None:
    """R-FG8 §4: a verification result becomes a cached, sweep-exempt fact."""
    try:
        await _q_exec(
            """INSERT INTO narasi_known_good_claims
               (claim_pattern, verified_value, epistemic_class, source, scope, project_id)
               VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (claim_pattern) DO NOTHING""",
            claim_pattern, (verified_value or "")[:300],
            (epistemic_class if epistemic_class in ("world", "source", "scholarly", "attribution") else "world"),
            (source or "")[:300],
            ("project" if str(scope).lower() == "project" and project_id else "global"),
            (_uid(project_id) if project_id else None))
    except Exception as e:
        log.warning("add_known_good_claim (non-fatal): %s", e)


async def count_active_narasi_jobs(tenant_id) -> int:
    """BullMQ S3 fairness: how many narasi jobs this tenant currently has in flight."""
    try:
        n = await _q_fetchval(
            """SELECT count(*) FROM jobs
                WHERE tenant_id=$1 AND job_type='narasi'
                  AND status::text IN ('processing','running','polishing')""",
            _uid(tenant_id), tenant=str(tenant_id))
        return int(n or 0)
    except Exception as e:
        log.warning("count_active_narasi_jobs (non-fatal): %s", e); return 0


async def get_narasi_chapter_contents(tenant_id, job_uuid) -> list:
    """BullMQ S2 resume: the checkpointed chapters of a job — [{chapter_index, content}].
    job_uuid = jobs.id UUID. [] on any error (resume then just rewrites everything)."""
    try:
        rows = await _q_fetch(
            """SELECT chapter_index, content FROM narasi_chapters
                WHERE job_id=$1 AND tenant_id=$2 ORDER BY chapter_index""",
            _uid(job_uuid), _uid(tenant_id), tenant=str(tenant_id))
        return [_row(r) for r in rows]
    except Exception as e:
        log.warning("get_narasi_chapter_contents (non-fatal): %s", e); return []


async def get_chapters_for_rating(tenant_id, job_id) -> list:
    """Chapters of a job with their latest 1-5 rating, for the per-chapter rating UI
    (Step 1.4). Returns [{id, chapter_index, rating}] ordered by chapter_index."""
    try:
        rows = await _q_fetch(
            """SELECT nc.id, nc.chapter_index,
                      (SELECT a.rating FROM approvals a
                        WHERE a.chapter_id = nc.id
                        ORDER BY a.created_at DESC LIMIT 1) AS rating
                 FROM narasi_chapters nc
                WHERE nc.job_id = $1
                ORDER BY nc.chapter_index ASC""",
            _uid(job_id), tenant=str(tenant_id))
        return [_row(r) for r in rows]
    except Exception as e:
        log.error("get_chapters_for_rating: %s", e); raise

async def save_approval(tenant_id, user_id, chapter_id, rating) -> str:
    """Record a 1-5 rating for a chapter (Step 1.4 moat signal). approved=true when
    rating >= 4; also reflects that flag onto narasi_chapters.approved.

    Tenant-scoped WRITE (Dalang v2 Slice 0 / IDOR fix): the prod runtime role app_user is
    NOBYPASSRLS (migration 0016), so the tenant_isolation RLS policy DOES apply — but the
    explicit tenant_id predicate is kept as intentional defense-in-depth ALONGSIDE it (never
    remove the SQL filter). The INSERT is gated on the chapter belonging to tenant_id
    (INSERT…SELECT…WHERE nc.tenant_id) and the UPDATE carries an explicit AND tenant_id. A
    cross-tenant / unknown chapter_id writes NOTHING and this returns "" so the caller can
    surface not-found."""
    approved = bool(rating and int(rating) >= 4)
    try:
        aid = await _q_fetchval(
            """INSERT INTO approvals (tenant_id, user_id, chapter_id, approved, rating)
               SELECT $1,$2,nc.id,$4,$5 FROM narasi_chapters nc
               WHERE nc.id=$3 AND nc.tenant_id=$1
               RETURNING id""",
            _uid(tenant_id), _uid(user_id), _uid(chapter_id), approved, int(rating),
            tenant=str(tenant_id))
        if not aid:
            log.warning("save_approval: chapter %s not owned by tenant %s — no write",
                        chapter_id, tenant_id)
            return ""
        await _q_exec("UPDATE narasi_chapters SET approved=$2, updated_at=now() "
                      "WHERE id=$1 AND tenant_id=$3",
                      _uid(chapter_id), approved, _uid(tenant_id), tenant=str(tenant_id))
        return str(aid)
    except Exception as e:
        log.error("save_approval: %s", e); raise

async def save_approval_all(tenant_id, user_id, job_id, rating) -> int:
    """Rate EVERY chapter of a job with the same 1-5 value ('beri rating narasi').
    One approval row per chapter + reflect approved onto narasi_chapters. Returns
    the number of chapters rated."""
    approved = bool(rating and int(rating) >= 4)
    try:
        # Dalang v2 Slice 0 / IDOR defense-in-depth: app_user is NOBYPASSRLS (migration 0016) so
        # the tenant_isolation RLS policy applies; the explicit AND tenant_id is kept ALONGSIDE it
        # as belt-and-suspenders (the handler already resolves job_id via
        # get_job_by_external(tenant_id, …); this guards the DB layer regardless).
        await _q_exec(
            """INSERT INTO approvals (tenant_id, user_id, chapter_id, approved, rating)
               SELECT $1,$2,nc.id,$4,$5 FROM narasi_chapters nc
               WHERE nc.job_id=$3 AND nc.tenant_id=$1""",
            _uid(tenant_id), _uid(user_id), _uid(job_id), approved, int(rating),
            tenant=str(tenant_id))
        await _q_exec(
            "UPDATE narasi_chapters SET approved=$2, updated_at=now() "
            "WHERE job_id=$1 AND tenant_id=$3",
            _uid(job_id), approved, _uid(tenant_id), tenant=str(tenant_id))
        cnt = await _q_fetchval(
            "SELECT count(*) FROM narasi_chapters WHERE job_id=$1 AND tenant_id=$2",
            _uid(job_id), _uid(tenant_id), tenant=str(tenant_id))
        return int(cnt or 0)
    except Exception as e:
        log.error("save_approval_all: %s", e); raise

async def cleanup_old_jobs(tenant_id, older_than_hours=24) -> int:
    """Delete completed/failed jobs older than N hours."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
        tag = await _q_exec(
            """DELETE FROM jobs WHERE tenant_id=$1
               AND status IN ('done','error') AND created_at<$2""",
            _uid(tenant_id), cutoff)
        n = int(tag.split()[-1])
        if n: log.info("Cleaned %d old jobs (tenant=%s)", n, tenant_id)
        return n
    except Exception as e:
        log.error("cleanup_old_jobs: %s", e); raise

# ═════════════════════════════════════════════════════════════════════════════
# USAGE LOGGING
# ═════════════════════════════════════════════════════════════════════════════

async def log_usage(
    tenant_id, user_id, model, endpoint, tokens_in, tokens_out, cost_usd,
    *, job_id=None, session_id=None, provider="laozhang",
    model_upstream=None, latency_ms=None, http_status=200,
    finish_reason="stop", credits=0, is_paid=None) -> None:
    """`model` → model_alias. Pass model_upstream for resolved name.
    `credits` = credits charged by the Step 4 metering layer (0 for BYOK/free).
    `is_paid` = funding source override; None → derived from the tenant's plan
    (paid-plan consumption recognizes revenue; free-plan = acquisition cost)."""
    try:
        # user_id has an FK to users(id). The middleware may hand us a derived
        # UUID that was never inserted — null it out if it doesn't exist, else
        # the INSERT fails with a foreign key violation (same guard as
        # get_or_create_session).
        uid = None
        if user_id:
            _cand = _uid(user_id)
            _exists = await _q_fetchval(
                "SELECT 1 FROM users WHERE id=$1", _cand, tenant=str(tenant_id))
            uid = _cand if _exists else None

        # ── Financial GL tagging (auto, clean-from-day-one). Derived from values we
        # already have; best-effort so a tagging hiccup never blocks usage logging. ──
        _cost_idr = _rev_idr = _markup = _gl_rev = _gl_cogs = None
        _paid = is_paid
        try:
            import credit_catalog as _cat
            _gl_rev, _gl_cogs = _cat.gl_codes(endpoint)
            _cusd = float(cost_usd or 0)
            _cr = int(credits or 0)
            _cost_idr = round(_cusd * _cat.KURS_IDR_USD, 2)
            _markup = round(_cr * _cat.CREDIT_USD_VALUE / _cusd, 3) if _cusd > 0 else None
            # resolve plan: drives is_paid AND the per-plan revenue-recognition price
            _plan = await _q_fetchval("SELECT plan FROM tenants WHERE id=$1",
                                      _uid(tenant_id), tenant=str(tenant_id))
            if _paid is None:   # funding source: paid-plan = revenue, free-plan = acquisition cost
                _paid = bool(_plan) and _plan != "free"
            _rev_idr = round(_cr * _cat.credit_sale_price_idr(_plan), 2) if _paid else 0
            if _paid is False and _cusd > 0:        # feed the global free-tier kill-switch counter
                try:
                    import credits as _credits
                    await _credits.note_free_cost(_cusd)
                except Exception:
                    pass
        except Exception as _gle:
            log.warning("log_usage GL tag derive failed (non-fatal): %s", _gle)

        await _q_exec(
            """INSERT INTO usage_logs
                   (tenant_id,user_id,session_id,job_id,
                    endpoint,model_alias,model_upstream,provider,
                    tokens_in,tokens_out,cost_usd,credits,
                    finish_reason,latency_ms,http_status,
                    cost_idr,revenue_idr,markup_factor,is_paid,gl_revenue_code,gl_cogs_code)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,
                       $16,$17,$18,$19,$20,$21)""",
            _uid(tenant_id), uid, _uid(session_id), _uid(job_id),
            endpoint, model, model_upstream or model, provider,
            tokens_in, tokens_out, cost_usd, int(credits or 0),
            finish_reason, latency_ms, http_status,
            _cost_idr, _rev_idr, _markup, _paid, _gl_rev, _gl_cogs,
            tenant=str(tenant_id))   # explicit — request ContextVar is gone during SSE finally
    except Exception as e:
        log.error("log_usage: %s", e); raise

# ═════════════════════════════════════════════════════════════════════════════
# MIGRATION GUIDE — how to update laozhang_api.py
# ═════════════════════════════════════════════════════════════════════════════
#
# 1. ONESHOT JOBS — narasi/oneshot-fix (~line 4295):
#
#     BEFORE: _oneshot_jobs[job_id] = {"status": "processing", ...}
#     AFTER:  job_id = await db.create_job(tenant_id, user_id,
#                                          "oneshot_fix", file_name)
#
#     BEFORE: _oneshot_jobs[job_id]["progress"] = "AI membaca..."
#     AFTER:  await db.update_job_progress(job_id, "AI membaca...")
#
#     BEFORE: _oneshot_jobs[job_id].update({"status": "done", ...})
#     AFTER:  await db.complete_job(job_id, {"fixed_book": ..., ...})
#
#     BEFORE: _oneshot_jobs[job_id].update({"status": "error", ...})
#     AFTER:  await db.fail_job(job_id, str(e))
#
#     BEFORE: job = _oneshot_jobs.get(job_id)
#     AFTER:  job = await db.get_job(tenant_id, job_id)
#
#
#
# 2. HISTORY ENDPOINT (~line 997):
#
#     BEFORE: return {"history": sessions[session_id].get_history()}
#     AFTER:  return {"history": await db.get_session_history(tid, session_id)}
#
#
#
# 3. SESSION CREATION — stream_chat (~line 947):
#
#     BEFORE: sessions[req.session_id] = Conversation(model=..., system=...)
#             conv = sessions[req.session_id]
#     AFTER:  session = await db.get_or_create_session(
#                 tenant_id, user_id, req.session_id, req.model, req.system,
#                 temperature=req.temperature, max_tokens=req.max_tokens)
#             history = await db.get_session_history(tenant_id, req.session_id)
#
#
#
# 4. APPENDING MESSAGES — Conversation.stream (~line 782):
#
#     BEFORE: self.history.append({"role": "user", "content": stored_user})
#             self.history.append({"role": "assistant", "content": reply})
#     AFTER:  await db.append_message(tid, sid, "user", prompt, model, 0, 0, 0)
#             await db.append_message(tid, sid, "assistant", reply,
#                                     model, tokens_in, tokens_out, cost)
#
#
#
# 5. FASTAPI LIFESPAN — replace `app = FastAPI(...)` block (~line 251):
#
#     from contextlib import asynccontextmanager
#     import database as db
#
#     @asynccontextmanager
#     async def lifespan(application):
#         await db.init_db()
#         yield
#         await db.close_db()
#
#     app = FastAPI(title="LaoZhang Chat API", lifespan=lifespan)
#


# ═════════════════════════════════════════════════════════════════════════════
# MOAT CAPTURE — WS-G Task 5 (correction pairs)
# ═════════════════════════════════════════════════════════════════════════════

async def save_outline(tenant_id, user_id, topic, style, language,
                       chap_count, outline_text, chapters, model) -> str:
    """Store a generated narasi outline as a moat artifact (research → outline step).
    Best-effort: caller should treat failures as non-fatal."""
    try:
        sid = await _q_fetchval(
            """INSERT INTO narasi_outlines
                 (tenant_id,user_id,topic,style,language,chap_count,
                  outline_text,chapters,model)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id""",
            _uid(tenant_id), _uid(user_id), topic, style, language,
            int(chap_count or 0), outline_text, json.dumps(chapters), model,
            tenant=str(tenant_id))
        return str(sid)
    except Exception as e:
        log.error("save_outline: %s", e); raise


async def save_moat_session(tenant_id, user_id, topic, style, rag_result: dict,
                            model, tokens_in, tokens_out, cost_usd) -> str:
    """Store one generated narration + its RAG context. Returns moat_session id."""
    try:
        sid = await _q_fetchval(
            """INSERT INTO moat_sessions
                 (tenant_id,user_id,topic,style,rag_used,sources,passages,
                  prompt_used,generated_narration,model,tokens_in,tokens_out,cost_usd,
                  modality)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,'text') RETURNING id""",
            _uid(tenant_id), _uid(user_id), topic, style,
            bool(rag_result.get("rag_used")),
            json.dumps(rag_result.get("sources")),
            json.dumps(rag_result.get("passages")),
            rag_result.get("prompt_used"),
            rag_result.get("narration"),
            model, tokens_in, tokens_out, cost_usd,
            tenant=str(tenant_id))
        return str(sid)
    except Exception as e:
        log.error("save_moat_session: %s", e); raise

async def save_correction_pair(moat_session_id, tenant_id, user_id,
                               original_text, corrected_text,
                               style_label, topic, duration_minutes, language) -> dict:
    """Save a user edit as a training pair. Caller should treat failures as non-fatal."""
    import difflib
    dist = sum(1 for d in difflib.ndiff(original_text or "", corrected_text or "")
               if d[0] != " ")
    ratio = round(dist / max(len(original_text or ""), 1), 4)
    if ratio < 0.05:    tier = "low"        # trivial edit — skip training use
    elif ratio <= 0.40: tier = "high"       # meaningful improvement
    else:               tier = "rewrite"    # major rewrite
    try:
        row = await _q_fetchrow(
            """INSERT INTO correction_pairs
                 (moat_session_id,tenant_id,user_id,original_text,corrected_text,
                  edit_distance,edit_ratio,quality_tier,style_label,topic,
                  duration_minutes,language,modality)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,'text') RETURNING *""",
            _uid(moat_session_id), _uid(tenant_id), _uid(user_id),
            original_text, corrected_text, dist, ratio, tier,
            style_label, topic, duration_minutes, language,
            tenant=str(tenant_id))
        return _row(row)
    except Exception as e:
        log.error("save_correction_pair: %s", e); raise

async def get_corpus_stats(tenant_id=None) -> dict:
    """Count correction pairs by quality tier (current tenant unless tenant_id given)."""
    try:
        rows = await _q_fetch(
            "SELECT quality_tier, COUNT(*) AS n FROM correction_pairs GROUP BY quality_tier",
            tenant=str(tenant_id or _current_tenant()))
        return {r["quality_tier"]: r["n"] for r in rows}
    except Exception as e:
        log.error("get_corpus_stats: %s", e); raise

# ═════════════════════════════════════════════════════════════════════════════
# PROVISIONING + TENANT CONTEXT (used by auth_middleware)
# ═════════════════════════════════════════════════════════════════════════════

async def provision_tenant(clerk_user_id: str, email: str, plan: str = "free",
                           tenant_id=None) -> dict:
    """Idempotently create tenant+user+subscription via the SQL SECURITY DEFINER
    function provision_tenant() (migration 0015), which bypasses RLS safely.
    Returns {'id': <tenant_uuid_str>}.
    `tenant_id` may be None → the SQL function generates one."""
    try:
        name = (email.split("@")[0] if email else clerk_user_id) or clerk_user_id
        slug = (name + "-" + (clerk_user_id[-6:] if clerk_user_id else "")).lower()
        # Call the SQL function on a plain pooled connection (owner role bypasses
        # RLS for this DEFINER call; no set_config needed).
        tid = await _db().fetchval(
            "SELECT provision_tenant($1,$2,$3,$4,$5,$6,'admin')",
            _uid(tenant_id) if tenant_id else None,
            name, slug, email, plan, clerk_user_id)
        return {"id": str(tid)}
    except Exception as e:
        log.error("provision_tenant: %s", e); raise

async def get_tenant_context(tenant_id, user_id=None) -> dict:
    """Return per-request tenant state for auth_middleware: tier, credits, keys.
    Reads under the tenant's own RLS context. Falls back to sane defaults."""
    try:
        row = await _q_fetchrow(
            """SELECT t.plan AS tier,
                      COALESCE(cb.balance, s.monthly_token_limit, 100) AS credits
                 FROM tenants t
            LEFT JOIN subscriptions   s  ON s.tenant_id  = t.id
            LEFT JOIN credit_balances cb ON cb.tenant_id = t.id
                WHERE t.id = $1
                LIMIT 1""",
            _uid(tenant_id), tenant=str(tenant_id))
        if row:
            return {"tier": row["tier"] or "free",
                    "credits": int(row["credits"]) if row["credits"] is not None else 100,
                    "laozhang_key": "", "deepseek_key": "", "gemini_key": ""}
    except Exception as e:
        log.warning("get_tenant_context: %s", e)
    return {"tier": "free", "credits": 100,
            "laozhang_key": "", "deepseek_key": "", "gemini_key": ""}


# ═════════════════════════════════════════════════════════════════════════════
# PLATFORM QC METER — function-only surface (0074)
#
# These helpers deliberately use no customer tenant, no table DML and no log_usage path.
# app_user has EXECUTE on the five functions below and zero privilege on the underlying
# platform tables. The SQL functions pin platform attribution server-side.
# ═════════════════════════════════════════════════════════════════════════════

async def platform_qc_begin_attempt(
    run_id, job_uuid, job_external_id, phase, unit_index, attempt_ordinal,
    provider, model_upstream, pricing_version,
    rate_in_usd_per_m, rate_out_usd_per_m, attempt_timeout_s,
) -> dict:
    row = await _db().fetchrow(
        """SELECT attempt_id, outcome
             FROM public.platform_qc_begin_attempt(
               $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)""",
        run_id, _uid(job_uuid), job_external_id, phase,
        int(unit_index), int(attempt_ordinal),
        provider, model_upstream, pricing_version,
        rate_in_usd_per_m, rate_out_usd_per_m, attempt_timeout_s)
    return _row(row)


async def platform_qc_finish_attempt(attempt_id, attempt_state) -> dict:
    row = await _db().fetchrow(
        """SELECT lifecycle_applied, current_state
             FROM public.platform_qc_finish_attempt($1,$2)""",
        _uid(attempt_id), attempt_state)
    return _row(row)


async def platform_qc_resolve_cost(
    attempt_id, tokens_in, tokens_out, provider_reported_cost_usd,
) -> dict:
    # provider_reported_cost_usd intentionally has no default. Every caller must state NULL
    # or a Decimal value; SQL computes authoritative cost from the frozen row rates.
    row = await _db().fetchrow(
        """SELECT outcome, cost_usd
             FROM public.platform_qc_resolve_cost($1,$2,$3,$4)""",
        _uid(attempt_id), int(tokens_in), int(tokens_out),
        provider_reported_cost_usd)
    return _row(row)


async def platform_qc_arm_kill(reason_code) -> dict:
    row = await _db().fetchrow(
        """SELECT armed, was_already_armed
             FROM public.platform_qc_arm_kill($1)""",
        reason_code)
    return _row(row)


async def platform_qc_kill_is_armed() -> bool:
    return bool(await _db().fetchval(
        "SELECT public.platform_qc_kill_is_armed()"))
