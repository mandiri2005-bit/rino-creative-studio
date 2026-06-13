// ─────────────────────────────────────────────────────────────────────────────
// backfill_assets_to_r2.mjs — one-time (idempotent) backfill of existing TEXT
// content (narasi chapters, outlines, chat transcripts) into R2 + the assets
// table, so they show up in the Media Vault with samples.
//
// Runs as the OWNER role (DATABASE_URL[_*]) which bypasses RLS, so it can read
// every tenant's rows and insert assets with the correct tenant_id. Idempotent:
// deterministic s3_key per source row → ON CONFLICT updates instead of dupes.
//
// Usage:  node database/backfill_assets_to_r2.mjs <dev|staging|main>
// ─────────────────────────────────────────────────────────────────────────────
import fs from "fs";
import pg from "pg";

// ── load .env BEFORE importing storage.mjs (it reads STORAGE_* at module load) ─
const env = fs.readFileSync(new URL("../.env", import.meta.url), "utf8");
for (const line of env.split("\n")) {
  const t = line.trim();
  if (!t || t.startsWith("#") || !t.includes("=")) continue;
  const i = t.indexOf("=");
  if (!(t.slice(0, i).trim() in process.env)) process.env[t.slice(0, i).trim()] = t.slice(i + 1).trim();
}
const storage = await import("../backend/storage.mjs");

const branch = (process.argv[2] || "staging").toLowerCase();
const URL_BY = { dev: "DATABASE_URL_DEV", staging: "DATABASE_URL_STAGING", main: "DATABASE_URL" };
const dbUrl = process.env[URL_BY[branch]];
if (!dbUrl) { console.error("No DB url for branch", branch); process.exit(1); }
if (!storage.isConfigured()) { console.error("STORAGE_* not configured"); process.exit(1); }

const c = new pg.Client({ connectionString: dbUrl, ssl: { rejectUnauthorized: false } });
const CT = "text/plain; charset=utf-8";
const slug = (s) => (s || "").toString().replace(/[^a-zA-Z0-9-_ ]/g, "").trim().slice(0, 48).replace(/\s+/g, "-") || "untitled";

async function putAsset({ tenantId, userId, jobId, key, filename, text, kind, extraMeta }) {
  const data = Buffer.from(text || "", "utf8");
  await storage.uploadBytes(key, data, CT);
  await c.query(
    `INSERT INTO assets (tenant_id,user_id,job_id,bucket,s3_key,original_filename,
                         content_type,size_bytes,asset_type,source_job_type,metadata)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'document',NULL,$9::jsonb)
     ON CONFLICT (bucket,s3_key) DO UPDATE SET
       size_bytes=EXCLUDED.size_bytes, content_type=EXCLUDED.content_type,
       metadata=EXCLUDED.metadata, updated_at=now()`,
    [tenantId, userId || null, jobId || null, storage.BUCKET_NAME, key, filename, CT,
     data.length, JSON.stringify({ kind, backfill: true, ...extraMeta })]
  );
}

async function main() {
  await c.connect();
  let n = { narasi: 0, outline: 0, chat: 0 };

  // 1. narasi chapters
  for (const r of (await c.query("SELECT id,tenant_id,user_id,job_id,chapter_index,content FROM narasi_chapters WHERE content<>''")).rows) {
    await putAsset({
      tenantId: r.tenant_id, userId: r.user_id, jobId: r.job_id,
      key: `tenants/${r.tenant_id}/backfill/narasi/${r.id}.txt`,
      filename: `narasi-bab-${r.chapter_index}.txt`, text: r.content, kind: "narasi",
      extraMeta: { source_id: r.id, chapter_index: r.chapter_index },
    });
    n.narasi++;
  }

  // 2. narasi outlines (table may not exist on some branches)
  try {
    for (const r of (await c.query("SELECT id,tenant_id,user_id,topic,style,outline_text FROM narasi_outlines WHERE COALESCE(outline_text,'')<>''")).rows) {
      await putAsset({
        tenantId: r.tenant_id, userId: r.user_id,
        key: `tenants/${r.tenant_id}/backfill/outline/${r.id}.txt`,
        filename: `outline-${slug(r.topic)}.txt`, text: r.outline_text, kind: "outline",
        extraMeta: { source_id: r.id, topic: r.topic, style: r.style },
      });
      n.outline++;
    }
  } catch (e) { console.log("  (narasi_outlines skipped:", e.code || e.message, ")"); }

  // 3. chat sessions → transcript
  for (const s of (await c.query("SELECT id,tenant_id,user_id,title,model,created_at FROM chat_sessions WHERE is_archived=false")).rows) {
    const msgs = (await c.query("SELECT role,content FROM chat_messages WHERE session_id=$1 ORDER BY sequence_number", [s.id])).rows;
    if (!msgs.length) continue;
    const transcript = `# ${s.title || "Chat"}\n_model: ${s.model || "?"}_\n\n` +
      msgs.map((m) => `## ${m.role}\n\n${m.content}`).join("\n\n---\n\n");
    await putAsset({
      tenantId: s.tenant_id, userId: s.user_id,
      key: `tenants/${s.tenant_id}/chat/${s.id}.txt`,   // canonical per-session key (matches live capture)
      filename: `chat-${s.created_at ? new Date(s.created_at).toISOString().slice(0,19).replace(/[:T]/g,"-") : "import"}.txt`, text: transcript, kind: "chat",
      extraMeta: { source_id: s.id, title: s.title, model: s.model, messages: msgs.length },
    });
    n.chat++;
  }

  await c.end();
  console.log(`[${branch}] backfilled → narasi:${n.narasi} outline:${n.outline} chat:${n.chat}`);
}

main().catch((e) => { console.error("backfill failed:", e); process.exit(1); });
