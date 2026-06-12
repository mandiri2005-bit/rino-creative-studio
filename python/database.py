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

async def create_narasi_job(tenant_id, user_id, external_id, topic, total_chapters=0) -> str:
    """Create a jobs row for a narasi run. The narasi 8-char id goes in
    external_job_id (cancel/stitch keep using it); the row's UUID is the PK."""
    try:
        jid = await _q_fetchval(
            """INSERT INTO jobs
                   (tenant_id,user_id,job_type,status,progress_message,
                    progress_current,progress_total,external_job_id,output_prefix,started_at)
               VALUES ($1,$2,'narasi'::job_type_enum,'processing','Memulai narasi...',
                       0,$3,$4,$5,now()) RETURNING id""",
            _uid(tenant_id), _uid(user_id), int(total_chapters or 0),
            external_id, (topic or "")[:200])
        return str(jid)
    except Exception as e:
        log.error("create_narasi_job: %s", e); raise

async def update_narasi_progress(tenant_id, external_id, current, total, message) -> None:
    """Update progress_current/total + message for a narasi job (by external id)."""
    try:
        await _q_exec(
            """UPDATE jobs SET progress_current=$3, progress_total=$4,
               progress_message=$5, status='processing',
               logs=logs||to_jsonb($5::text)
               WHERE external_job_id=$2 AND tenant_id=$1""",
            _uid(tenant_id), external_id, int(current), int(total), message)
    except Exception as e:
        log.error("update_narasi_progress: %s", e); raise

async def get_job_by_external(tenant_id, external_id) -> Optional[dict]:
    """Look up the most recent job by external_job_id (narasi 8-char id, etc.)."""
    try:
        row = await _q_fetchrow(
            "SELECT * FROM jobs WHERE external_job_id=$1 AND tenant_id=$2 "
            "ORDER BY created_at DESC LIMIT 1",
            external_id, _uid(tenant_id))
        return _row(row) if row else None
    except Exception as e:
        log.error("get_job_by_external: %s", e); raise

async def finish_narasi_job(tenant_id, external_id, status, result=None, error=None) -> None:
    """Terminal status for a narasi job (done/cancelled/error), by external id."""
    try:
        await _q_exec(
            """UPDATE jobs SET status=$3::job_status_enum,
               result_payload=$4, error_message=$5,
               progress_message=CASE WHEN $3='done' THEN 'Selesai' ELSE progress_message END,
               completed_at=now()
               WHERE external_job_id=$2 AND tenant_id=$1""",
            _uid(tenant_id), external_id, status, result, error)
    except Exception as e:
        log.error("finish_narasi_job: %s", e); raise

async def save_narasi_chapter(tenant_id, job_id, chapter_index, content,
                              word_count, source_prompt, retrieved_ids,
                              version=1, approved=False):
    """Upsert one chapter into narasi_chapters (durable read-back + capture).
    job_id is the jobs.id UUID (NOT the external 8-char id). Idempotent on
    (job_id, chapter_index): a retry of the same chapter overwrites in place and
    bumps version. retrieved_ids is a list of Qdrant passage_id strings → stored
    as a real jsonb array (pass the list, let the codec encode once)."""
    try:
        cid = await _q_fetchval(
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
            source_prompt or "", list(retrieved_ids or []), bool(approved),
            tenant=str(tenant_id))           # bg task has no request ctx → set tenant
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
    finish_reason="stop") -> None:
    """`model` → model_alias. Pass model_upstream for resolved name."""
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
        await _q_exec(
            """INSERT INTO usage_logs
                   (tenant_id,user_id,session_id,job_id,
                    endpoint,model_alias,model_upstream,provider,
                    tokens_in,tokens_out,cost_usd,
                    finish_reason,latency_ms,http_status)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)""",
            _uid(tenant_id), uid, _uid(session_id), _uid(job_id),
            endpoint, model, model_upstream or model, provider,
            tokens_in, tokens_out, cost_usd,
            finish_reason, latency_ms, http_status,
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
                  prompt_used,generated_narration,model,tokens_in,tokens_out,cost_usd)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) RETURNING id""",
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
                  duration_minutes,language)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING *""",
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
                      COALESCE(s.monthly_token_limit, 100) AS credits
                 FROM tenants t
            LEFT JOIN subscriptions s ON s.tenant_id = t.id
                WHERE t.id = $1
                LIMIT 1""",
            _uid(tenant_id), tenant=str(tenant_id))
        if row:
            return {"tier": row["tier"] or "free",
                    "credits": row["credits"] or 100,
                    "laozhang_key": "", "deepseek_key": "", "gemini_key": ""}
    except Exception as e:
        log.warning("get_tenant_context: %s", e)
    return {"tier": "free", "credits": 100,
            "laozhang_key": "", "deepseek_key": "", "gemini_key": ""}
