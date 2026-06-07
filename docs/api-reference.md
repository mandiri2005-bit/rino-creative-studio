# Rino Creative Studio — API Reference (Phase 1)

> **Scope:** This document describes the **Phase 1** HTTP contract as implemented in
> `server.js` (Node/Express public API) and `laozhang_api.py` (Python/FastAPI backend),
> backed by the multi-tenant PostgreSQL schema in `schema.sql`.
>
> The **public contract** is the Node `/api/*` surface reached through nginx on port `8080`.
> Most `/api/*` routes proxy to the Python backend (internal port `8000`); a few are served
> directly by Node (`@google/genai`). Where a route proxies, the upstream Python path is noted.
>
> **Verify-before-trust note:** Config field lists and the TTS-profile object shape are defined in
> `db.js`, which was not part of this export. Those two areas are marked **(defined in `db.js`)**
> and describe the wire behaviour observed in `server.js` rather than the exact persisted columns.

---

## 1. Overview

**Base URL pattern**

```
https://<host>:8080/api/...        # public API (Node Express, behind nginx)
https://<host>:8080/webhooks/...   # internal webhooks (Svix-signed, not for frontend)
```

- **All client requests go through nginx on port `8080`.** nginx forwards to the Node process
  (default `:3000`), which either answers directly or proxies to the Python FastAPI process
  (default `:8000`, `PYTHON_API_URL`) or the MCP sidecar (`:8001`, `MCP_API_URL`).
- **Auth:** Clerk JWT supplied as `Authorization: Bearer <token>`. Node's `clerkMiddleware()`
  runs on every request; protected routes additionally call `requireAuth`. The `Authorization`
  header is forwarded to Python, which decodes it (`get_current_user`) into a `tenant_id` and a
  resolved `users.id` UUID.
- **Tenant isolation:** every authenticated endpoint scopes data to the caller's tenant. The
  Python layer resolves the tenant from the JWT and the DB enforces it with PostgreSQL
  Row-Level Security: all tenant tables use `FORCE ROW LEVEL SECURITY` and a policy keyed off
  `current_setting('app.current_tenant_id')`. A request can only read/write rows whose
  `tenant_id` matches the JWT's tenant.
- **SSE:** the chat endpoints stream with `Content-Type: text/event-stream`. Clients must read
  the stream incrementally (e.g. `EventSource` or a `fetch` reader), not buffer the whole body.

**Two backends behind one surface**

| Concern | Served by | Notes |
|---|---|---|
| Chat (proxy + SSE) | Python via Node | `/api/chat` → Python `/chat/stream` |
| Chat (Google-native) | Node directly | `/api/chat/google` uses `@google/genai` |
| Narasi, one-shot fix, storyboard, image/video gen | Python via Node | proxied |
| Batch image, TTS, Imagen jobs | Node directly | `@google/genai`, state in Postgres + Redis |
| Config, TTS profiles | Node directly | `db.js` |
| Clerk webhook | Node directly | Svix-verified |

---

## 2. Authentication

**Obtaining a token (frontend)**

Clerk issues short-lived session JWTs. In the browser:

```js
const token = await window.Clerk.session.getToken();
fetch("/api/config", { headers: { Authorization: `Bearer ${token}` } });
```

**Token lifetime & refresh**

- Clerk session tokens are short-lived (on the order of one minute) and must be re-fetched per
  request or on a short timer. `getToken()` returns a fresh JWT and is the source of truth — do
  not cache a token for the life of the page.
- Phase 1 has no server-side refresh: the client is responsible for calling `getToken()` again
  when a request returns `401`. (Automatic refresh via `@clerk/nextjs` is a Phase 3 change.)

**401 vs 403**

| Status | Meaning | Typical cause |
|---|---|---|
| `401 Unauthorized` | No valid identity | Missing/expired/malformed `Authorization` header; `requireAuth` rejected the request |
| `403 Forbidden` | Valid identity, not allowed | Authenticated, but the resource belongs to another tenant or the role lacks permission |

On `401`, refresh the token and retry once. On `403`, do not retry — the caller is
authenticated but lacks access to that tenant/resource.

> **Tenant resolution:** `tenant_id` and the resolved `users.id` come from the JWT, never from
> request fields. The Clerk webhook (§11) provisions the tenant/user rows; in local dev the
> backend will just-in-time provision a minimal user row if the webhook has not fired.

---

## 3. Standard Error Response Shape

Errors are returned as JSON. The Node layer emits:

```json
{ "error": "string" }
```

Python raises `HTTPException`, whose body is `{ "detail": "string" }`. When Node proxies a Python
error it unwraps it to `{ "error": <detail> }`, so clients should read **`error` first, then
`detail`**:

```json
{ "error": "string", "detail": "string (optional)" }
```

During SSE streaming, errors arrive **inside the stream** as a data frame rather than as an HTTP
status: `data: [ERROR: <message>]` followed by `data: [DONE]` (see §4).

**Status codes used**

| Status | Where it appears | Meaning |
|---|---|---|
| `200 OK` | Most successful GET/POST/DELETE | Success; body contains the result |
| `201 Created` | (Reserved) | Resource creation. Phase 1 job-submit endpoints return `200` with a `job_id`, not `201`. |
| `400 Bad Request` | Validation failures | Missing/invalid field, empty body, unknown model, file too large, missing API key |
| `401 Unauthorized` | `requireAuth` / Python auth | No valid Clerk session |
| `403 Forbidden` | RLS / role checks | Authenticated but cross-tenant or insufficient role |
| `404 Not Found` | Session/job lookups | Session, job, or temp dir not found |
| `409 Conflict` | (Reserved) | Not emitted explicitly in Phase 1; unique-constraint collisions surface as `500`/`400` |
| `422 Unprocessable Entity` | FastAPI request-model validation | Pydantic rejected the body on a Python route (e.g. malformed `ChatRequest`) |
| `429 Too Many Requests` | Upstream provider passthrough | Provider rate limit (LaoZhang/Gemini) bubbles up; TTS key rotation treats `429/401/403` as "rotate key" |
| `500 Internal Server Error` | Unhandled exceptions, DB errors | Server-side failure; `error`/`detail` carries the message |
| `503 Service Unavailable` | DB/stream unavailable | `Database unavailable`, or a video stream not ready after retries |

> Note: `201`, `409`, and a strict `429` contract are not consistently produced in Phase 1.
> They are listed because clients should tolerate them; Phase 2 (queue-based jobs) will formalize
> `201` for submission and `429` for quota.

---

## 4. Chat Endpoints

Chat has three flavours: the **LaoZhang proxy stream** (`/api/chat`), the **agentic/MCP stream**
(`/api/chat/agentic`), and the **Google-native stream** (`/api/chat/google`). All three are SSE.
There is also a non-streaming one-shot (`/api/chat/once`).

### SSE event format (all streaming chat endpoints)

The stream is a sequence of `data:` frames separated by blank lines:

```
data: <text-chunk>

data: [USAGE:{"input":1234,"output":567,"finish":"stop"}]

data: [DONE]
```

Rules a client must handle:

- **Text chunks** arrive as `data: <chunk>`. Inside a chunk, literal newlines are encoded as `\n`
  and backslashes as `\\`; decode by reversing that before display.
- **`data: [USAGE:{...}]`** — a JSON token/cost payload (`input`, `output`, optional `finish`).
  Not user-visible text; parse and use for cost display.
- **`data: [TOOL_CALL:{...}]`** and **`data: [TOOL_RESULT:{...}]`** — emitted only on the agentic
  path when the model invokes an MCP tool. Each is a JSON blob the UI can render.
- **`data: [CANCELLED]`** — the stream was cancelled (see below); no `[DONE]` follows the cancel
  on some paths, so treat `[CANCELLED]` as terminal.
- **`data: [ERROR: <message>]`** — an error occurred mid-stream; a `data: [DONE]` follows.
- **`data: [DONE]`** — terminal marker. Stop reading.

**Cancelling an in-progress stream:** call `POST /api/cancel/:sessionId`. This sets a Redis flag
(`cancel:<session_id>`) that the running generator polls between chunks; it works across
containers/replicas. The current chunk finishes, then the stream emits `[CANCELLED]`.

---

### POST /api/chat

Streaming chat via the LaoZhang/OpenAI-compatible backend. Proxies to Python `POST /chat/stream`.

**Auth required:** Yes (JWT is forwarded to Python, which requires it)

**Request headers:**

| Header | Required | Value |
|---|---|---|
| `Authorization` | Yes | `Bearer <clerk-jwt>` |
| `Content-Type` | Yes | `application/json` |
| `X-LaoZhang-API-Key` | No | Per-request upstream key override; otherwise the tenant's stored key / server env is used |
| `X-DeepSeek-Route` | No | `deepseek` (default) or `laozhang`; only affects `deepseek-v4-pro` / `deepseek-r1` |

**Request body:**

```jsonc
{
  "sessionId": "string",        // client session id; normalised to a UUID server-side
  "message": "string",          // required, non-empty, ≤ 2,000,000 chars
  "model": "string",            // default "gemini-2.5-flash"; must be a key in the model list
  "system": "string",           // system prompt; default "You are a helpful assistant."
  "temperature": 0.9,           // number, clamped 0.0–2.0
  "max_tokens": 8192,           // integer, capped at 100000 then at the model ceiling
  "images": [                   // optional, vision-capable models only
    { "b64": "string", "mime": "image/png", "name": "foo.png" }
  ]
}
```

**Response:** `200 OK`, `Content-Type: text/event-stream`. The body is the SSE stream described
above. On success the backend persists the user turn + assistant reply to `chat_messages` and a
row to `usage_logs` after the stream completes.

**Error responses:**

| Status | Code | Meaning |
|---|---|---|
| 400 | `error` | Empty message, message too long (Python `ChatRequest` validators) |
| 401 | `error` | Missing/expired Clerk token |
| 422 | `detail` | Body failed FastAPI validation upstream |
| 503 | `detail` | `Database unavailable` (session/history load failed) |
| — | `[ERROR: …]` | Upstream model error mid-stream (delivered in the SSE body) |

**Notes:** SSE — client must stream, not buffer. The Node handler cancels the upstream reader if
the client disconnects (`req.on("close")`). `sessionId` (camelCase) on the wire maps to Python
`session_id`. The session row is created on first message via `get_or_create_session`, scoped to
the tenant.

---

### POST /api/chat/agentic

Same as `/api/chat` but enables MCP tool-calling (`use_tools: true`, default model
`claude-sonnet`). The model may emit `[TOOL_CALL]`/`[TOOL_RESULT]` frames as it searches the
indexed folder. Requires the MCP sidecar to be running (see §10); if unavailable, the backend
falls back to a normal (non-tool) stream.

**Auth required:** Yes · **Body:** same as `/api/chat` plus `mcpPaths` (comma-separated paths to
restrict file search). **Notes:** only models in the tool-capable set actually invoke tools; max
6 tool rounds per turn.

---

### POST /api/chat/once

Non-streaming single-shot completion (used by the "auto-pick" video feature). Proxies to Python
`POST /chat/once`.

**Auth required:** No

**Request body:**

```json
{
  "message": "string",
  "model": "gemini-2.5-flash",
  "system": "You are a helpful assistant.",
  "max_tokens": 512
}
```

**Response:**

```json
{ "text": "string" }
```

**Notes:** if the requested model returns empty text, the backend retries once with
`gemini-2.5-flash`. Non-streaming JSON, not SSE.

---

### POST /api/chat/google  and  POST /api/chat/google/once

Google-native chat served directly by Node via `@google/genai` (no Python hop). `/api/chat/google`
is SSE with the same frame format as above; `/api/chat/google/once` is non-streaming `{ "text" }`.

**Auth required:** No (but tenant/user are resolved if a token is present, for persistence)

**Request body (streaming):**

```jsonc
{
  "message": "string",
  "model": "gemini-2.5-flash",
  "system": "string",
  "history": [ { "role": "user", "content": "..." } ],
  "temperature": 1.0,
  "thinkingLevel": "",          // only applied for gemini-3* models
  "google_api_key": "",         // client key; falls back to server GEMINI_API_KEY
  "max_tokens": 8192,
  "images": [ { "b64": "string", "mime": "image/png" } ],
  "sessionId": ""               // optional; derived if omitted
}
```

**Error responses:**

| Status | Code | Meaning |
|---|---|---|
| 400 | `error` | No Gemini key available (neither client nor server env) |
| — | `[ERROR: …]` | Upstream Gemini error mid-stream |

**Notes:** SSE. Persists messages + usage (`provider: "gemini"`) after the stream closes if a
session/tenant context exists.

---

## 5. Session Endpoints

Sessions are persisted in `chat_sessions` (and messages in `chat_messages`), tenant-scoped.

### GET /api/history/:id

Return the message history for a session. Proxies to Python `GET /history/{session_id}`.

**Auth required:** Yes

**Response:**

```json
{ "history": [ { "role": "user", "content": "..." }, { "role": "assistant", "content": "..." } ] }
```

**Error responses:**

| Status | Code | Meaning |
|---|---|---|
| 404 | `detail` | Session not found |
| 503 | `detail` | Database unavailable |

**Notes:** the `:id` is normalised to a UUID server-side, so any stable string works as a session key.

---

### POST /api/save

Persist a session transcript to a server-side text file. Proxies to Python `POST /save`.

**Auth required:** Yes · **Body:** `{ "session_id": "string", "filename": "string (optional)" }`

**Response:** `{ "saved": "conversation_YYYYMMDD_HHMMSS.txt" }` · **Errors:** `404` session not
found, `503` DB unavailable.

---

### DELETE /api/session/:id

Delete a single session (and its messages, via `ON DELETE CASCADE`). Proxies to Python
`DELETE /session/{session_id}`.

**Auth required:** Yes · **Response:** `{ "status": "cleared" }` · **Errors:** `503` DB unavailable.

---

### DELETE /api/sessions  *(Python `DELETE /sessions`)*

Deletes **all** sessions for the authenticated tenant. Exposed on the Python backend; reach it via
the proxy if wired in nginx. **Auth required:** Yes · **Response:** `{ "status": "all sessions cleared" }`.

---

## 6. Job Endpoints

Phase 1 unifies async work in the `jobs` table (`job_type_enum`:
`oneshot_fix · batch_image · tts · imagen · veo · sora · narasi`). Live progress for
Node-run jobs (TTS/Imagen) is mirrored in Redis; narasi/one-shot-fix progress is mirrored in Redis
too. The video jobs (Veo/Sora) are provider tasks polled by `task_id` and are summarized at the
end of this section.

### Job status state machine

The persisted enum is **`job_status_enum`**:

```
queued → processing → running → done
                    ↘  cancelling → cancelled
                    ↘  error
```

| Status | Meaning |
|---|---|
| `queued` | Row created, not started |
| `processing` / `running` | Work in progress (Node live jobs report `running`; submit endpoints seed `processing`) |
| `cancelling` | Cancel requested; will stop after the current unit |
| `cancelled` | Stopped by user |
| `done` | Completed; `result_payload` populated |
| `error` | Failed; `error_message` populated |

> **Naming note:** the original spec described `pending → processing → completed / failed`. The
> live system uses the enum above. Map `pending→queued`, `completed→done`, `failed→error`. Node's
> in-memory live objects also use `done`/`error`/`cancelled`/`cancelling`.

---

### 6.1 One-shot narasi fix

Submit a long manuscript for a single-pass rule-compliance rewrite. Returns a `job_id`
immediately; poll for status, then fetch the result. Proxies to Python
`/narasi/oneshot-fix{,/status,/result}`.

#### POST /api/narasi/oneshot-fix  — create

**Auth required:** Yes

**Request headers:**

| Header | Required | Value |
|---|---|---|
| `Authorization` | Yes | `Bearer <clerk-jwt>` |
| `Content-Type` | Yes | `application/json` |
| `X-LaoZhang-API-Key` | No | Upstream key override |

**Request body:**

```json
{
  "model": "gemini-2.5-pro",
  "system": "string (required) — the rule/system prompt",
  "content": "string (required) — full manuscript",
  "file_name": "narasi",
  "temperature": 0
}
```

**Response:**

```json
{ "ok": true, "job_id": "uuid-string" }
```

**Error responses:**

| Status | Code | Meaning |
|---|---|---|
| 400 | `detail` | `content required` / `system required` / `DEEPSEEK_API_KEY is not set` |
| 401 | `error` | Not authenticated |

**Notes:** runs in the background (`asyncio.create_task`). The result is parsed into
`checklist_before`, `fixed_book`, and `checklist_after` using the
`---FIXED_BOOK_START---`/`---FIXED_BOOK_END---` delimiters. `job_id` here is the internal
`jobs.id` UUID.

#### GET /api/narasi/oneshot-fix/status/:jobId  — poll status

**Auth required:** Yes · **Response:**

```json
{ "ok": true, "status": "processing", "progress": "AI membaca seluruh manuskrip...", "error": null }
```

`status` is one of the `job_status_enum` values. `progress` is live text from Redis (falls back to
the DB `progress_message`). **Errors:** `404` job not found.

#### GET /api/narasi/oneshot-fix/result/:jobId  — get result

**Auth required:** Yes · **Response:**

```json
{
  "ok": true,
  "file_name": "narasi",
  "checklist_before": "markdown table",
  "fixed_book": "full fixed manuscript",
  "checklist_after": "markdown table"
}
```

**Errors:** `404` job not found; `400` `Job not done: <status>` if status ≠ `done`.

---

### 6.2 Narasi generation (submit → poll → stitch)

Generate a multi-chapter narration. Submit returns an **8-character** `external_job_id`; poll for
per-chapter progress; stitch the saved chapter files into final markdown.

#### POST /api/narasi/generate  — create

**Auth required:** Yes · Proxies to Python `POST /narasi/generate`.

**Request body:**

```jsonc
{
  "model": "gemini-2.5-flash",
  "topic": "string",
  "style": "storytelling",       // one of the supported style keys
  "language": "id",              // "id" | "en"
  "chapters": [                  // from the outline step
    { "id": "1", "title": "string", "description": "string", "words": 400 }
  ],
  "brief": "string (optional)",
  "outline": "string (optional)",
  "use_rag": false,              // pull Gutenberg passages if RAG is available
  "video_mode": false,           // emit VO [ANCHOR]/[BEAT] markers
  "pre_job_id": "string (optional)"
}
```

**Response:**

```json
{ "ok": true, "job_id": "8charid", "status": "started" }
```

**Notes:** the `jobs` row is created up front (`create_narasi_job`) so polling sees it
immediately; `external_job_id` (TEXT) stores the 8-char id while the PK stays a UUID. Generation
runs as a background task with a 1s delay between chapters; if chapter 1 comes back under 50 words
the job auto-cancels.

#### GET /api/narasi/status/:jobId  — poll progress

**Auth required:** No on the proxy, but resolves tenant if present · Proxies to Python
`GET /narasi/status/{job_id}`.

**Response (job found):**

```json
{
  "job_id": "8charid",
  "status": "running",
  "progress": "Menulis bab 3/8: <title>",
  "current": 3,
  "total": 8,
  "result": { "chapters": 8, "errors": [], "tmp_dir": "/app/data/narasi_temp/<id>" },
  "error": null,
  "found": true
}
```

**Response (not found yet):** `{ "job_id": "...", "status": "processing" | "unknown", "progress": "", "current": 0, "total": 0, "found": false }`.

**Notes:** merges live Redis progress (fast, per-chapter) with the authoritative `jobs` row. Drives
the per-chapter checkbox UI via `current`/`total`.

#### POST /api/narasi/cancel/:jobId  — cancel

**Auth required:** No · Sets a Node-side flag *and* proxies to Python `POST /narasi/cancel/{job_id}`,
which sets the Redis flag `cancel:narasi_<job_id>`. The job stops after the current chapter.
**Response:** `{ "ok": true, "status": "cancel_requested", "job_id": "..." }`.

#### POST /api/narasi/stitch/:jobId  — assemble final markdown

**Auth required:** No · Reads the per-chapter `.txt` files for the job and concatenates them.
**Body:** `{ "topic": "...", "style": "...", "language": "id" }`. **Response:**
`{ "ok": true, "markdown": "...", "total_words": 1234 }`. **Errors:** `404` job dir not found.

#### Supporting narasi routes

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /api/narasi/outline` | Yes | Generate/revise outline + a brief (`action: "brief"`). Returns `{ chapters[], outline_text }`. Captures the outline to `narasi_outlines` (MOAT). |
| `POST /api/narasi/review` | No | Non-streaming editorial review pass; returns `{ ok, text, finish_reason, usage }`. |
| `POST /api/narasi/save-edit/:jobId` | Yes | Persist a human edit as a `correction_pairs` row (MOAT training data). Returns `{ ok, quality_tier, edit_ratio }`. Non-fatal. |
| `POST /api/narasi/outline/google`, `POST /api/narasi/generate/google` | No | Google-native variants (Node `@google/genai`) with client-side anti-drift; outline returns pipe-delimited parse → `{ chapters[], outline_text }`. |

---

### 6.3 Batch image (create → poll → retrieve)

Node-run via `@google/genai` batch API. State lives in the `jobs` table (`batch_image`) with the
Google-specific record inside `result_payload`.

#### POST /api/submit  — create batch

**Auth required:** Yes

**Request body:**

```jsonc
{
  "settings": {
    "modelId": "gemini-3-pro-image-preview",
    "displayName": "image-generation-batch",
    "aspectRatio": "16:9",
    "imageSize": "1K"
  },
  "jobs": [ { "output": "image-1", "...": "per-image prompt fields" } ]
}
```

**Response:**

```json
{ "ok": true, "jobName": "batches/...", "record": { "id": "uuid", "jobName": "batches/...", "count": 3, "state": "JOB_STATE_PENDING", "mapping": [ { "key": "image-1", "output": "image-1" } ] } }
```

**Errors:** `400` `No GEMINI_API_KEY in .env` / `No jobs`; `500` upstream error.

**Notes:** the upstream Google batch lifecycle is tracked inside `result_payload.state`
(`JOB_STATE_PENDING` → `JOB_STATE_SUCCEEDED`/failed), distinct from the row's `job_status_enum`.

#### GET /api/status?name=<jobName>  — poll batch

**Auth required:** Yes · **Query:** `name` (the Google `batches/...` name, required).
**Response:** `{ "ok": true, "state": "JOB_STATE_RUNNING", "destFile": null }`. **Errors:** `400`
`name required` / `No API key`.

#### POST /api/retrieve  — download results

**Auth required:** Yes · **Body:** `{ "jobName": "batches/..." }`. On success downloads the result
JSONL, writes PNGs to the server image dir, and returns
`{ "ok": true, "state": "JOB_STATE_SUCCEEDED", "saved": 3, "failed": 0, "files": [ { "key": "image-1", "file": "image-1.png" } ] }`.
If not ready: `{ "ok": false, "state": "...", "message": "Not ready" }`. **Errors:** `400`
`jobName required`; `500` `No result file` / download failure.

#### GET /api/jobs  — list batch jobs

**Auth required:** Yes · Returns the batch records for the tenant (shape from `result_payload`,
plus `id`, `state`, `createdAt`).

#### GET /api/images  — list rendered files

**Auth required:** No · Returns `[{ "file": "x.png", "url": "/images/x.png" }]` from the static
output dir.

---

### 6.4 TTS (start → poll → files)

Node-run, sequential, with API-key rotation. Two providers: Google (`runTtsJob`) and LaoZhang
(`runLaozhangTtsJob`, OpenAI-compatible `/v1/audio/speech`). Live progress in Redis; terminal
state in the `jobs` table (`tts`).

#### POST /api/tts/start  — create

**Auth required:** Yes

**Request body:**

```jsonc
{
  "apiMode": "google",            // "google" | "laozhang"
  "apiKeys": ["key1", "key2"],    // rotated on 429/401/403; Google falls back to server env
  "model": "gemini-3.1-flash-tts-preview",
  "voice": "Enceladus",
  "speed": 1.0,                   // laozhang only
  "language": "auto",             // laozhang: filename suffix only (TTS auto-detects language)
  "audiobookMode": false,         // laozhang: chunk continuous prose at ~4000 chars
  "silenceSeconds": 0.5,          // prepend silence to each clip
  "audioProfile": "",             // google: style/voice-direction prompt
  "transcriptBody": "string",     // required, non-empty; split on blank lines into chunks
  "outputPrefix": "tts"
}
```

**Response:**

```json
{ "ok": true, "jobId": "uuid", "total": 12 }
```

**Errors:** `400` `No API keys`/`No LaoZhang API key`/`Empty transcript`.

**Notes:** one `.wav` per chunk (default = one per blank-line-separated paragraph; audiobook mode =
~4000-char chunks). Files are written to the static audio dir and exposed at `/audio/<file>`.

#### GET /api/tts/job/:id  — poll one job

**Auth required:** Yes · Returns the **live** Redis object while running (rich: `status`,
`progress`, `total`, `logs[]`, `files[]`), or the DB row once finished. **Errors:** `404` Not found.

```jsonc
{
  "jobId": "uuid",
  "status": "running",            // queued | running | cancelling | cancelled | done | error
  "progress": 5,
  "total": 12,
  "logs": ["[5/12] tts_05.wav", "✅ tts_05.wav"],
  "files": [ { "file": "tts_05.wav", "url": "/audio/tts_05.wav" } ]
}
```

#### POST /api/tts/cancel/:id  — cancel

**Auth required:** Yes · Sets the cancel flag; the live job moves to `cancelling`, then `cancelled`
(DB row → `error` with message `Cancelled by user`). **Response:** `{ "ok": true, "jobId": "...", "status": "cancelling" }`.
If the job is not running: `{ "ok": false, "reason": "Job not running", "status": "<status>" }`.
**Errors:** `404` Job not found.

#### GET /api/tts/jobs  /  GET /api/tts/files

List the tenant's TTS jobs (`requireAuth`) / list rendered `.wav` files (no auth).

---

### 6.5 Imagen (start → poll → files)

Node-run, sequential, via `@google/genai` `generateImages`. Same shape family as TTS jobs (`jobs`
type `imagen`).

#### POST /api/imagen/start  — create

**Auth required:** Yes

**Request body:**

```jsonc
{
  "apiKey": "",                   // falls back to server GEMINI_API_KEY
  "model": "imagen-4.0-generate-001",
  "prompts": ["prompt 1", "prompt 2"],   // required, non-empty
  "outputPrefix": "imagen",
  "aspectRatio": "16:9",
  "resolution": "1920x1080"       // images are resized to this WxH
}
```

**Response:** `{ "ok": true, "jobId": "uuid", "total": 2 }` · **Errors:** `400` `No API key` /
`No prompts`.

**Notes:** the "pause" models (`imagen-4.0*-generate-001`) pause 90s every 20 images to avoid rate
limits. Outputs are `.jpeg` exposed at `/imgs/<file>`.

#### GET /api/imagen/job/:id  — poll

**Auth required:** Yes · Live Redis object while running, DB row when finished (same field shape as
TTS). **Errors:** `404` Not found.

#### GET /api/imagen/jobs  /  GET /api/imagen/files

List the tenant's imagen jobs (`requireAuth`) / list rendered `.jpeg` files (no auth).

---

### 6.6 Video tasks — Veo & Sora (provider-polled)

These are provider tasks identified by a `task_id` (not the `jobs` table), proxied to Python which
forwards to LaoZhang's `/v1/videos`.

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /api/veo/submit` | No | Submit a Veo 3.1 text/image-to-video task → `{ task_id, status }` |
| `GET /api/veo/status/:id` | No | Poll → `{ task_id, status, progress, raw }` |
| `GET /api/veo/download/:id` | No | Download the MP4 (attachment) |
| `GET /api/veo/stream/:id` | No | Stream the MP4 inline (`video/mp4`); retries while `IN_PROGRESS` |
| `POST /api/sora/submit` | No | Submit a Sora 2 task → `{ task_id, status }` |
| `GET /api/sora/status/:id` | No | Poll → `{ task_id, status, progress, raw }` |
| `GET /api/sora/stream/:id` | No | Stream the MP4 inline |

**Notes:** the provider's own status values flow through in `status`/`raw`. The stream endpoints
cache the MP4 server-side after first fetch and may return `503` if the video isn't ready after
retries. An `X-Veo-API-Key` / `X-Sora-API-Key` header overrides the upstream key.

---

## 7. Config Endpoints  (Node Express)

Per-tenant configuration, stored via `db.js` and tenant-scoped.

### GET /api/config

Return the tenant config object.

**Auth required:** Yes · **Response:** `200 OK` with the tenant config object (shape **defined in
`db.js`**). **Errors:** `500` `{ "error": "<message>" }`.

### POST /api/config

Upsert config fields. The handler passes `req.body` straight to `setConfig(tenantId, body)`, so it
accepts a partial object and merges/upserts the provided fields.

**Auth required:** Yes · **Body:** an object of config fields to set. **Response:** `{ "ok": true }`.
**Errors:** `500`.

> **Supported fields (defined in `db.js`):** the exact persisted field list is implemented in
> `db.js` (`getConfig`/`setConfig`), which was not part of this export. From usage across the
> codebase, config holds per-tenant settings such as upstream API keys/preferences and default
> model choices, but treat the authoritative list as whatever `getConfig` returns. Re-document this
> section against `db.js` to enumerate exact fields.

---

## 8. TTS Profile Endpoints  (Node Express)

Persistent, server-side TTS voice/style profiles, tenant-scoped. Backed by `db.js`
(`getTtsProfiles` / `saveTtsProfiles` / `deleteTtsProfile`).

### GET /api/tts/profiles

**Auth required:** Yes · **Response:** an **array** of profile objects for the tenant. **Errors:** `500`.

### POST /api/tts/profiles

Replace/save the tenant's profiles. The handler expects the body to be an **array** (`Array.isArray(req.body)`);
a non-array body is treated as an empty list.

**Auth required:** Yes · **Body:** `[ { …profile… }, … ]` · **Response:** `{ "ok": true, "count": <n> }`.
**Errors:** `500`.

### DELETE /api/tts/profiles/:id

Delete one profile by id, then return the remaining count.

**Auth required:** Yes · **Response:** `{ "ok": true, "count": <remaining> }` · **Errors:** `500`.

> **Profile object shape (defined in `db.js`):** the persisted columns are implemented in `db.js`
> and were not part of this export. On the wire the API accepts and returns an array of profile
> objects; the TTS runner consumes profile-adjacent values like `voice`, `model`, `speed`,
> `language`, `audioProfile`, and `silenceSeconds` (see §6.4). Re-document the exact stored fields
> against `db.js`.

---

## 9. Model List Endpoint

### GET /api/models

Return the available chat model aliases. Proxies to Python `GET /models`.

**Auth required:** No

**Response (actual Phase 1 shape):**

```json
{ "models": ["gemini-2.5-flash", "deepseek-v3", "gpt-4o-mini", "gemini-2.5-pro", "claude-sonnet", "..."] }
```

**Notes / discrepancy:** Phase 1 returns a flat list of **alias strings** under a `models` key — it
does **not** return objects of the form `{ id, name, provider, context_window, cost_tier }`. The
provider, output ceiling, and cost tier exist server-side (the `MODELS`, `MODEL_MAX_TOKENS`, and
cost tables in `laozhang_api.py`) but are not exposed by this endpoint in Phase 1. Related Python
endpoints: `GET /image-models` (image model aliases) and `GET /mcp/tool-capable-models`
(tool-capable subset). Promoting `/api/models` to the richer object shape is a Phase 2 change.

---

## 10. MCP File Search  (optional sidecar)

Available only when the MCP file-search sidecar is running — start it with the profile:

```
docker compose --profile mcp up
```

Node proxies to the sidecar at `MCP_API_URL` (`:8001`). If the sidecar is down, these return
`{ "available": false, ... }` instead of an error.

### GET /api/mcp/search?q=<query>

**Auth required:** No · **Query:** `q` (the search string). **Response:** the sidecar's search
result JSON. **Notes:** only `q` is forwarded in Phase 1 — a `limit` parameter is **not** passed
through by this route. For ranked semantic search with a cap, use
`GET /api/mcp/search_semantic?q=<query>&top_k=<n>` (default `top_k=25`).

### Other MCP routes

| Method & path | Purpose |
|---|---|
| `GET /api/mcp/status` | `{ available: true, ... }` health/info, or `{ available: false }` |
| `GET /api/mcp/files` | List indexed files |
| `GET /api/mcp/context?q=&paths=` | Hybrid search → context block (used by chat tool-calling) |
| `GET /api/mcp/file?path=` | Read one indexed file |
| `POST /api/mcp/reindex` | Re-index the folder |
| `POST /api/mcp/set-folder` | Hot-swap the indexed folder (no restart) |
| `POST /api/mcp/srt/polish` | Long-running SRT polish (up to 30-min timeout) |
| `GET /api/mcp/search_semantic?q=&top_k=` | Vector search with a result cap |
| `GET /api/mcp/mode` | Current search mode |

---

## 11. Webhook Endpoint  (internal — not for frontend use)

### POST /webhooks/clerk

Receives Clerk user/organization lifecycle events. **Mounted before `clerkMiddleware`/`requireAuth`
and before the JSON body parser**, using `express.raw()` so the raw bytes can be signature-verified.

**Auth required:** No JWT — **protected by Svix signature instead.**

**Request headers:**

| Header | Required | Value |
|---|---|---|
| `svix-id` | Yes | Svix message id |
| `svix-timestamp` | Yes | Svix timestamp |
| `svix-signature` | Yes | Svix HMAC signature (verified against `CLERK_WEBHOOK_SECRET`) |
| `Content-Type` | Yes | `application/json` (consumed as raw bytes) |

**Handled event types:** `user.created` (provisions tenant + user + free subscription via the
`provision_tenant` SQL function), `user.updated` (updates email), `user.deleted` (deactivates the
user), `organization.created` (provisions a tenant for the org). Unknown types are logged and
acknowledged.

**Response:**

```json
{ "ok": true, "type": "user.created" }
```

**Error responses:**

| Status | Code | Meaning |
|---|---|---|
| 400 | `error` | `Invalid webhook signature` (Svix verification failed) |
| 500 | `error` | `Webhook secret not configured` (`CLERK_WEBHOOK_SECRET` missing) or `Database error` |

**Notes:** never call this from the frontend. Provisioning is idempotent and runs through a
`SECURITY DEFINER` function so it can write across tenants safely (RLS-exempt by design).

---

## Known Limitations and Phase 2 Changes

The following behaviours are **Phase 1 only** and will change. Build clients defensively.

1. **Background tasks → real queue.** Narasi, one-shot fix, batch/TTS/Imagen all run as in-process
   `asyncio` tasks / sequential Node loops with progress mirrored in Redis. Phase 2 moves to a
   durable job queue with workers. Expect submission to return **`201`** with a job resource, and
   status semantics to formalize around the `job_status_enum`.

2. **Local files → object storage (S3/R2 signed URLs).** Phase 1 writes outputs to local static
   dirs and serves them at `/images`, `/audio`, `/imgs`, and proxies MP4 bytes through the API.
   The `assets` table already models `bucket`, `s3_key`, `signed_url`, and
   `signed_url_expires_at`. Phase 2 returns **time-limited signed URLs** to object storage instead
   of local paths; clients should stop assuming stable local URLs.

3. **Token refresh.** Phase 1 has no server-side refresh; clients must re-call
   `Clerk.session.getToken()` and retry on `401`. Phase 3 (`@clerk/nextjs`) handles refresh
   automatically.

4. **RLS enforcement role.** Phase 1 runs with an elevated DB role and relies on
   `set_current_tenant_id`/policies for isolation, with the strict app-role switch deferred. Phase
   5 switches to a least-privilege application role; tenant isolation behaviour for clients does
   not change.

5. **`/api/models` shape.** Returns a flat list of alias strings, not
   `{ id, name, provider, context_window, cost_tier }` objects. Phase 2 will expose the richer
   metadata that already exists server-side.

6. **MCP `limit` parameter.** `GET /api/mcp/search` forwards only `q`; there is no `limit`
   passthrough. Use `/api/mcp/search_semantic` with `top_k` for a capped, ranked search until this
   is unified.

7. **Error contract gaps.** `201`, `409`, and a strict `429` quota contract are not consistently
   produced in Phase 1 (provider `429`s pass through; submissions return `200`). Phase 2 formalizes
   these. Mid-stream chat errors are delivered as `data: [ERROR: …]` frames, not HTTP statuses —
   handle both.

8. **Config & TTS-profile schemas live in `db.js`.** The exact persisted fields (§7, §8) are not
   captured here because `db.js` was not exported. Confirm field lists against `db.js` before
   relying on specific keys.

9. **Mixed auth on proxied routes.** Some proxy routes (e.g. `/api/narasi/status`,
   `/api/narasi/cancel`, `/api/narasi/review`, video submit/stream, `/api/chat/once`,
   `/api/chat/google`) do not enforce `requireAuth` at the Node layer in Phase 1, even though the
   data they touch is tenant-scoped where a token is present. Phase 2 should apply `requireAuth`
   uniformly; do not depend on these being publicly callable long-term.
