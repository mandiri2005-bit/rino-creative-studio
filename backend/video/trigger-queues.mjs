// ─────────────────────────────────────────────────────────────────────────────
// video/trigger-queues.mjs — API-side producers for the BullMQ trigger queues.
//
// BULLMQ MIGRATION (default OFF). Used ONLY by server.js when VIDEO_BULLMQ_ENABLED /
// RECIPE_BULLMQ_ENABLED is set. Queues are created LAZILY on first enqueue, so with
// the flags off nothing here ever runs and the API process opens no extra Redis
// connection (byte-identical default behavior). The producer just writes a job with
// {jobId, tenantId, userId[, slug]}; the worker (worker-entry.mjs) does the work by
// calling the idempotent Python /run endpoint.
// ─────────────────────────────────────────────────────────────────────────────
import { Queue } from "bullmq";
import { QUEUE, sharedConnection } from "./connection.mjs";
import { VIDEOCLIP_JOB_OPTS } from "./workers-videoclip.mjs";
import { RECIPE_JOB_OPTS } from "./workers-recipe.mjs";

let _videoClipQ = null;
let _recipeQ = null;

function videoClipQueue() {
  if (!_videoClipQ) _videoClipQ = new Queue(QUEUE.VIDEOCLIP, { connection: sharedConnection() });
  return _videoClipQ;
}
function recipeQueue() {
  if (!_recipeQ) _recipeQ = new Queue(QUEUE.RECIPE, { connection: sharedConnection() });
  return _recipeQ;
}

/** Enqueue one single-clip /video-tools job (Phase 2). jobId is the Python job row id. */
export async function enqueueVideoClip({ jobId, tenantId, userId }) {
  return videoClipQueue().add("videoclip", { jobId, tenantId, userId }, VIDEOCLIP_JOB_OPTS);
}

/** Enqueue one avatar-recipe job (Phase 3). slug ∈ product-ad|spokesperson. */
export async function enqueueRecipe({ jobId, tenantId, userId, slug }) {
  return recipeQueue().add("recipe", { jobId, tenantId, userId, slug }, RECIPE_JOB_OPTS);
}
