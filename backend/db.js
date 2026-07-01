/**
 * db.js — PostgreSQL data layer for Rino Creative Studio (Node.js / server.js)
 * Uses: drizzle-orm + pg  |  CommonJS (require/module.exports)
 *
 * Install once:  npm install drizzle-orm pg
 *
 * Env var picked by NODE_ENV:
 *   development  → DATABASE_POOL_URL_DEV
 *   staging      → DATABASE_POOL_URL_STAGING
 *   production   → DATABASE_POOL_URL
 */

import { Pool }    from "pg";
import { drizzle }  from "drizzle-orm/node-postgres";
import { sql }      from "drizzle-orm";
import { redis }    from "./redis.js";   // Step 4: mirror credit debits into the live balance cache
import fs           from "fs";
import path         from "path";
import { fileURLToPath } from "url";

// credit_usd_value is config-aware PER DEPLOYMENT (ENV still wins): the GLOBAL
// subscription deployment's pricing.json sets 0.002 (vs Indonesia $0.01) so this
// Node-side metering matches the cheaper global credit — mirrors python credit_catalog.
function _pricingCreditUsd(){
  const raw = process.env.PRICING_CONFIG_JSON;
  let cfg = {};
  if (raw) { try { cfg = JSON.parse(raw) || {}; } catch { /* ignore */ } }
  else {
    const here = path.dirname(fileURLToPath(import.meta.url));
    for (const p of [process.env.PRICING_CONFIG_PATH, path.join(here, "..", "config", "pricing.json"), "/app/config/pricing.json", path.join(process.cwd(), "config", "pricing.json")]) {
      try { if (p && fs.existsSync(p)) { cfg = JSON.parse(fs.readFileSync(p, "utf8")) || {}; break; } } catch { /* ignore */ }
    }
  }
  return cfg.credit_usd_value;
}
// Step 4 credit economics (mirror python credit_catalog; ENV > pricing.json > default)
const _CREDIT_USD    = Number(process.env.CREDIT_USD_VALUE || _pricingCreditUsd() || 0.01);
const _CREDIT_MARGIN = Number(process.env.CREDIT_MARGIN || 1.0);
const _METERING_ON   = String(process.env.METERING_ENABLED ?? "true").toLowerCase() !== "false";
function _usdToCredits(usd){ return usd > 0 ? Math.max(1, Math.ceil(usd * _CREDIT_MARGIN / _CREDIT_USD)) : 0; }

// ── Pool URL selection (mirrors database.py logic) ───────────────────────────

function _poolUrl() {
  const env = process.env.NODE_ENV || "development";
  const url = {
    production: process.env.DATABASE_POOL_URL,
    staging:    process.env.DATABASE_POOL_URL_STAGING,
  }[env] ?? process.env.DATABASE_POOL_URL_DEV;

  if (!url) throw new Error(`No DATABASE_POOL_URL for NODE_ENV=${env}`);
  return url;
}

// ── Singleton pool + drizzle instance ────────────────────────────────────────

const pool = new Pool({ connectionString: _poolUrl(), ssl: { rejectUnauthorized: false } });
const db   = drizzle(pool);

// ── Tenant ID resolution ─────────────────────────────────────────────────────
const DEV_TENANT_ID = "00000000-0000-0000-0000-000000000001";
const DEV_USER_ID   = "00000000-0000-0000-0000-000000000002";

import { createHash } from "crypto";

/**
 * Derive a deterministic UUID v5 from a Clerk user_id (mirrors Python's uuid5).
 * DNS namespace: 6ba7b810-9dad-11d1-80b4-00c04fd430c8
 */
function _uuid5(name) {
  const NS = "6ba7b810-9dad-11d1-80b4-00c04fd430c8".replace(/-/g, "");
  const nsBuf = Buffer.from(NS, "hex");
  const hash = createHash("sha1").update(nsBuf).update(name).digest();
  hash[6] = (hash[6] & 0x0f) | 0x50;  // version 5
  hash[8] = (hash[8] & 0x3f) | 0x80;  // variant
  const hex = hash.toString("hex");
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20,32)}`;
}

// Normalize an untrusted project_id/asset_id to a canonical UUID string or null.
// Postgres UUID columns raise 22P02 on a malformed literal BEFORE any WHERE-clause
// tenant filter can null it — so a garbage id would (M1) drop an asset inside a
// persist try/catch or (M2) 500 a read. Mirrors python database.py's guard: a
// non-UUID degrades to null (→ Unassigned), a well-formed-but-foreign UUID stays
// and is still safely nulled by the tenant subquery. Never raises.
const _UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
function uuidOrNull(v) {
  return (typeof v === "string" && _UUID_RE.test(v)) ? v : null;
}

/**
 * Resolve tenant_id from an authenticated Express request.
 * Uses Clerk orgId if present; falls back to a UUID derived from userId.
 */
function resolveTenantId(req) {
  const a = req.authData ?? req.auth ?? {};
  const orgId  = a.orgId  ?? null;
  const userId = a.userId ?? null;
  if (orgId)  return orgId;
  // MUST match Python: uuid.uuid5(NAMESPACE_DNS, f"clerk-user-{user_id}")
  if (userId) return _uuid5(`clerk-user-${userId}`);
  return DEV_TENANT_ID;
}

/**
 * Resolve the PostgreSQL users.id UUID from an authenticated Express request.
 * Mirrors database.py _resolve_user_uuid: looks up users by (tenant_id, external_id).
 *
 *   - no Clerk user (dev / unauthenticated) → DEV_USER_ID (exists in seed data)
 *   - Clerk user found in users table        → real users.id UUID
 *   - Clerk user NOT yet provisioned         → null (FK-safe; column is nullable)
 *
 * Returning null rather than a derived UUID avoids foreign-key violations on
 * chat_sessions.user_id / usage_logs.user_id when the user row doesn't exist yet.
 */
async function resolveUserId(req, tenantId = DEV_TENANT_ID) {
  const clerkUserId = (req.authData ?? req.auth)?.userId ?? null;
  if (!clerkUserId) return DEV_USER_ID;
  try {
    const res = await query(
      `SELECT id FROM users WHERE tenant_id = $1 AND external_id = $2`,
      [tenantId, clerkUserId],
      tenantId
    );
    if (res.rows.length) return res.rows[0].id;
  } catch (e) {
    console.error("[resolveUserId] lookup failed:", e.message);
  }
  return null;   // not provisioned → store NULL user_id
}

// ── RLS-safe query helper ────────────────────────────────────────────────────
// Opens a transaction, sets app.current_tenant_id for that transaction (required
// by Neon's pooler, which reuses connections), runs the query, commits.
// Pass tenantId as the 3rd argument so RLS policies can see the active tenant.
async function query(text, params = [], tenantId = null) {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    await client.query(
      "SELECT set_config('app.current_tenant_id', $1, true)",
      [tenantId || ""]
    );
    const res = await client.query(text, params);
    await client.query("COMMIT");
    return res;
  } catch (e) {
    await client.query("ROLLBACK");
    throw e;
  } finally {
    client.release();
  }
}

// ── RLS helper: set app.current_tenant_id for the current transaction ────────
// Usage: check out client from pool, call setTenantContext, run queries, release.
//   const client = await pool.connect();
//   try {
//     await client.query('BEGIN');
//     await setTenantContext(client, tenantId);
//     const res = await client.query('SELECT * FROM jobs WHERE tenant_id=$1', [tenantId]);
//     await client.query('COMMIT');
//     return res.rows;
//   } finally { client.release(); }
async function setTenantContext(client, tenantId) {
  // set_config (NOT `SET LOCAL x = $1` — Postgres rejects bind params in SET:
  // "syntax error at or near $1"). is_local=true → transaction-scoped, resets on
  // COMMIT/ROLLBACK so it never leaks across pooled (Neon/PgBouncer) connections.
  // Must run on the SAME client + same txn as the queries it guards.
  await client.query("SELECT set_config('app.current_tenant_id', $1, true)", [tenantId || ""]);
}

// ═════════════════════════════════════════════════════════════════════════════
// CONFIG  (stored in tenants.settings JSONB under key "config")
// Replaces: readJson(CONFIG_FILE, null) / writeJson(CONFIG_FILE, data)
// ═════════════════════════════════════════════════════════════════════════════

/**
 * getConfig(tenantId?) → object | null
 * Reads tenants.settings->>'config' for the given tenant.
 */
async function getConfig(tenantId = DEV_TENANT_ID) {
  const res = await query(
    `SELECT settings->'config' AS config FROM tenants WHERE id = $1`,
    [tenantId],
    tenantId
  );
  if (!res.rows.length) return null;
  return res.rows[0].config ?? null;
}

/**
 * setConfig(tenantId?, data) → void
 * Upserts data into tenants.settings->'config'.
 */
async function setConfig(tenantId = DEV_TENANT_ID, data) {
  await query(
    `UPDATE tenants
        SET settings   = jsonb_set(settings, '{config}', $2::jsonb, true),
            updated_at = now()
      WHERE id = $1`,
    [tenantId, JSON.stringify(data)],
    tenantId
  );
}

// ═════════════════════════════════════════════════════════════════════════════
// TTS PROFILES  (stored in tenants.settings JSONB under key "tts_profiles")
// Replaces: readJson(TTS_PROFILES_FILE, []) / writeJson(TTS_PROFILES_FILE, data)
// ═════════════════════════════════════════════════════════════════════════════

/**
 * getTtsProfiles(tenantId?) → array
 */
async function getTtsProfiles(tenantId = DEV_TENANT_ID) {
  const res = await query(
    `SELECT COALESCE(settings->'tts_profiles', '[]'::jsonb) AS profiles
       FROM tenants WHERE id = $1`,
    [tenantId],
    tenantId
  );
  if (!res.rows.length) return [];
  return res.rows[0].profiles ?? [];
}

/**
 * saveTtsProfiles(tenantId?, profiles) → void
 * Replaces all profiles for the tenant (full overwrite).
 */
async function saveTtsProfiles(tenantId = DEV_TENANT_ID, profiles) {
  const arr = Array.isArray(profiles) ? profiles : [];
  await query(
    `UPDATE tenants
        SET settings   = jsonb_set(settings, '{tts_profiles}', $2::jsonb, true),
            updated_at = now()
      WHERE id = $1`,
    [tenantId, JSON.stringify(arr)],
    tenantId
  );
}

/**
 * deleteTtsProfile(tenantId?, profileId) → void
 * Removes a single profile by id from the array.
 */
async function deleteTtsProfile(tenantId = DEV_TENANT_ID, profileId) {
  // Filter the JSONB array server-side — no need to round-trip the full array
  await query(
    `UPDATE tenants
        SET settings   = jsonb_set(
              settings,
              '{tts_profiles}',
              (
                SELECT COALESCE(jsonb_agg(elem), '[]'::jsonb)
                  FROM jsonb_array_elements(
                         COALESCE(settings->'tts_profiles', '[]'::jsonb)
                       ) elem
                 WHERE elem->>'id' <> $2
              ),
              true
            ),
            updated_at = now()
      WHERE id = $1`,
    [tenantId, profileId],
    tenantId
  );
}

// ═════════════════════════════════════════════════════════════════════════════
// JOBS
// Replaces: jobs.json / tts-jobs.json / imagen-jobs.json / activeJobs Map
// ═════════════════════════════════════════════════════════════════════════════

/**
 * createJob(tenantId?, userId?, jobType, outputPrefix?) → string (job_id UUID)
 */
async function createJob(
  tenantId    = DEV_TENANT_ID,
  userId      = DEV_USER_ID,
  jobType,
  outputPrefix = null
) {
  const res = await query(
    `INSERT INTO jobs (tenant_id, user_id, job_type, status, output_prefix, started_at)
          VALUES ($1, $2, $3::job_type_enum, 'processing', $4, now())
       RETURNING id`,
    [tenantId, userId, jobType, outputPrefix],
    tenantId
  );
  return res.rows[0].id;
}

/**
 * _jobTenant(jobId) → tenant_id string | null
 * Internal: looks up a job's owning tenant so background updates (which have no
 * request context) can still satisfy RLS. Runs with empty context but reads only
 * by primary key — keep an RLS bypass policy for this OR call from the owner role.
 */
async function _jobTenant(jobId) {
  // Uses the SECURITY DEFINER function job_tenant() (see migration 0014) which
  // safely bypasses RLS for this single by-primary-key lookup. Without it, an
  // UPDATE under FORCE RLS with no tenant context would match zero rows.
  const client = await pool.connect();
  try {
    const r = await client.query(`SELECT job_tenant($1) AS tid`, [jobId]);
    return r.rows[0]?.tid ?? null;
  } finally {
    client.release();
  }
}

/**
 * updateJobProgress(jobId, message) → void
 * Updates progress_message and appends message to logs array.
 */
async function updateJobProgress(jobId, message) {
  const tenantId = await _jobTenant(jobId);
  await query(
    `UPDATE jobs
        SET progress_message = $2,
            logs             = logs || to_jsonb($2::text),
            updated_at       = now()
      WHERE id = $1`,
    [jobId, message],
    tenantId
  );
}

/**
 * completeJob(jobId, resultPayload) → void
 * resultPayload: any object — stored as result_payload JSONB.
 */
async function completeJob(jobId, resultPayload) {
  const tenantId = await _jobTenant(jobId);
  await query(
    `UPDATE jobs
        SET status          = 'done',
            result_payload  = $2::jsonb,
            progress_message = 'Selesai',
            completed_at    = now(),
            updated_at      = now()
      WHERE id = $1`,
    [jobId, JSON.stringify(resultPayload ?? {})],
    tenantId
  );
}

/**
 * failJob(jobId, errorMessage) → void
 */
async function failJob(jobId, errorMessage) {
  const tenantId = await _jobTenant(jobId);
  await query(
    `UPDATE jobs
        SET status        = 'error',
            error_message = $2,
            updated_at    = now()
      WHERE id = $1`,
    [jobId, String(errorMessage)],
    tenantId
  );
}

/**
 * getJob(tenantId?, jobId) → object | null
 */
async function getJob(tenantId = DEV_TENANT_ID, jobId) {
  const res = await query(
    `SELECT * FROM jobs WHERE id = $1 AND tenant_id = $2`,
    [jobId, tenantId],
    tenantId
  );
  return res.rows[0] ?? null;
}

/**
 * listJobs(tenantId?, jobType) → array
 * Lists a tenant's jobs of one type, newest first, capped at 100.
 * Replaces: readJson(BATCH_JOBS_FILE/TTS_JOBS_FILE/IMAGEN_JOBS_FILE, [])
 */
async function listJobs(tenantId = DEV_TENANT_ID, jobType) {
  const res = await query(
    `SELECT * FROM jobs
      WHERE tenant_id = $1 AND job_type = $2::job_type_enum
      ORDER BY created_at DESC
      LIMIT 100`,
    [tenantId, jobType],
    tenantId
  );
  return res.rows;
}

/**
 * findJobByJobName(tenantId?, jobName) → object | null
 * Batch-only lookup. Batch jobs store Google's batch resource name in
 * result_payload->>'jobName'. Returns the most recent matching row.
 */
async function findJobByJobName(tenantId = DEV_TENANT_ID, jobName) {
  const res = await query(
    `SELECT * FROM jobs
      WHERE tenant_id = $1
        AND job_type = 'batch_image'::job_type_enum
        AND result_payload->>'jobName' = $2
      ORDER BY created_at DESC
      LIMIT 1`,
    [tenantId, jobName],
    tenantId
  );
  return res.rows[0] ?? null;
}

/**
 * patchJobPayload(jobId, patch) → void
 * Shallow-merges `patch` into result_payload (jsonb || jsonb). Used by batch
 * /api/status and /api/retrieve, and to seed TTS/Imagen metadata, without a
 * full create→complete lifecycle.
 */
async function patchJobPayload(jobId, patch) {
  const tenantId = await _jobTenant(jobId);
  await query(
    `UPDATE jobs
        SET result_payload = COALESCE(result_payload, '{}'::jsonb) || $2::jsonb,
            updated_at     = now()
      WHERE id = $1`,
    [jobId, JSON.stringify(patch ?? {})],
    tenantId
  );
}

// ═════════════════════════════════════════════════════════════════════════════
// ASSETS  (object-storage file references — Step 2; mirrors database.py insert_asset)
// ═════════════════════════════════════════════════════════════════════════════

/**
 * insertAsset({...}) → string (asset id UUID)
 * Records one object-storage file in `assets` (storage metadata + moat capture).
 * Idempotent on (bucket, s3_key): a re-upload updates size/content_type.
 *   assetType      ∈ video|audio|image|document|archive|other   (required)
 *   sourceJobType  ∈ batch_image|tts|imagen|veo|sora | null      (job_type_enum)
 */
// asset_type → modality (text|image|video|audio). document/archive/other → null.
const ASSET_MODALITY = { image: "image", video: "video", audio: "audio" };

async function insertAsset({
  tenantId, userId = null, jobId = null,
  bucket, s3Key, originalFilename = null,
  contentType, sizeBytes = 0, assetType,
  sourceJobType = null, metadata = {}, modality = null, sourcePrompt = null,
  projectId = null,
} = {}) {
  // Step 1 (moat): modality auto-derives from asset_type; source_prompt falls back
  // to metadata.prompt — both let one query span narration + image + video signal.
  const md = metadata || {};
  const _modality = modality || ASSET_MODALITY[assetType] || null;
  const _prompt   = sourcePrompt != null ? sourcePrompt : (md.prompt ?? md.text ?? null);
  const _pid      = uuidOrNull(projectId);  // malformed id → null, never 22P02-drops the asset
  const res = await query(
    `INSERT INTO assets
         (tenant_id, user_id, job_id, bucket, s3_key, original_filename,
          content_type, size_bytes, asset_type, source_job_type, metadata,
          modality, source_prompt, project_id)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::job_type_enum,$11::jsonb,$12,$13,
             -- self-guarding project_id: resolves to the id only when it is one
             -- of THIS tenant's projects, else NULL (asset lands Unassigned).
             -- Stops a forged/foreign project id from mis-filing an asset.
             (SELECT p.id FROM projects p WHERE p.id = $14::uuid AND p.tenant_id = $1))
     ON CONFLICT (bucket, s3_key) DO UPDATE SET
         size_bytes    = EXCLUDED.size_bytes,
         content_type  = EXCLUDED.content_type,
         modality      = COALESCE(EXCLUDED.modality, assets.modality),
         source_prompt = COALESCE(EXCLUDED.source_prompt, assets.source_prompt),
         project_id    = COALESCE(EXCLUDED.project_id, assets.project_id),
         updated_at    = now()
     RETURNING id`,
    [tenantId, userId, jobId, bucket, s3Key, originalFilename,
     contentType, sizeBytes, assetType, sourceJobType,
     JSON.stringify(md), _modality, _prompt, _pid],
    tenantId
  );
  return res.rows[0]?.id ?? null;
}

/**
 * listAssets(tenantId, {assetType?, sourceJobType?, projectId?, limit?, before?}) → rows
 * Durable retrieval of a tenant's generated assets straight from the `assets`
 * table (survives redeploy + any job cleanup). Newest first, RLS-scoped.
 * Cursor pagination via `before` (pass the previous page's last created_at).
 *   projectId  a UUID       → only that project's assets (folder-open view)
 *              '__none__'   → only Unassigned assets (project_id IS NULL)
 *              null         → all assets regardless of project
 */
async function listAssets(tenantId, {
  assetType = null, sourceJobTypes = null, metadataKind = null,
  projectId = null, limit = 50, before = null,
} = {}) {
  const lim = Math.min(Math.max(1, parseInt(limit) || 50), 200);
  const conds = ["tenant_id = $1", "is_deleted = false"];
  const params = [tenantId];
  if (assetType) { params.push(assetType); conds.push(`asset_type = $${params.length}`); }
  if (Array.isArray(sourceJobTypes) && sourceJobTypes.length) {
    params.push(sourceJobTypes);
    conds.push(`source_job_type = ANY($${params.length}::job_type_enum[])`);
  }
  if (metadataKind) { params.push(metadataKind); conds.push(`metadata->>'kind' = $${params.length}`); }
  if (projectId === "__none__") {
    conds.push("project_id IS NULL");
  } else {
    const _pid = uuidOrNull(projectId);  // garbage id → treat as null (all), never 500 the read
    if (_pid) { params.push(_pid); conds.push(`project_id = $${params.length}::uuid`); }
  }
  if (before) { params.push(before); conds.push(`created_at < $${params.length}`); }
  params.push(lim);
  const res = await query(
    `SELECT id, asset_type, source_job_type, bucket, s3_key, original_filename,
            content_type, size_bytes, metadata, project_id, created_at
       FROM assets
      WHERE ${conds.join(" AND ")}
      ORDER BY created_at DESC
      LIMIT $${params.length}`,
    params, tenantId
  );
  return res.rows;
}

// ═════════════════════════════════════════════════════════════════════════════
// PROJECTS  (user-created collections/folders assets belong to — migration 0051)
// The unit the user packages + exports to CapCut/FCP. Tenant-scoped, RLS-safe.
// Single-project-per-asset via assets.project_id (0052, ON DELETE SET NULL).
// ═════════════════════════════════════════════════════════════════════════════

/**
 * createProject(tenantId, {userId?, name, description?}) → {id, name, description, created_at, updated_at}
 * `name` is required and trimmed; blank names are rejected by the caller.
 */
async function createProject(tenantId, { userId = null, name, description = null } = {}) {
  const res = await query(
    `INSERT INTO projects (tenant_id, user_id, name, description)
     VALUES ($1, $2, $3, $4)
     RETURNING id, name, description, created_at, updated_at`,
    [tenantId, userId, name, description],
    tenantId
  );
  return res.rows[0] ?? null;
}

/**
 * listProjects(tenantId) → [{id, name, description, created_at, updated_at, asset_count}]
 * Folder-grid data: every project for the tenant, newest first, with a live
 * count of assigned (non-deleted) assets so the grid can show "N items".
 */
async function listProjects(tenantId) {
  const res = await query(
    `SELECT p.id, p.name, p.description, p.created_at, p.updated_at,
            COUNT(a.id) FILTER (WHERE a.is_deleted = false) AS asset_count
       FROM projects p
       LEFT JOIN assets a ON a.project_id = p.id AND a.tenant_id = p.tenant_id
      WHERE p.tenant_id = $1
      GROUP BY p.id
      ORDER BY p.created_at DESC`,
    [tenantId], tenantId
  );
  return res.rows.map((r) => ({ ...r, asset_count: Number(r.asset_count) || 0 }));
}

/**
 * getProject(tenantId, projectId) → row | null
 */
async function getProject(tenantId, projectId) {
  const res = await query(
    `SELECT id, name, description, created_at, updated_at
       FROM projects WHERE id = $1 AND tenant_id = $2`,
    [projectId, tenantId], tenantId
  );
  return res.rows[0] ?? null;
}

/**
 * updateProject(tenantId, projectId, {name?, description?}) → row | null
 * Only provided fields change (COALESCE keeps the existing value on null).
 */
async function updateProject(tenantId, projectId, { name = null, description = null } = {}) {
  const res = await query(
    `UPDATE projects
        SET name        = COALESCE($3, name),
            description  = COALESCE($4, description)
      WHERE id = $1 AND tenant_id = $2
      RETURNING id, name, description, created_at, updated_at`,
    [projectId, tenantId, name, description], tenantId
  );
  return res.rows[0] ?? null;
}

/**
 * deleteProject(tenantId, projectId) → boolean (true if a row was removed)
 * Non-destructive to assets: assets.project_id is ON DELETE SET NULL, so the
 * project's assets fall back to "Unassigned" rather than being deleted.
 */
async function deleteProject(tenantId, projectId) {
  const res = await query(
    `DELETE FROM projects WHERE id = $1 AND tenant_id = $2`,
    [projectId, tenantId], tenantId
  );
  return (res.rowCount || 0) > 0;
}

/**
 * assignAssetsToProject(tenantId, assetIds[], projectId|null) → number (rows moved)
 * Multi-select "Move to project". projectId=null moves assets back to Unassigned.
 * When projectId is set, it must belong to the tenant (guarded by the caller
 * and by the tenant_id predicate here — a foreign project id matches nothing).
 */
async function assignAssetsToProject(tenantId, assetIds, projectId = null) {
  if (!Array.isArray(assetIds)) return 0;
  // Drop malformed asset ids (one bad element in $2::uuid[] would 22P02 the whole
  // move); normalize the target so a garbage projectId degrades to Unassigned
  // rather than raising. A well-formed foreign id survives and is nulled by EXISTS.
  const ids  = assetIds.filter((x) => uuidOrNull(x));
  if (!ids.length) return 0;
  const _pid = uuidOrNull(projectId);
  // The EXISTS guard makes a forged foreign project id a no-op: a target that
  // isn't NULL and isn't one of THIS tenant's projects matches zero rows, so an
  // asset can never be attached to another tenant's folder even if the route
  // check is bypassed. NULL always passes (→ move to Unassigned).
  const res = await query(
    `UPDATE assets
        SET project_id = $3, updated_at = now()
      WHERE tenant_id = $1
        AND id = ANY($2::uuid[])
        AND ($3::uuid IS NULL
             OR EXISTS (SELECT 1 FROM projects p
                         WHERE p.id = $3 AND p.tenant_id = $1))`,
    [tenantId, ids, _pid], tenantId
  );
  return res.rowCount || 0;
}

// ═════════════════════════════════════════════════════════════════════════════
// CHAT SESSIONS  (mirrors database.py get_or_create_session / append_message)
// ═════════════════════════════════════════════════════════════════════════════

/**
 * getOrCreateSession(tenantId, userId, sessionId, model, systemPrompt?) → void
 * Upserts a row in chat_sessions. Safe to call on every request.
 */
async function getOrCreateSession(
  tenantId, userId, sessionId, model, systemPrompt = ""
) {
  await query(
    `INSERT INTO chat_sessions
         (id, tenant_id, user_id, model, system_prompt, created_at, last_message_at)
     VALUES ($1, $2, $3, $4, $5, now(), now())
     ON CONFLICT (id) DO UPDATE
         SET model            = EXCLUDED.model,
             system_prompt    = EXCLUDED.system_prompt,
             last_message_at  = now()`,
    [sessionId, tenantId, userId, model, systemPrompt || ""],
    tenantId
  );
}

/**
 * appendMessage(tenantId, sessionId, role, content, model?, tokensIn?, tokensOut?) → void
 * Inserts one chat_messages row and bumps last_message_at on the session.
 */
async function appendMessage(
  tenantId, sessionId, role, content,
  model = null, tokensIn = 0, tokensOut = 0
) {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    await client.query(
      "SELECT set_config('app.current_tenant_id', $1, true)", [tenantId || ""]
    );
    const { rows } = await client.query(
      "SELECT COALESCE(MAX(sequence_number),0)+1 AS seq FROM chat_messages WHERE session_id=$1",
      [sessionId]
    );
    const seq = rows[0].seq;
    await client.query(
      `INSERT INTO chat_messages
           (tenant_id, session_id, role, content,
            tokens_in, tokens_out, sequence_number, finish_reason)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`,
      [tenantId, sessionId, role, content,
       tokensIn || null, tokensOut || null, seq,
       role === "assistant" ? "stop" : null]
    );
    await client.query(
      "UPDATE chat_sessions SET last_message_at=now() WHERE id=$1", [sessionId]
    );
    await client.query("COMMIT");
  } catch (e) {
    await client.query("ROLLBACK");
    throw e;
  } finally {
    client.release();
  }
}

/**
 * logUsage(tenantId, userId, model, endpoint, tokensIn, tokensOut, costUsd, sessionId?, provider?) → void
 * Inserts one row into usage_logs.
 * NOTE: endpoint must be one of: chat|image|tts|video|embedding|batch|other
 *       provider must be one of: laozhang|deepseek|gemini|openai|other
 */
async function logUsage(
  tenantId, userId, model, endpoint,
  tokensIn, tokensOut, costUsd,
  sessionId = null, provider = "gemini", jobId = null
) {
  // Step 4: this is the choke point for every DIRECT Node (google-native) paid
  // call, so charge credits here too — not just write a usage row. Python-proxied
  // routes are metered Python-side and never reach this function, so no double-charge.
  const credits = _METERING_ON ? _usdToCredits(costUsd) : 0;
  await query(
    `INSERT INTO usage_logs
         (tenant_id, user_id, session_id, job_id, endpoint,
          model_alias, model_upstream, provider,
          tokens_in, tokens_out, cost_usd, credits,
          finish_reason, http_status)
     VALUES ($1,$2,$3,$4,$5,$6,$6,$7,$8,$9,$10,$11,'stop',200)`,
    [tenantId, userId, sessionId, jobId, endpoint,
     model, provider, tokensIn, tokensOut, costUsd, credits],
    tenantId
  );
  if (credits > 0) {
    try {
      // durable debit (idempotent op_id) + mirror into the Redis live balance if cached
      await query(
        `SELECT credit_apply($1,$2,$3,'charge',$4,'{}'::jsonb)`,
        [tenantId, userId, -credits,
         `usage:${endpoint}:${Date.now()}:${Math.random().toString(36).slice(2, 8)}`],
        tenantId);
      try {
        await redis.eval(
          "if redis.call('EXISTS',KEYS[1])==1 then return redis.call('DECRBY',KEYS[1],ARGV[1]) else return -1 end",
          1, `bal:${tenantId}:credits`, String(credits));
      } catch (_) { /* redis optional */ }
    } catch (e) { console.error("[logUsage debit]", e.message); }
  }
}

/**
 * logSyncJob(tenantId, jobType, result?) → string (job id) | null
 * Inserts an already-'done' jobs row for a synchronous one-shot AI flow (Node
 * google-native routes). Mirrors database.py log_sync_job. user_id left NULL.
 */
async function logSyncJob(tenantId, jobType, result = {}) {
  try {
    const res = await query(
      `INSERT INTO jobs (tenant_id, job_type, status, progress_message,
                         result_payload, started_at, completed_at)
       VALUES ($1, $2::job_type_enum, 'done', 'Selesai', $3::jsonb, now(), now())
       RETURNING id`,
      [tenantId, jobType, JSON.stringify(result || {})],
      tenantId
    );
    return res.rows[0]?.id ?? null;
  } catch (e) { console.error("[logSyncJob]", jobType, e.message); return null; }
}

// ── Google model cost table ($/M tokens) ─────────────────────────────────────
const GOOGLE_COSTS = {
  "gemini-2.5-flash": [0.15, 0.60],
  "gemini-2.5-pro":   [1.25, 10.00],
  "gemini-2.0-flash": [0.10,  0.40],
  "gemini-1.5-flash": [0.075, 0.30],
  "gemini-1.5-pro":   [1.25,  5.00],
};

function calcGoogleCost(model, tokensIn, tokensOut) {
  const key = Object.keys(GOOGLE_COSTS).find(k => model.startsWith(k));
  if (!key) return 0;
  const [inP, outP] = GOOGLE_COSTS[key];
  return +((tokensIn * inP + tokensOut * outP) / 1_000_000).toFixed(8);
}

export {
  // internals (for tests / advanced use)
  db,
  pool,
  DEV_TENANT_ID,
  DEV_USER_ID,
  _uuid5,

  // tenant helpers
  resolveTenantId,
  resolveUserId,
  setTenantContext,
  query,

  // config
  getConfig,
  setConfig,

  // tts profiles
  getTtsProfiles,
  saveTtsProfiles,
  deleteTtsProfile,

  // jobs
  createJob,
  updateJobProgress,
  completeJob,
  failJob,
  getJob,
  listJobs,
  findJobByJobName,
  patchJobPayload,

  // assets (object storage)
  insertAsset,
  listAssets,

  // projects (collections/folders — migration 0051/0052)
  createProject,
  listProjects,
  getProject,
  updateProject,
  deleteProject,
  assignAssetsToProject,
  uuidOrNull,   // shared UUID guard (route-layer :id validation)

  // sync-flow job ledger
  logSyncJob,

  // chat sessions + usage
  getOrCreateSession,
  appendMessage,
  logUsage,
  calcGoogleCost,
};
