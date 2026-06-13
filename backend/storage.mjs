// ─────────────────────────────────────────────────────────────────────────────
// storage.mjs — Object-storage abstraction (Cloudflare R2 / any S3-compatible).
//
// Node counterpart to python/storage.py. Wraps the bucket via @aws-sdk/client-s3
// so server.js never treats local disk as the source of truth. Reads the same
// STORAGE_* env the Python side does (see python/storage.py header for the list).
//
// Key convention (matches assets.s3_key in 0008_create_assets.sql):
//   tenants/{tenantId}/jobs/{jobId}/{assetType}/{filename}
//
// isConfigured() is false until STORAGE_ACCESS_KEY + STORAGE_SECRET_KEY are set,
// so callers can keep writing to local disk as a fallback until R2 is provisioned.
// ─────────────────────────────────────────────────────────────────────────────
import {
  S3Client,
  PutObjectCommand,
  GetObjectCommand,
  HeadObjectCommand,
  DeleteObjectCommand,
} from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";

const ENDPOINT   = (process.env.STORAGE_ENDPOINT   || "").trim();
const ACCESS_KEY = (process.env.STORAGE_ACCESS_KEY || "").trim();
const SECRET_KEY = (process.env.STORAGE_SECRET_KEY || "").trim();
const BUCKET     = (process.env.STORAGE_BUCKET     || "").trim();
const REGION     = (process.env.STORAGE_REGION     || "auto").trim() || "auto";

const DEFAULT_EXPIRY = 600; // 10 minutes

export function isConfigured() {
  return Boolean(ENDPOINT && ACCESS_KEY && SECRET_KEY && BUCKET);
}

// ── Lazy singleton client ────────────────────────────────────────────────────
let _client = null;
function client() {
  if (!_client) {
    if (!isConfigured()) {
      throw new Error(
        "storage not configured — set STORAGE_ENDPOINT/ACCESS_KEY/SECRET_KEY/BUCKET in .env"
      );
    }
    _client = new S3Client({
      region: REGION,
      endpoint: ENDPOINT,
      forcePathStyle: true, // R2 works with path-style addressing
      credentials: { accessKeyId: ACCESS_KEY, secretAccessKey: SECRET_KEY },
    });
  }
  return _client;
}

// ── Key builder ──────────────────────────────────────────────────────────────
export function buildKey(tenantId, jobId, assetType, filename) {
  const safe = String(filename).replace(/^\/+/, "");
  const job = jobId ? String(jobId) : "_";
  return `tenants/${tenantId}/jobs/${job}/${assetType}/${safe}`;
}

// ── Core ─────────────────────────────────────────────────────────────────────
export async function uploadBytes(key, data, contentType = "application/octet-stream") {
  await client().send(
    new PutObjectCommand({ Bucket: BUCKET, Key: key, Body: data, ContentType: contentType })
  );
  return key;
}

export async function downloadBytes(key) {
  const out = await client().send(new GetObjectCommand({ Bucket: BUCKET, Key: key }));
  const chunks = [];
  for await (const chunk of out.Body) chunks.push(chunk);
  return Buffer.concat(chunks);
}

export async function signedUrl(key, expirySeconds = DEFAULT_EXPIRY) {
  return getSignedUrl(
    client(),
    new GetObjectCommand({ Bucket: BUCKET, Key: key }),
    { expiresIn: Number(expirySeconds) }
  );
}

export async function exists(key) {
  try {
    await client().send(new HeadObjectCommand({ Bucket: BUCKET, Key: key }));
    return true;
  } catch (e) {
    const code = e?.$metadata?.httpStatusCode;
    if (code === 404 || e?.name === "NotFound" || e?.name === "NoSuchKey") return false;
    throw e;
  }
}

export async function del(key) {
  await client().send(new DeleteObjectCommand({ Bucket: BUCKET, Key: key }));
}

export const BUCKET_NAME = BUCKET;
