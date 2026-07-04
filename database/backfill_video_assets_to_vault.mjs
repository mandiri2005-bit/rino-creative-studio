// ─────────────────────────────────────────────────────────────────────────────
// backfill_video_assets_to_vault.mjs — one-time (idempotent) backfill of past
// video-worker uploads (WB + standard clip + Avatar) into the `assets` table so
// they show up in Media Vault.
//
// Rino-level context (2026-07-04): the video-worker (workers.mjs) uploads MP4s
// directly to R2 and only stamps `mp4Key` in Redis (`vjob:{id}`, 24h TTL) —
// it never called db.insert_asset. Videos lived in R2 but were invisible to the
// Vault (Vault reads `assets`). Commit f5191a7 wired registerVideoAsset() for
// new jobs; this script backfills the ones that shipped BEFORE that.
//
// Approach: scan Redis `vjob:*` → keep entries with status=done + mp4Key → for
// each, INSERT INTO assets (bypass API, direct SQL — same idempotency as the
// endpoint: ON CONFLICT (bucket, s3_key) DO UPDATE). Also stashes the BullMQ
// id under metadata.bullmq_job_id so support can trace back.
//
// Usage:
//   node database/backfill_video_assets_to_vault.mjs [--branch main|staging|dev]
//                                                    [--dry-run] [--limit N]
//                                                    [--tenant <uuid>]
//
// Idempotent: safe to re-run — already-registered rows get updated in place.
// ─────────────────────────────────────────────────────────────────────────────
import fs from "fs";
import pg from "pg";
import Redis from "ioredis";

// ── args ─────────────────────────────────────────────────────────────────────
const args = process.argv.slice(2);
const argVal = (k, dflt) => {
  const i = args.findIndex((a) => a === k || a.startsWith(`${k}=`));
  if (i < 0) return dflt;
  const eq = args[i].indexOf("=");
  if (eq > 0) return args[i].slice(eq + 1);
  return args[i + 1];
};
const DRY = args.includes("--dry-run");
const LIMIT = Number(argVal("--limit", "0")) || 0;
const BRANCH = (argVal("--branch", "main") || "main").toLowerCase();
const TENANT = argVal("--tenant");

// ── .env (loaded before any storage import) ──────────────────────────────────
try {
  const envText = fs.readFileSync(new URL("../.env", import.meta.url), "utf8");
  for (const line of envText.split("\n")) {
    const t = line.trim();
    if (!t || t.startsWith("#") || !t.includes("=")) continue;
    const i = t.indexOf("=");
    const k = t.slice(0, i).trim();
    if (!(k in process.env)) process.env[k] = t.slice(i + 1).trim();
  }
} catch { /* no .env is OK if envs are already set */ }

const DB_URL_KEY = { main: "DATABASE_URL", staging: "DATABASE_URL_STAGING", dev: "DATABASE_URL_DEV" };
const dbUrl = process.env[DB_URL_KEY[BRANCH]] || process.env.DATABASE_URL;
if (!dbUrl) { console.error(`no DATABASE_URL (or ${DB_URL_KEY[BRANCH]}) in env`); process.exit(1); }
const redisUrl = process.env.REDIS_URL;
if (!redisUrl) { console.error("no REDIS_URL in env"); process.exit(1); }
const bucket = (process.env.STORAGE_BUCKET || process.env.R2_BUCKET || "").trim();
if (!bucket) { console.error("no STORAGE_BUCKET / R2_BUCKET in env"); process.exit(1); }

console.log(`branch=${BRANCH} bucket=${bucket} dry-run=${DRY}${LIMIT ? ` limit=${LIMIT}` : ""}${TENANT ? ` tenant=${TENANT}` : ""}`);

// ── connections ──────────────────────────────────────────────────────────────
const pgc = new pg.Client({ connectionString: dbUrl, ssl: { rejectUnauthorized: false } });
await pgc.connect();

const redis = new Redis(redisUrl, {
  maxRetriesPerRequest: 3,
  enableReadyCheck: true,
  connectTimeout: 20000,
  tls: redisUrl.startsWith("rediss://") ? { rejectUnauthorized: false } : undefined,
});

// ── scan vjob:* — TOP-LEVEL keys only (skip vjob:{id}:scene:… etc) ───────────
async function scanTopLevelJobs() {
  const out = [];
  let cursor = "0";
  do {
    const [next, batch] = await redis.scan(cursor, "MATCH", "vjob:*", "COUNT", 500);
    cursor = next;
    for (const k of batch) {
      // vjob:{id} — no additional ":" segment (scene / stitch / dispatched carry them)
      if (/^vjob:[^:]+$/.test(k)) out.push(k);
    }
  } while (cursor !== "0");
  return out;
}

// ── insert one row into assets (identical to db.js insertAsset, direct SQL) ─
async function insertAsset({ tenantId, userId, bucket, s3Key, contentType, sizeBytes,
                              assetType, sourceJobType, metadata, projectId }) {
  const modality = assetType === "video" ? "video"
    : assetType === "image" ? "image"
    : assetType === "audio" ? "audio" : null;
  const md = metadata || {};
  const sourcePrompt = md.prompt || md.text || null;
  const sql = `
    INSERT INTO assets
        (tenant_id, user_id, job_id, bucket, s3_key, original_filename,
         content_type, size_bytes, asset_type, source_job_type, metadata,
         modality, source_prompt, project_id)
      VALUES ($1, $2, NULL, $3, $4, NULL,
              $5, $6, $7, $8::job_type_enum, $9::jsonb, $10, $11,
              (SELECT p.id FROM projects p WHERE p.id = $12::uuid AND p.tenant_id = $1))
    ON CONFLICT (bucket, s3_key) DO UPDATE SET
      size_bytes   = EXCLUDED.size_bytes,
      content_type = EXCLUDED.content_type,
      modality     = COALESCE(EXCLUDED.modality, assets.modality),
      source_prompt= COALESCE(EXCLUDED.source_prompt, assets.source_prompt),
      metadata     = assets.metadata || EXCLUDED.metadata,
      updated_at   = now()
    RETURNING id`;
  const params = [
    tenantId, userId || null, bucket, s3Key,
    contentType || "video/mp4", sizeBytes || 0, assetType, sourceJobType || "veo",
    JSON.stringify(md), modality, sourcePrompt, projectId || null,
  ];
  const r = await pgc.query(sql, params);
  return r.rows[0]?.id || null;
}

// ── main ──────────────────────────────────────────────────────────────────────
const stats = { scanned: 0, skipped_no_mp4: 0, skipped_not_done: 0, skipped_no_tenant: 0,
                skipped_tenant_filter: 0, inserted: 0, errors: 0 };

const keys = await scanTopLevelJobs();
console.log(`found ${keys.length} top-level vjob keys`);

let processed = 0;
for (const key of keys) {
  stats.scanned += 1;
  let m; try { const raw = await redis.get(key); if (!raw) continue; m = JSON.parse(raw); }
  catch (e) { stats.errors += 1; console.warn(`[${key}] parse: ${e.message}`); continue; }

  if (m.status !== "done") { stats.skipped_not_done += 1; continue; }
  if (!m.mp4Key) { stats.skipped_no_mp4 += 1; continue; }
  if (!m.tenantId) { stats.skipped_no_tenant += 1; continue; }
  if (TENANT && m.tenantId !== TENANT) { stats.skipped_tenant_filter += 1; continue; }

  const jobId = key.replace(/^vjob:/, "");
  const md = {
    visualMode: m.visualMode || "video",
    whiteboardGenre: m.whiteboardGenre || null,
    durationSec: Math.round(m.durationActual || 0),
    aspectRatio: m.aspectRatio || "16:9",
    bullmq_job_id: jobId,
    backfilled_at: new Date().toISOString(),
  };

  if (DRY) {
    console.log(`[dry] ${jobId} tenant=${m.tenantId.slice(0,8)} mp4Key=${m.mp4Key} vmode=${md.visualMode}`);
  } else {
    try {
      const id = await insertAsset({
        tenantId: m.tenantId, userId: m.userId || null,
        bucket, s3Key: m.mp4Key,
        contentType: "video/mp4",
        sizeBytes: 0,  // unknown post-hoc; storage.headObject roundtrip skipped for speed
        assetType: "video",
        sourceJobType: "veo",
        metadata: md,
        projectId: m.projectId || null,
      });
      stats.inserted += 1;
      console.log(`✓ ${jobId} → asset_id=${id} (tenant ${m.tenantId.slice(0,8)}) mp4Key=${m.mp4Key}`);
    } catch (e) {
      stats.errors += 1;
      console.error(`✗ ${jobId} tenant ${m.tenantId.slice(0,8)}: ${e.message}`);
    }
  }

  processed += 1;
  if (LIMIT && processed >= LIMIT) { console.log(`limit ${LIMIT} reached — stopping`); break; }
}

console.log("");
console.log("summary:");
for (const [k, v] of Object.entries(stats)) console.log(`  ${k.padEnd(24)} ${v}`);
console.log(DRY ? "(dry-run — nothing written)" : "done");

await redis.quit();
await pgc.end();
process.exit(stats.errors > 0 && !DRY ? 1 : 0);
