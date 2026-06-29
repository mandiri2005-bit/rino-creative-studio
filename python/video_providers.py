"""
video_providers.py — multi-provider VIDEO backend with per-model failover.

Mirror of image_providers.py for the Wimba Video tools (the /video-tools/* namespace), built
against the BUILD CONTRACT (9 ops: text_to_video, image_to_video, modify_video→video_edit,
reframe_video, upscale_video, caption_video [native], lip_sync, motion_control→image_to_video,
paparazzi_moment→image_to_video).

Registry: video_registry.json beside this module (model -> picker meta + per-second COGS + ordered
chain [cheapest aggregator -> 2nd -> first-party source]). dispatch() walks the chain: each provider
gets 1 call + 1 retry; on failure it advances to the next. Every video provider is ASYNC submit→poll
→fetch (none are synchronous). Output MP4 bytes are re-hosted to R2 (provider URLs expire); dispatch()
returns the bytes PLUS the stable R2 object KEY — storage.aupload_bytes returns a KEY, not a URL, so
the caller signs it on read (never fetch the key as a URL). API keys = env vars (see registry._env);
a missing/empty key makes the adapter raise immediately -> auto-failover to the next step (and finally
to the caller's legacy LaoZhang/Vertex Veo/Sora tail, which has no adapter here).

Normalized op contract — every adapter returns (bytes, mime); dispatch() re-hosts and returns
    {"ref": <R2 key | data: URI>, "data": <bytes>, "mime": str, "provider", "cost_usd", ...}
where op ∈ text_to_video|image_to_video|reference_to_video|video_edit (prompt-style, model chain) or
reframe_video|upscale_video|lip_sync (tool ops, op-chain; caption_video is backend-native, NOT here)
and params carries: prompt, ref_images[{url|b64}] (i2v/ref), ref_video{url|b64}, audio{url|b64}
(lip_sync), aspect, seconds, resolution, motion_strength, camera.

Pricing is PER-SECOND (video bills per-second, unlike image's per-image): credits_for(feature,
model_id, seconds) multiplies a rounded per-second credit by the duration; see credits_for().

NOTE: structurally mirrors the working-tree image_providers.py (2026-06-29). Video adapter endpoints
carry VERIFY comments where the exact provider video path was not confidently verifiable — confirm
each once its API key is live in Railway.
"""
from __future__ import annotations
import os, json, base64, asyncio, logging
from pathlib import Path
from typing import Optional
import httpx

try:
    import storage  # aupload_bytes(key, data, content_type) -> KEY (not a URL; sign on read)
except Exception:  # pragma: no cover
    storage = None

log = logging.getLogger("video_providers")


def _load_registry() -> dict:
    """Locate video_registry.json robustly. It lives NEXT TO this module (python/video_registry.json) so
    it is inside the Python service's Docker build context (`COPY *.py` + an explicit COPY); the repo-root
    config/ copy is NOT in that context. Falls back to ../config for any legacy/dev layout + an env override."""
    here = Path(__file__).resolve().parent
    for c in (os.getenv("VIDEO_REGISTRY_PATH"),
              str(here / "video_registry.json"),                      # bundled beside this module (prod image + repo)
              str(here.parent / "config" / "video_registry.json")):   # legacy: config/ at repo root
        if c and Path(c).exists():
            return json.loads(Path(c).read_text())
    raise FileNotFoundError("video_registry.json not found beside video_providers.py or in ../config/")


_REG = _load_registry()
_MODELS = {m["id"]: m for m in _REG["models"]}
_OP_CHAINS = _REG.get("op_chains", {})
# Tool ops (reframe_video/upscale_video/lip_sync/caption_video) are priced + dispatched model-INDEPENDENTLY
# off their op-chain, never a model chain (mirror of image's _OP_TOOL_FEATURES). caption_video has no
# provider chain (backend-native whisper+ffmpeg) but is still a tool feature so dispatch won't run a
# model chain for it. Built from the registry so it can't drift from the JSON.
_OP_TOOL_FEATURES = set(_REG.get("_op_tool_features", []))

# Inner provider poll-loop bound. MUST stay BELOW the caller's overall video dispatch deadline so a
# slow-but-alive provider trips THIS (a ProviderError → graceful failover/legacy fallback) before the
# outer asyncio.wait_for fires its hard TimeoutError (which skips the fallback). Video renders take
# MINUTES (not the seconds an image takes), so the bound is far larger than image's 240s. Both
# env-tunable. The poll is intended to live in a detached worker, not a request handler.
_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)
_POLL_EVERY = float(os.getenv("VIDEO_POLL_EVERY", "6.0"))
_POLL_MAX = float(os.getenv("VIDEO_POLL_MAX", "900.0"))


class ProviderError(RuntimeError):
    """Raised by an adapter when a provider call fails (→ failover to next)."""


def _key(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise ProviderError(f"missing env {name}")
    return v


# ── SSRF guard + size cap at the ACTUAL server-side fetch. We resolve the host ONCE, reject unless
# EVERY resolved address is public, and PIN that vetted IP into the request — httpx then connects to the
# IP literal with NO second DNS lookup, so the validated address IS the connected address (closes the
# DNS-rebinding TOCTOU: a low-TTL attacker domain can't flip public→internal between check and fetch).
# The pinned user-ref stream also passes follow_redirects=False per-request so a 30x can't escape the
# vetted IP (the shared dispatch client follows redirects for provider result/poll traffic). ──
# Video files are tens–hundreds of MB (not the ~36MB images cap) → bump via VIDEO_MAX_BYTES (default 300MB).
_MAX_FETCH_BYTES = int(os.getenv("VIDEO_MAX_BYTES", str(300 * 1024 * 1024)))   # generous for video results


def _ip_is_public(ip) -> bool:
    """True iff `ip` is a routable public address (rejects private/loopback/link-local/reserved/
    multicast/unspecified + CGNAT 100.64/10 = Railway-internal + 169.254 cloud-metadata)."""
    import ipaddress as _ipx
    m = getattr(ip, "ipv4_mapped", None)          # ::ffff:a.b.c.d → re-classify on the embedded v4, else
    if m is not None:                              # an internal target wrapped as v6 walks past the v4-gated
        ip = m                                     # CGNAT check (all is_* flags are False on the v6 form).
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified):
        return False
    if ip.version == 6 and ip in _ipx.ip_network("64:ff9b::/96"):   # NAT64 embeds a v4 target → reject
        return False
    if ip.version == 4 and _ipx.ip_address("100.64.0.0") <= ip <= _ipx.ip_address("100.127.255.255"):
        return False
    return True


def _resolve_pinned(url: str):
    """Resolve url's host ONCE, reject unless EVERY resolved address is public, then PIN the first vetted
    IP into the URL. Returns (pinned_url, host_header, sni_host): the caller connects to the IP literal
    (no re-resolution at fetch → DNS-rebinding TOCTOU closed) while the Host header + TLS SNI carry the
    original hostname (correct vhost routing + cert verified against the name, not the IP). Raises
    ProviderError if the scheme/host is bad or ANY address is non-public. Mirrors
    laozhang_api._is_public_http_url (kept per-module; sharing would invert the import direction)."""
    import ipaddress as _ipx, socket as _sock
    from urllib.parse import urlparse as _up, urlunparse as _unp
    u = _up(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ProviderError("ref video URL not allowed")
    port = u.port or (443 if u.scheme == "https" else 80)
    pinned = None
    for info in _sock.getaddrinfo(u.hostname, port, proto=_sock.IPPROTO_TCP):
        ip = _ipx.ip_address(info[4][0])
        if not _ip_is_public(ip):
            raise ProviderError("ref video URL resolves to a non-public address")
        if pinned is None:
            pinned = ip
    if pinned is None:
        raise ProviderError("ref video URL did not resolve")
    hostlit = f"[{pinned}]" if pinned.version == 6 else str(pinned)
    netloc = f"{hostlit}:{u.port}" if u.port else hostlit
    pinned_url = _unp((u.scheme, netloc, u.path or "/", u.params, u.query, u.fragment))
    return pinned_url, u.netloc, u.hostname


async def _drain_capped(r) -> bytes:
    """Read an OPEN streaming httpx response body under a hard running-total cap (OOM guard): reject early
    on an honest oversized Content-Length, else accumulate aiter_bytes() and abort the instant the total
    crosses the cap — so a chunked / no-Content-Length oversized body can never be fully buffered into
    memory. Shared by every server-side fetch that re-hosts provider bytes (GET results + POST inline)."""
    if int(r.headers.get("content-length") or 0) > _MAX_FETCH_BYTES:
        raise ProviderError("video too large")   # honest oversized declaration → reject before download
    buf, total = bytearray(), 0
    async for chunk in r.aiter_bytes():
        total += len(chunk)
        if total > _MAX_FETCH_BYTES:
            raise ProviderError("video too large")   # chunked / lying Content-Length → abort mid-stream
        buf += chunk
    return bytes(buf)


async def _capped_get(client, url: str, *, validate_public: bool = False):
    """STREAM GET → (bytes, mime) under _drain_capped's running-total cap. validate_public PINS a vetted
    public IP for USER-supplied URLs (resolve-once-then-connect-by-IP = DNS-rebinding-safe; the client
    must be follow_redirects=False so a 30x can't escape the pin)."""
    if validate_public:
        purl, host_hdr, sni = _resolve_pinned(url)   # resolve + vet every IP + pin (raises if blocked)
        # follow_redirects=False HERE ONLY: a 30x on a user ref would escape the pinned/vetted IP. The
        # shared dispatch client follows redirects for provider traffic; we override per-request.
        async with client.stream("GET", purl, headers={"Host": host_hdr},
                                 extensions={"sni_hostname": sni}, follow_redirects=False) as r:
            r.raise_for_status()
            return await _drain_capped(r), r.headers.get("content-type", "video/mp4")
    async with client.stream("GET", url) as r:
        r.raise_for_status()
        return await _drain_capped(r), r.headers.get("content-type", "video/mp4")


# ── input helpers — providers disagree on URL vs base64 vs multipart file ──────
async def _img_bytes(client: httpx.AsyncClient, img: dict) -> tuple[bytes, str]:
    """Fetch an input ref {url|b64} → (bytes, mime). USER url → SSRF-validated + size-capped.
    Used for both image refs (i2v/ref) and video refs (video_edit/reframe/lip_sync)."""
    if img.get("b64"):
        return base64.b64decode(img["b64"]), img.get("mime", "image/png")
    return await _capped_get(client, img["url"], validate_public=True)


async def _img_b64(client, img: dict) -> str:
    b, _ = await _img_bytes(client, img); return base64.b64encode(b).decode()


def _img_url(img: dict) -> str:
    if img.get("url"):
        return img["url"]
    return f"data:{img.get('mime','image/png')};base64,{img['b64']}"


async def _rehost(data: bytes, mime: str, op_id: str) -> str:
    ext = {"video/mp4": "mp4", "video/webm": "webm",
           "image/svg+xml": "svg", "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(mime, "mp4")
    # Gate on is_configured(), NOT just `storage is None` (None only on import failure). If the module
    # imported but R2 env is unset/misconfigured, aupload_bytes would raise → _try treats it as transient
    # → whole provider chain fails over → a WORKING provider is turned into an outage (and the tool ops,
    # which have no legacy tail, hard-502). Fall back to an inline data: URI instead. Mirrors _persist_asset.
    if storage is None or not storage.is_configured():
        return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode())
    return await storage.aupload_bytes(f"vid/{op_id}.{ext}", data, mime)


async def _fetch(client, url: str) -> tuple[bytes, str]:
    return await _capped_get(client, url)   # provider result URL — size-capped (OOM guard)


async def _poll(client, url: str, headers: dict, *, done, result, err=None,
                interval=_POLL_EVERY, max_s=_POLL_MAX):
    # WALL-CLOCK deadline (not a sleep-only budget): a slow poll GET counts against max_s too, so a
    # stuck/slow provider can't hang the request for hours (the old `waited += interval` ignored the
    # GET latency). Each GET also gets a tight explicit timeout.
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max_s
    while loop.time() < deadline:
        r = await client.get(url, headers=headers, timeout=15.0); r.raise_for_status()
        j = r.json()
        if err and err(j):
            raise ProviderError(f"provider job failed: {str(j)[:200]}")
        if done(j):
            return result(j)
        await asyncio.sleep(interval)
    raise ProviderError("poll timeout")


def _aspect(params) -> str:
    return params.get("aspect") or "16:9"


def _seconds(params) -> int:
    try:
        s = int(params.get("seconds") or 5)
    except (TypeError, ValueError):
        s = 5
    return s if s > 0 else 5


# ══════════════════════════ adapters ══════════════════════════
# Each is async submit→_poll→_fetch and returns (bytes, mime); dispatch() re-hosts.

async def _atlascloud_video(client, op, slug, params):
    """AtlasCloud video generation — mirrors the image _atlascloud submit/poll shape. VERIFY endpoint:
    the image path is POST /model/generateImage + poll /model/prediction/{id}; the video path here uses
    /model/generateVideo with the same prediction-poll contract. If AtlasCloud names the video endpoint
    differently, swap the path but keep this submit→prediction-poll→fetch contract."""
    h = {"Authorization": f"Bearer {_key('ATLASCLOUD_API_KEY')}", "Content-Type": "application/json"}
    base = "https://api.atlascloud.ai/api/v1"
    body = {"model": slug, "enable_sync_mode": False,
            "duration": _seconds(params), "aspect_ratio": _aspect(params)}
    if op == "text_to_video":
        body["prompt"] = params.get("prompt") or ""
    elif op in ("image_to_video", "reference_to_video"):
        body["prompt"] = params.get("prompt") or ""
        refs = params.get("ref_images") or []
        if refs:
            body["image"] = _img_url(refs[0])           # i2v/ref seed frame (URL or data: URI)
    elif op == "video_edit":
        body["prompt"] = params.get("prompt") or ""
        rv = params.get("ref_video")
        if rv:
            body["video"] = _img_url(rv)
    else:
        raise ProviderError(f"atlascloud_video: op {op} unsupported")
    if params.get("resolution"):
        body["resolution"] = params["resolution"]
    r = await client.post(f"{base}/model/generateVideo", headers=h, json=body); r.raise_for_status()  # VERIFY endpoint
    d = r.json().get("data", r.json())
    if d.get("outputs"):                                 # rare inline-result fast path
        return await _fetch(client, d["outputs"][0])
    pid = d["id"]
    out = await _poll(client, f"{base}/model/prediction/{pid}", h,
                      done=lambda j: (j.get("data") or j).get("status") == "completed",
                      err=lambda j: (j.get("data") or j).get("status") == "failed",
                      result=lambda j: (j.get("data") or j)["outputs"][0])
    return await _fetch(client, out)


async def _fal_video(client, op, slug, params):
    """fal.ai queue video — mirrors the image _fal submit/poll shape: POST queue.fal.run/{slug} →
    poll status_url (COMPLETED/FAILED) → GET response_url → output video.url. Per-op input fields:
    image_url (i2v/ref seed), video_url (video_edit/reframe/lip_sync source), audio_url (lip_sync)."""
    h = {"Authorization": f"Key {_key('FAL_API_KEY')}", "Content-Type": "application/json"}
    body = {}
    if params.get("prompt"):
        body["prompt"] = params["prompt"]
    if op in ("image_to_video", "reference_to_video", "motion_control", "paparazzi_moment"):
        refs = params.get("ref_images") or []
        if refs:
            body["image_url"] = _img_url(refs[0])
    if op in ("video_edit", "reframe_video", "lip_sync"):
        rv = params.get("ref_video")
        if rv:
            body["video_url"] = _img_url(rv)
    if op == "lip_sync":
        aud = params.get("audio")
        if aud:
            body["audio_url"] = _img_url(aud)
    body["aspect_ratio"] = _aspect(params)
    body["duration"] = _seconds(params)
    if params.get("resolution"):
        body["resolution"] = params["resolution"]
    r = await client.post(f"https://queue.fal.run/{slug}", headers=h, json=body); r.raise_for_status()
    j = r.json(); status_url, resp_url = j["status_url"], j["response_url"]
    await _poll(client, status_url, h, done=lambda s: s.get("status") == "COMPLETED",
                err=lambda s: s.get("status") in ("FAILED", "ERROR"), result=lambda s: True)
    rr = await client.get(resp_url, headers=h); rr.raise_for_status(); out = rr.json()
    vid = out.get("video") or (out.get("output") or {}).get("video")
    if not (isinstance(vid, dict) and vid.get("url")):
        raise ProviderError(f"fal_video: no video url in response: {str(out)[:200]}")
    return await _fetch(client, vid["url"])


async def _aiml_video(client, op, slug, params):
    """AI/ML API video — submit returns a generation id, then poll by id. VERIFY endpoint: AIML video is
    a 2-step generate→poll. The submit path here is POST /v2/generate/video/generation and the poll is
    GET /v2/generate/video/generation?generation_id=… ; if AIML's live video path differs, swap the paths
    but keep this submit→poll-by-id→fetch contract. Raises ProviderError on an unexpected shape so the
    step fails over rather than charging for a 404/garbage response."""
    h = {"Authorization": f"Bearer {_key('AIMLAPI_API_KEY')}", "Content-Type": "application/json"}
    base = "https://api.aimlapi.com/v2/generate/video"   # VERIFY endpoint
    body = {"model": slug, "prompt": params.get("prompt") or ""}
    if op in ("image_to_video", "reference_to_video", "motion_control", "paparazzi_moment"):
        refs = params.get("ref_images") or []
        if refs:
            body["image_url"] = _img_url(refs[0])
    if op in ("video_edit", "reframe_video", "lip_sync"):
        rv = params.get("ref_video")
        if rv:
            body["video_url"] = _img_url(rv)
    if op == "lip_sync":
        aud = params.get("audio")
        if aud:
            body["audio_url"] = _img_url(aud)
    body["duration"] = _seconds(params)
    body["aspect_ratio"] = _aspect(params)
    if params.get("resolution"):
        body["resolution"] = params["resolution"]
    r = await client.post(f"{base}/generation", headers=h, json=body); r.raise_for_status()  # VERIFY endpoint
    j = r.json()
    gid = (j.get("id") or j.get("generation_id")
           or (j.get("data") or {}).get("id") or (j.get("data") or {}).get("generation_id"))
    if not gid:
        raise ProviderError(f"aiml_video: no generation id in submit response: {str(j)[:200]}")

    def _status(j):
        return ((j.get("status") or (j.get("data") or {}).get("status")) or "").lower()

    def _result(j):
        d = j.get("data") or j
        v = d.get("video") or d.get("output") or d.get("video_url") or d.get("url")
        if isinstance(v, dict):
            v = v.get("url")
        if isinstance(v, list) and v:
            v = v[0].get("url") if isinstance(v[0], dict) else v[0]
        if not v:
            raise ProviderError(f"aiml_video: no video url in completed response: {str(j)[:200]}")
        return v

    out = await _poll(client, f"{base}/generation?generation_id={gid}", h,   # VERIFY endpoint
                      done=lambda j: _status(j) in ("completed", "succeeded", "success", "done"),
                      err=lambda j: _status(j) in ("failed", "error", "cancelled", "canceled"),
                      result=_result)
    return await _fetch(client, out)


_ADAPTERS = {
    "atlascloud": _atlascloud_video, "fal": _fal_video, "aiml": _aiml_video,
    # laozhang + vertex are routed through the EXISTING Veo/Sora path (handled by the caller as the
    # guaranteed legacy tail of every chain) — they have NO adapter here, so a chain step naming them
    # raises `no adapter for '...'` → that step fails over → caller's legacy tail. See dispatch().
}


async def _try(client, provider, op, slug, params, op_id):
    """One provider, 1 call + 1 retry. Returns (ref_key, bytes, mime) or raises ProviderError.
    ref_key = the R2 object key from _rehost (or a data: URI when storage is unset) — the rendered
    bytes are threaded back alongside it so the caller never has to re-fetch the key as a URL."""
    fn = _ADAPTERS.get(provider)
    if fn is None:
        raise ProviderError(f"no adapter for '{provider}' (use legacy Veo/Sora tail)")
    last = None
    for attempt in (1, 2):
        try:
            data, mime = await fn(client, op, slug, params)
            ref = await _rehost(data, mime, f"{op_id}-{provider}")  # R2 KEY (or data: URI if no storage)
            return ref, data, mime
        except ProviderError:
            raise  # config/auth error — don't retry, failover now
        except Exception as e:  # transient (network/5xx) — retry once
            last = e; log.warning("vid %s/%s attempt %d failed: %s", provider, op, attempt, e)
            await asyncio.sleep(0.4)
    raise ProviderError(f"{provider} failed after retry: {last}")


def _slug_for(step: dict, op: str) -> Optional[str]:
    # Each op is DISTINCT — a step must carry an EXPLICIT slug for THAT op key (or a generic "slug"),
    # else it yields None → dispatch skips it → fails over (→ 502+refund if nothing in the chain can do
    # the op) rather than silently degrading to a DIFFERENT op and charging a WRONG video. Generalized
    # from the image module's reframe special-case: the protected prompt ops below (text/image/reference
    # to-video + video_edit) NEVER fall back to another op's slug — only an explicit per-op key or the
    # generic "slug" resolves. op_chains tool steps carry an explicit "slug" so they still resolve.
    _PROTECTED = {"text_to_video", "image_to_video", "reference_to_video", "video_edit",
                  "reframe_video", "upscale_video", "lip_sync", "caption_video"}
    if op in _PROTECTED:
        return step.get(op) or step.get("slug")
    return step.get(op) or step.get("slug")


async def dispatch(feature: str, model_id: str, params: dict, op_id: str) -> dict:
    """Run `feature` for `model_id` through its failover chain. Returns
    {"ref", "data", "mime", "provider", "model", "feature", "cost_usd", "step_index"} — `data` is
    the rendered video bytes (threaded back so the caller never re-fetches `ref` as a URL — `ref`
    is a bare R2 object key, not a signed URL); cost_usd = the WINNING provider's real upstream
    per-second cost (for accurate COGS/margin in usage_logs; multiply by seconds at the caller for
    the absolute COGS). Raises if the whole chain fails (caller then falls back to the legacy
    Veo/Sora LaoZhang/Vertex path)."""
    model = _MODELS.get(model_id)
    cogs = (model or {}).get("cogs_usd")
    official = (model or {}).get("official_usd") or cogs
    # Tool ops (reframe/upscale/lip_sync) are PRICED off the op-chain (price_basis→_op_chain_basis), so they
    # MUST also dispatch + book COGS off the op-chain — never a model chain. Else a client posting a model id
    # to a tool op runs the model chain and books the model COGS, undercutting the margin floor. Prompt ops
    # (text/image/reference-to-video + video_edit) use the selected model's chain (a step with no slug for
    # the op then fails over / 502+refunds via _slug_for, never charging a wrong video).
    model_chain = None if feature in _OP_TOOL_FEATURES else \
        (model["chain"] if model and feature in model.get("features", []) else None)
    chain = model_chain or _OP_CHAINS.get(feature)
    if not chain:
        raise ProviderError(f"no chain for feature={feature} model={model_id}")
    errors = []
    # Provider traffic (POST/poll/result-fetch) FOLLOWS redirects — signed result URLs legitimately 30x to
    # object storage, and blanket redirects-off turned every such 3xx into a spurious failover / paid-but-502.
    # SSRF on the only user-controlled fetch (ref URLs via _capped_get(validate_public=True)) is closed by the
    # IP-PIN itself (_resolve_pinned vets every resolved IP + connects to the literal) PLUS a per-request
    # follow_redirects=False on that one pinned stream — so a user ref can't 30x-escape the vetted IP.
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        for idx, step in enumerate(chain):
            prov = step["provider"]; slug = _slug_for(step, feature)
            if not slug:
                continue
            try:
                ref, data, mime = await _try(client, prov, feature, slug, params, op_id)
                # COGS of the provider that actually served: an op-chain step carries its own
                # cogs_usd; otherwise the cheapest aggregator (chain[0]) costs the model's cogs_usd,
                # and any failover to a pricier source books the conservative first-party price.
                cost = step.get("cogs_usd")
                if cost is None:
                    cost = cogs if idx == 0 else official
                return {"ref": ref, "data": data, "mime": mime, "provider": prov,
                        "model": model_id, "feature": feature, "cost_usd": cost, "step_index": idx}
            except ProviderError as e:
                errors.append(f"{prov}: {e}"); log.info("failover %s: %s", prov, e); continue
    raise ProviderError("all providers failed → " + " | ".join(errors))


# convenience for the banner / picker
def model_catalog() -> list:
    """Picker metadata (no secrets) for the studio model dropdown."""
    return [{"id": m["id"], "label": m["label"], "sublabel": m.get("sublabel", ""),
             "icon": m.get("icon", ""), "features": m.get("features", []),
             "transparent": m.get("transparent", False),
             "official_usd": m.get("official_usd"), "cogs_usd": m.get("cogs_usd")}
            for m in _REG["models"]]


def _op_chain_basis(feature: str):
    """(worst_official, worst_cogs, sell_markup) across ALL steps of a tool's op-chain — so the SELL price
    covers the MOST EXPENSIVE provider we might fail over to (never sell off step[0] alone, which would be
    negative-margin the moment step[0] is down and we fall to a pricier source). Every op_chain step
    carries cogs_usd/official_usd; markup = the first step that declares one. All prices are PER-SECOND."""
    steps = _OP_CHAINS.get(feature) or []
    offs, cogs, markup = [], [], None
    for s in steps:
        o = s.get("official_usd") or s.get("cogs_usd")
        if o:
            offs.append(o)
        if s.get("cogs_usd"):
            cogs.append(s["cogs_usd"])
        if s.get("sell_markup") and markup is None:
            markup = s["sell_markup"]
    return (max(offs) if offs else None), (max(cogs) if cogs else None), markup


def price_basis(feature: str, model_id: str):
    """(official_usd, cogs_usd, sell_markup) PER-SECOND that drive the credit price for a (feature, model).
    Op-tools (reframe/upscale/lip_sync) price off their op-chain's WORST (most expensive) step
    (model-independent, covers failover); every prompt-op (text/image/reference-to-video + video_edit)
    prices by the selected model. Returns (None, None, None) when nothing matches."""
    if feature in _OP_TOOL_FEATURES:
        return _op_chain_basis(feature)
    m = _MODELS.get(model_id) or {}
    return (m.get("official_usd") or m.get("cogs_usd")), m.get("cogs_usd"), None


def credits_per_sec(feature: str, model_id: str):
    """PER-SECOND sell credits for a (feature, model) — the picker badge multiplies this by the chosen
    duration, and the caller debits credits_per_sec * seconds. Single source so badge == charge. Resolves
    the per-second price basis from the registry and runs it through catalog.video_credits_for_usd at
    seconds=1 (the same double floor as image: max(official*VIDEO_SELL_MARKUP, 2*cogs) → /credit_usd_value
    → round-up-5, computed PER SECOND). Returns int per-second credits, or None if unpriced."""
    import credit_catalog as _cat
    official, cogs, markup = price_basis(feature, model_id)
    if not official:
        return None
    return _cat.video_credits_for_usd(official, cogs, seconds=1, markup=markup)


def credits_for(feature: str, model_id: str, seconds: float = 5):
    """SINGLE SOURCE for the picker badge AND the metered debit (badge == charge). Video bills PER-SECOND:
    badge = credits_per_sec(feature, model) * seconds (default 5s) via catalog.video_credits_for_usd. The
    per-second credit is rounded up to 5 FIRST (inside video_credits_for_usd), then multiplied by the
    integer duration — matching the contract's `perSecCredits * seconds`. Returns int credits, or None if
    unpriced."""
    import credit_catalog as _cat
    official, cogs, markup = price_basis(feature, model_id)
    if not official:
        return None
    return _cat.video_credits_for_usd(official, cogs, seconds=seconds, markup=markup)
