# server.js — Migration Patch: JSON files → PostgreSQL
# Apply these 6 changes in order.

# ══════════════════════════════════════════════════════════════════════════════
# CHANGE 0 — Add import at the top of server.js  (after existing imports)
# ══════════════════════════════════════════════════════════════════════════════

# BEFORE  (no db import exists)

# AFTER — add immediately after the last import/const block, before DATA_DIR:

import db from "./db.js";
# Note: db.js uses CommonJS (module.exports). Because server.js uses ESM
# (import/export), Node.js interop handles this automatically — the named
# exports become properties of the default import.
# Destructure what you need:
const {
  getConfig, setConfig,
  getTtsProfiles, saveTtsProfiles, deleteTtsProfile,
  createJob, updateJobProgress, completeJob, failJob, getJob,
} = db;


# ══════════════════════════════════════════════════════════════════════════════
# CHANGE 1 — GET /api/config
# ══════════════════════════════════════════════════════════════════════════════

# BEFORE:
app.get("/api/config", (_,res) => res.json(readJson(CONFIG_FILE,null)));

# AFTER:
app.get("/api/config", async (_,res) => {
  try {
    res.json(await getConfig());
  } catch(e) { res.status(500).json({ error: e.message }); }
});


# ══════════════════════════════════════════════════════════════════════════════
# CHANGE 2 — POST /api/config
# ══════════════════════════════════════════════════════════════════════════════

# BEFORE:
app.post("/api/config",(req,res)=>{ writeJson(CONFIG_FILE,req.body||{}); res.json({ok:true}); });

# AFTER:
app.post("/api/config", async (req,res) => {
  try {
    await setConfig(undefined, req.body || {});
    res.json({ ok: true });
  } catch(e) { res.status(500).json({ error: e.message }); }
});


# ══════════════════════════════════════════════════════════════════════════════
# CHANGE 3 — GET /api/tts/profiles
# ══════════════════════════════════════════════════════════════════════════════

# BEFORE:
app.get("/api/tts/profiles", (_,res) => res.json(readJson(TTS_PROFILES_FILE, [])));

# AFTER:
app.get("/api/tts/profiles", async (_,res) => {
  try {
    res.json(await getTtsProfiles());
  } catch(e) { res.status(500).json({ error: e.message }); }
});


# ══════════════════════════════════════════════════════════════════════════════
# CHANGE 4 — POST /api/tts/profiles
# ══════════════════════════════════════════════════════════════════════════════

# BEFORE:
app.post("/api/tts/profiles", (req,res) => {
  const profiles = Array.isArray(req.body) ? req.body : [];
  writeJson(TTS_PROFILES_FILE, profiles);
  res.json({ ok:true, count: profiles.length });
});

# AFTER:
app.post("/api/tts/profiles", async (req,res) => {
  try {
    const profiles = Array.isArray(req.body) ? req.body : [];
    await saveTtsProfiles(undefined, profiles);
    res.json({ ok: true, count: profiles.length });
  } catch(e) { res.status(500).json({ error: e.message }); }
});


# ══════════════════════════════════════════════════════════════════════════════
# CHANGE 5 — DELETE /api/tts/profiles/:id
# ══════════════════════════════════════════════════════════════════════════════

# BEFORE:
app.delete("/api/tts/profiles/:id", (req,res) => {
  const profiles = readJson(TTS_PROFILES_FILE, []);
  const updated = profiles.filter(p => p.id !== req.params.id);
  writeJson(TTS_PROFILES_FILE, updated);
  res.json({ ok:true, count: updated.length });
});

# AFTER:
app.delete("/api/tts/profiles/:id", async (req,res) => {
  try {
    await deleteTtsProfile(undefined, req.params.id);
    const remaining = await getTtsProfiles();
    res.json({ ok: true, count: remaining.length });
  } catch(e) { res.status(500).json({ error: e.message }); }
});


# ══════════════════════════════════════════════════════════════════════════════
# INSTALL
# ══════════════════════════════════════════════════════════════════════════════
# In ./backend:
#   npm install drizzle-orm pg
#
# Add to docker-compose.yml under backend environment:
#   - DATABASE_POOL_URL_DEV=${DATABASE_POOL_URL_DEV}
#
# ══════════════════════════════════════════════════════════════════════════════
# NOTES ON JOBS (batch_image / tts / imagen)
# ══════════════════════════════════════════════════════════════════════════════
# The jobs.json / tts-jobs.json / imagen-jobs.json patterns inside
# runTtsJob, runLaozhangTtsJob, runImagenJob, and the /api/submit batch
# endpoint are Phase 2 work (BullMQ queues). They are intentionally left
# as-is here because:
#   1. They are tightly coupled to the in-memory activeJobs Map.
#   2. Phase 2 replaces both the Map AND the JSON file with BullMQ + DB.
#   3. Migrating them now without queues would leave job status non-durable.
#
# db.js already exports createJob / updateJobProgress / completeJob / failJob
# / getJob — they will be wired in during Phase 2.
