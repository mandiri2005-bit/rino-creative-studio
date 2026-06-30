"""
video_providers.py — multi-provider VIDEO backend with per-model failover.

Mirror of image_providers.py for the Wimba Video tools (the /video-tools/* namespace), built
against the BUILD CONTRACT (9 ops: text_to_video, image_to_video, modify_video→video_edit,
reframe_video, upscale_video, caption_video [native], lip_sync, motion_control→image_to_video,
seamless_looping→text_to_video [loop=true]).

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


def _audio_on(params) -> Optional[bool]:
    """Tri-state audio flag from params: True/False when the caller set it, None when unspecified (so an
    adapter can OMIT the field entirely rather than forcing a provider default)."""
    a = params.get("audio")
    if a is None:
        return None
    return bool(a)


def _loop_on(params) -> bool:
    """Seamless-looping flag (set only for the seamless_looping op). Defaults False."""
    return bool(params.get("loop"))


# ══════════════════════════ adapters ══════════════════════════
# Each is async submit→_poll→_fetch and returns (bytes, mime); dispatch() re-hosts.

def _is_avatar(slug) -> bool:
    """True for audio-driven talking-head avatar slugs across providers (ByteDance OmniHuman + Kling-avatar
    on atlascloud/fal/aiml/kie). These take a hosted PORTRAIT + a hosted DRIVING-AUDIO and lip-sync — NOT
    the generic clip params (no duration/aspect/resolution/audio-toggle). Our video models never otherwise
    contain 'omnihuman' or 'avatar' in the slug, so this match is safe."""
    s = (slug or "").lower()
    return ("omnihuman" in s) or ("avatar" in s)


def _avatar_audio_url(params):
    """Hosted driving-audio URL for an avatar render (the recipe passes params['audio_ref'] = {url} | url)."""
    aud = params.get("audio_ref")
    if aud is None:
        return None
    return aud.get("url") if isinstance(aud, dict) else aud


async def _atlascloud_video(client, op, slug, params):
    """AtlasCloud video generation — POST /model/generateVideo + poll /model/prediction/{id} (endpoint
    DOC-VERIFIED). The request BODY field names diverge BY MODEL FAMILY (slug prefix), so a generic body
    silently no-ops params to provider defaults. Mapped per FAILOVER-DOC-VERIFY:
      • AUDIO field: 'generate_audio' for google/* + bytedance/*, 'sound' for kwaivgi/*, OMITTED for
        alibaba/happyhorse* (no audio field).
      • ASPECT field: 'aspect_ratio' for google/*, 'ratio' for bytedance/*, OMITTED for kwaivgi/* +
        happyhorse (no aspect param).
      • REFERENCE-TO-VIDEO on bytedance/* uses the array body['reference_images']=[urls]; image_to_video
        uses the single body['image'].
      • RESOLUTION casing: lowercase ('720p'/'1080p'/'4k') for google/* + bytedance/*; UPPERCASE
        ('720P'/'1080P') for kwaivgi/* + happyhorse.
    Done-check accepts 'completed' AND 'succeeded'."""
    h = {"Authorization": f"Bearer {_key('ATLASCLOUD_API_KEY')}", "Content-Type": "application/json"}
    base = "https://api.atlascloud.ai/api/v1"
    s = (slug or "").lower()
    is_google = s.startswith("google/")
    is_bytedance = s.startswith("bytedance/")
    is_kling = s.startswith("kwaivgi/")
    is_happyhorse = "happyhorse" in s
    # AUDIO-DRIVEN avatar (talking head): portrait + driving voice → lip-synced video. Distinct body from
    # the generic clip models, so handle it FIRST and skip the generic builder.
    is_avatar = ("avatar-omni-human" in s) or s.endswith("/avatar")
    if is_avatar:
        # atlascloud REQUIRES hosted URLs for both inputs (its pricing probe FETCHES them — data: URIs are
        # rejected with "url not allowed for probe"), and the driving audio is MANDATORY (the avatar IS the
        # lip-sync). Field names differ by family: omnihuman → image_url/audio_url (+ output_resolution
        # 720|1080); kwaivgi/.../avatar → image/audio.
        imgs = params.get("ref_images") or []
        aud = params.get("audio_ref")
        if not imgs or aud is None:
            raise ProviderError("atlascloud avatar requires a portrait (ref_images[0]) + driving audio (audio_ref) as hosted URLs")
        img_u = _img_url(imgs[0])
        aud_u = aud.get("url") if isinstance(aud, dict) else aud
        if "avatar-omni-human" in s:
            body = {"model": slug, "image_url": img_u, "audio_url": aud_u}
            _rr = _norm_res(params.get("resolution") or "720p")
            body["output_resolution"] = 720 if str(_rr).startswith("720") else 1080
        else:                                                # kwaivgi/.../avatar
            body = {"model": slug, "image": img_u, "audio": aud_u}
        if params.get("prompt"):
            body["prompt"] = params["prompt"]
    else:
        body = {"model": slug, "enable_sync_mode": False, "duration": _seconds(params)}
        # ASPECT — field name + presence per family.
        if is_google:
            body["aspect_ratio"] = _aspect(params)
        elif is_bytedance:
            body["ratio"] = _aspect(params)
        # kwaivgi/* + happyhorse take NO aspect param → omit.
        if op == "text_to_video":
            body["prompt"] = params.get("prompt") or ""
        elif op in ("image_to_video", "reference_to_video"):
            body["prompt"] = params.get("prompt") or ""
            refs = params.get("ref_images") or []
            if refs:
                if op == "reference_to_video" and is_bytedance:
                    body["reference_images"] = [_img_url(r) for r in refs]   # Seedance ref → array
                else:
                    body["image"] = _img_url(refs[0])                        # i2v seed frame (single)
        elif op == "video_edit":
            body["prompt"] = params.get("prompt") or ""
            rv = params.get("ref_video")
            if rv:
                body["video"] = _img_url(rv)
        else:
            raise ProviderError(f"atlascloud_video: op {op} unsupported")
        # RESOLUTION casing per family: lowercase for veo/seedance, UPPERCASE for kling/happyhorse.
        if params.get("resolution"):
            rsv = _norm_res(params["resolution"])           # canonical lowercase e.g. '1080p'/'4k'
            body["resolution"] = rsv.upper() if (is_kling or is_happyhorse) else rsv
        # AUDIO field name per family — omit entirely for happyhorse (no audio field).
        _a = _audio_on(params)
        if _a is not None and not is_happyhorse:
            body["sound" if is_kling else "generate_audio"] = _a
        if _loop_on(params):
            body["loop"] = True
    r = await client.post(f"{base}/model/generateVideo", headers=h, json=body); r.raise_for_status()
    d = r.json().get("data", r.json())
    if d.get("outputs"):                                 # rare inline-result fast path
        return await _fetch(client, d["outputs"][0])
    pid = d["id"]
    _done = {"completed", "succeeded"}
    out = await _poll(client, f"{base}/model/prediction/{pid}", h,
                      done=lambda j: (j.get("data") or j).get("status") in _done,
                      err=lambda j: (j.get("data") or j).get("status") == "failed",
                      result=lambda j: (j.get("data") or j)["outputs"][0])
    return await _fetch(client, out)


async def _fal_video(client, op, slug, params):
    """fal.ai queue video — POST queue.fal.run/{slug} → poll status_url (COMPLETED) → GET response_url →
    output video.url (DOC-VERIFIED queue contract). Per-op input fields: image_url (i2v/ref seed),
    video_url (video_edit/reframe/lip_sync source), audio_url (lip_sync). Per FAILOVER-DOC-VERIFY:
      • AUTH is 'Authorization: Key <key>' (NOT Bearer).
      • AUDIO param is 'generate_audio' (NOT 'enable_audio').
      • DURATION is a STRING enum: veo3.1* slugs need the 's'-suffixed form ('8s'/'6s'/'4s'); all others
        send bare seconds ('8'/'5'/'6').
      • RESOLUTION is OMITTED for fal-ai/kling-video/o3/standard* and fal-ai/minimax/hailuo-02* (no
        resolution param — passing one risks a 422)."""
    h = {"Authorization": f"Key {_key('FAL_API_KEY')}", "Content-Type": "application/json"}
    s = (slug or "").lower()
    if _is_avatar(slug):
        # AUDIO-DRIVEN avatar (OmniHuman / Kling ai-avatar): hosted portrait + hosted voice → talking head.
        # fal field names are image_url/audio_url; NO duration/aspect/resolution/generate_audio (length is
        # driven by the audio). Optional prompt (kling avatar allows it).
        refs = params.get("ref_images") or []
        aud = _avatar_audio_url(params)
        if not refs or not aud:
            raise ProviderError("fal avatar requires ref_images[0] + audio_ref (hosted urls)")
        body = {"image_url": _img_url(refs[0]), "audio_url": aud}
        if params.get("prompt"):
            body["prompt"] = params["prompt"]
    else:
        body = {}
        if params.get("prompt"):
            body["prompt"] = params["prompt"]
        if op in ("image_to_video", "reference_to_video"):
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
        # DURATION as STRING; veo3.1 family expects the 's'-suffixed enum, everything else bare seconds.
        _secs = str(_seconds(params))
        body["duration"] = f"{_secs}s" if "veo3.1" in s else _secs
        # RESOLUTION — gated: kling-o3/standard + hailuo-02 have NO resolution param.
        _no_res = ("fal-ai/kling-video/o3/standard" in s) or ("fal-ai/minimax/hailuo-02" in s)
        if params.get("resolution") and not _no_res:
            body["resolution"] = params["resolution"]
        # fal audio toggle is `generate_audio` on audio-capable models + a `loop` flag on looping models.
        # Pass defensively (only when set); fal ignores unknown input fields rather than 400-ing.
        _a = _audio_on(params)
        if _a is not None:
            body["generate_audio"] = _a
        if _loop_on(params):
            body["loop"] = True
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
    """AI/ML API video — submit returns a generation id, then poll by id (endpoint DOC-VERIFIED):
    POST https://api.aimlapi.com/v2/video/generations (submit) and GET the same path with
    ?generation_id=<id> (poll). Submit id read from `id`; status done=='completed' / err=='error';
    output video.url. Raises ProviderError on an unexpected shape so the step fails over rather than
    charging for a 404/garbage response."""
    h = {"Authorization": f"Bearer {_key('AIMLAPI_API_KEY')}", "Content-Type": "application/json"}
    base = "https://api.aimlapi.com/v2/video/generations"
    if _is_avatar(slug):
        # AUDIO-DRIVEN avatar (OmniHuman / klingai/avatar-*): hosted portrait + voice → talking head; NO
        # duration/aspect/resolution/enable_audio (length follows the audio).
        refs = params.get("ref_images") or []
        aud = _avatar_audio_url(params)
        if not refs or not aud:
            raise ProviderError("aiml avatar requires ref_images[0] + audio_ref (hosted urls)")
        body = {"model": slug, "image_url": _img_url(refs[0]), "audio_url": aud}
        if params.get("prompt"):
            body["prompt"] = params["prompt"]
    else:
        body = {"model": slug, "prompt": params.get("prompt") or ""}
        if op in ("image_to_video", "reference_to_video"):
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
        # AIML audio/loop support is per-model and under-documented — pass best-effort + defensively (only
        # when set) so an audio-incapable model just ignores the unknown field instead of 400-ing.
        _a = _audio_on(params)
        if _a is not None:
            body["enable_audio"] = _a
        if _loop_on(params):
            body["loop"] = True
    r = await client.post(base, headers=h, json=body); r.raise_for_status()
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

    out = await _poll(client, f"{base}?generation_id={gid}", h,
                      done=lambda j: _status(j) in ("completed", "succeeded", "success", "done"),
                      err=lambda j: _status(j) in ("failed", "error", "cancelled", "canceled"),
                      result=_result)
    return await _fetch(client, out)


def _kie_jobs_input(slug, op, params):
    """Build the per-model 'input' object for KIE's UNIFIED /jobs branch — field names + types DIVERGE
    PER MODEL FAMILY (the core KIE adapter problem), so a small per-slug-family builder maps our generic
    params onto each model's exact fields. All durations are model-specific (int vs string); image-input
    field names differ (first_frame_url/last_frame_url/reference_image_urls[] vs image_urls[] vs
    image_url single). Resolution/aspect/audio presence + casing differ per family."""
    s = (slug or "").lower()
    prompt = params.get("prompt") or ""
    refs = params.get("ref_images") or []
    ref_urls = [_img_url(r) for r in refs]
    secs = _seconds(params)
    res = _norm_res(params["resolution"]) if params.get("resolution") else None
    aspect = _aspect(params)
    a = _audio_on(params)
    inp = {"prompt": prompt}

    if "seedance" in s:                                   # bytedance/seedance-* → ints, generate_audio
        if op in ("image_to_video", "reference_to_video") and ref_urls:
            if op == "reference_to_video":
                inp["reference_image_urls"] = ref_urls
            else:
                inp["first_frame_url"] = ref_urls[0]
                if len(ref_urls) > 1:
                    inp["last_frame_url"] = ref_urls[1]
        if a is not None:
            inp["generate_audio"] = a
        inp["duration"] = secs                            # INT
        if res:
            inp["resolution"] = res                       # '480p'..'4k' lowercase
        inp["aspect_ratio"] = aspect
    elif "kling" in s:                                    # kling-3.0/video → image_urls[], sound, mode, str dur
        if op in ("image_to_video", "reference_to_video") and ref_urls:
            inp["image_urls"] = ref_urls
        if a is not None:
            inp["sound"] = a
        # mode encodes tier/resolution: 4K→'4K', 1080p→'pro', else 'std'.
        inp["mode"] = "4K" if res == "4k" else ("pro" if res == "1080p" else "std")
        inp["duration"] = str(secs)                       # STRING
    elif "hailuo" in s:                                   # hailuo/* → image_url single, str dur, UPPER res
        if op in ("image_to_video", "reference_to_video") and ref_urls:
            inp["image_url"] = ref_urls[0]                # SINGLE
        inp["duration"] = str(secs)                       # STRING '6'|'10'
        if res:
            inp["resolution"] = "1080P" if res == "1080p" else "768P"
    elif s.startswith("wan"):                             # wan* → image_urls[], str dur
        if op in ("image_to_video", "reference_to_video") and ref_urls:
            inp["image_urls"] = ref_urls
        inp["duration"] = str(secs)                       # STRING
        if res:
            inp["resolution"] = res
        inp["aspect_ratio"] = aspect
    else:                                                 # happyhorse* + any other jobs model → generic
        if op in ("image_to_video", "reference_to_video") and ref_urls:
            inp["image_urls"] = ref_urls
        if op == "video_edit":
            rv = params.get("ref_video")
            if rv:
                inp["video_url"] = _img_url(rv)
        inp["duration"] = str(secs)
        if res:
            inp["resolution"] = res
    return inp


async def _kie_video(client, op, slug, params):
    """KIE API video — TWO endpoints by slug (DOC-VERIFIED, FAILOVER-DOC-VERIFY):
      (a) VEO slugs (veo3/veo3_fast/veo3_lite) → POST /api/v1/veo/generate with a FLAT body
          {prompt, model, imageUrls[], generationType, aspect_ratio, resolution, duration} → poll
          GET /api/v1/veo/record-info?taskId={id} until data.successFlag; result data.resultUrls[0].
      (b) JOBS slugs (bytedance/*, kling-3.0/video, hailuo/*, wan*, happyhorse*) → POST
          /api/v1/jobs/createTask {model, input:{...per-model...}} → poll GET
          /api/v1/jobs/recordInfo?taskId={id} until data.state=='success'; result =
          json.loads(data.resultJson)['resultUrls'][0]  (resultJson is a JSON STRING — must json.loads).
    Reuses _poll/_fetch/_rehost; KIE result URLs expire ~24h → _fetch re-hosts immediately.
    Keeps ALL SSRF/OOM guards (_img_bytes/_capped_get on ref inputs, _drain_capped on result fetch)."""
    h = {"Authorization": f"Bearer {_key('KIE_API_KEY')}", "Content-Type": "application/json"}
    base = "https://api.kie.ai/api/v1"
    s = (slug or "").lower()
    is_veo = s in ("veo3", "veo3_fast", "veo3_lite") or s.startswith("veo3")

    if is_veo:
        refs = params.get("ref_images") or []
        ref_urls = [_img_url(r) for r in refs]
        # generationType per op: i2v/ref carry imageUrls; t2v is plain.
        if op == "reference_to_video":
            gen_type = "REFERENCE_2_VIDEO"
        elif op == "image_to_video" and ref_urls:
            gen_type = "FIRST_AND_LAST_FRAMES_2_VIDEO"
        else:
            gen_type = "TEXT_2_VIDEO"
        body = {"prompt": params.get("prompt") or "", "model": slug,
                "generationType": gen_type, "aspect_ratio": _aspect(params)}
        if ref_urls and op in ("image_to_video", "reference_to_video"):
            body["imageUrls"] = ref_urls
        if params.get("resolution"):
            body["resolution"] = _norm_res(params["resolution"])    # '720p'|'1080p'|'4k'
        body["duration"] = _seconds(params)                          # 4|6|8 int
        r = await client.post(f"{base}/veo/generate", headers=h, json=body); r.raise_for_status()
        j = r.json()
        tid = (j.get("data") or {}).get("taskId") or j.get("taskId") or (j.get("data") or {}).get("id")
        if not tid:
            raise ProviderError(f"kie_video(veo): no taskId in submit response: {str(j)[:200]}")

        def _veo_result(jj):
            d = jj.get("data") or {}
            urls = d.get("resultUrls") or (d.get("response") or {}).get("resultUrls")
            if not urls:
                raise ProviderError(f"kie_video(veo): no resultUrls in response: {str(jj)[:200]}")
            return urls[0]

        out = await _poll(client, f"{base}/veo/record-info?taskId={tid}", h,
                          done=lambda jj: (jj.get("data") or {}).get("successFlag") in (1, True, "1"),
                          err=lambda jj: str((jj.get("data") or {}).get("successFlag")) in ("2", "3"),
                          result=_veo_result)
        return await _fetch(client, out)

    # ── JOBS branch ──
    if _is_avatar(slug):
        # AUDIO-DRIVEN avatar (omnihuman-1-5 / kling/ai-avatar-*): input.image_url + input.audio_url, NO
        # duration/resolution. The kling avatar REQUIRES a non-empty prompt (probe: '' → 500 "prompt is
        # required"), so default it.
        refs = params.get("ref_images") or []
        aud = _avatar_audio_url(params)
        if not refs or not aud:
            raise ProviderError("kie avatar requires ref_images[0] + audio_ref (hosted urls)")
        inp = {"image_url": _img_url(refs[0]), "audio_url": aud,
               "prompt": (params.get("prompt") or "a friendly presenter speaking to camera")}
    else:
        inp = _kie_jobs_input(slug, op, params)
    body = {"model": slug, "input": inp}
    r = await client.post(f"{base}/jobs/createTask", headers=h, json=body); r.raise_for_status()
    j = r.json()
    tid = (j.get("data") or {}).get("taskId") or j.get("taskId") or (j.get("data") or {}).get("id")
    if not tid:
        raise ProviderError(f"kie_video(jobs): no taskId in submit response: {str(j)[:200]}")

    def _jobs_result(jj):
        d = jj.get("data") or {}
        raw = d.get("resultJson")
        if not raw:
            raise ProviderError(f"kie_video(jobs): no resultJson in response: {str(jj)[:200]}")
        parsed = json.loads(raw) if isinstance(raw, str) else raw   # resultJson is a JSON STRING
        urls = parsed.get("resultUrls") or []
        if not urls:
            raise ProviderError(f"kie_video(jobs): no resultUrls in resultJson: {str(parsed)[:200]}")
        return urls[0]

    out = await _poll(client, f"{base}/jobs/recordInfo?taskId={tid}", h,
                      done=lambda jj: (jj.get("data") or {}).get("state") == "success",
                      err=lambda jj: (jj.get("data") or {}).get("state") == "fail",
                      result=_jobs_result)
    return await _fetch(client, out)


def _laozhang_size(params) -> str:
    """Derive an OpenAI-Videos-API `size` token ('WIDTHxHEIGHT') from aspect + resolution for the
    Sora/Veo /v1/videos route (those models take `size`, not a separate resolution+aspect). Maps the
    canonical short edge ('720p'/'1080p'/'4k', default 720p) onto 16:9 / 9:16 dims. VERIFY: exact
    accepted sizes are model-specific (docs list 1280x720 / 1024x1792 / 1920x1080 / 3840x2160); we emit
    the standard 16:9 / 9:16 pairs and the provider clamps/validates."""
    res = _norm_res(params.get("resolution")) or "720p"
    short = {"720p": 720, "1080p": 1080, "4k": 2160}.get(res, 720)
    long_ = {720: 1280, 1080: 1920, 2160: 3840}[short]
    asp = _aspect(params)
    if asp in ("9:16", "portrait") or (isinstance(asp, str) and asp.startswith("9:")):
        return f"{short}x{long_}"          # portrait
    return f"{long_}x{short}"              # 16:9 / default landscape


async def _laozhang_openai_video(client, h, slug, op, params):
    """OpenAI Videos API path shared by LaoZhang Sora AND Veo (both go through /v1/videos now — the old
    custom /veo route is DEPRECATED): POST /v1/videos {model, prompt, seconds(str), size} → poll
    GET /v1/videos/{id} until status=='completed' → the video is BINARY at GET /v1/videos/{id}/content
    (there is NO url field). We stream that binary through _drain_capped (OOM cap) and _rehost it.
    i2v: VERIFY — JSON requests pass first-frame Data URI(s) in `images`[] (+ `metadata.lastFrame` for a
    last frame); the multipart `input_reference` file form is the alternative. We send `images` as Data
    URIs so the request stays JSON (do NOT use `image`/`reference_image`/`referenceImages` — deprecated)."""
    body = {"model": slug, "prompt": params.get("prompt") or "",
            "seconds": str(_seconds(params)), "size": _laozhang_size(params)}
    if op in ("image_to_video", "reference_to_video"):
        refs = params.get("ref_images") or []
        if refs:
            # JSON i2v: first frame(s) as Data URIs in `images`; a 2nd ref → metadata.lastFrame.
            body["images"] = [_img_url(r) for r in refs[:2]]
            if len(refs) > 1:
                body.setdefault("metadata", {})["lastFrame"] = _img_url(refs[1])
    r = await client.post("https://api.laozhang.ai/v1/videos", headers=h, json=body); r.raise_for_status()
    j = r.json()
    vid = (j.get("id") or (j.get("data") or {}).get("id"))
    if not vid:
        raise ProviderError(f"laozhang_video(openai): no video id in submit response: {str(j)[:200]}")

    def _status(jj):
        return ((jj.get("status") or (jj.get("data") or {}).get("status")) or "").lower()

    # Poll status only (the result is a separate binary endpoint, not a URL in this JSON) — _poll's
    # result() just signals readiness; we fetch the bytes from /content afterward.
    await _poll(client, f"https://api.laozhang.ai/v1/videos/{vid}", h,
                done=lambda jj: _status(jj) in ("completed", "succeeded", "success"),
                err=lambda jj: _status(jj) in ("failed", "error", "cancelled", "canceled"),
                result=lambda jj: True)
    # BINARY download — NO url field. Stream through the running-total OOM cap, then _rehost (by caller).
    async with client.stream("GET", f"https://api.laozhang.ai/v1/videos/{vid}/content",
                             headers=h) as rc:
        rc.raise_for_status()
        data = await _drain_capped(rc)
        mime = rc.headers.get("content-type", "video/mp4")
    if not data:
        raise ProviderError("laozhang_video(openai): empty content body")
    return data, mime


async def _laozhang_wan_video(client, h, slug, op, params):
    """LaoZhang Wan (DashScope path, NOT OpenAI-compatible): POST
    /wan/api/v1/services/aigc/video-generation/video-synthesis with header X-DashScope-Async: enable and a
    NESTED body {model, input:{prompt, media:[{type:'first_frame',url}]}, parameters:{resolution,duration}}
    → submit returns output.task_id → poll GET /v1/tasks/{task_id} until status=='completed' → top-level
    `result_url` (a signed upstream URL → _fetch + _rehost; per docs the LaoZhang auth header must NOT be
    sent to result_url, and _fetch uses no auth header). VERIFY: the nested input/parameters shape +
    X-DashScope-Async header are doc-derived; field casing of resolution is UPPERCASE ('720P'/'1080P')."""
    wh = dict(h); wh["X-DashScope-Async"] = "enable"
    inp = {"prompt": params.get("prompt") or ""}
    if op in ("image_to_video", "reference_to_video"):
        refs = params.get("ref_images") or []
        if refs:
            inp["media"] = [{"type": "first_frame", "url": _img_url(refs[0])}]
            if len(refs) > 1:
                inp["media"].append({"type": "last_frame", "url": _img_url(refs[1])})
    par = {"duration": _seconds(params)}
    if params.get("resolution"):
        par["resolution"] = _norm_res(params["resolution"]).upper()    # '720P'/'1080P'
    body = {"model": slug, "input": inp, "parameters": par}
    r = await client.post(
        "https://api.laozhang.ai/wan/api/v1/services/aigc/video-generation/video-synthesis",
        headers=wh, json=body); r.raise_for_status()
    j = r.json()
    tid = (j.get("output") or {}).get("task_id") or j.get("task_id") or (j.get("data") or {}).get("task_id")
    if not tid:
        raise ProviderError(f"laozhang_video(wan): no task_id in submit response: {str(j)[:200]}")

    def _status(jj):
        d = jj.get("output") or jj
        return ((d.get("task_status") or d.get("status") or jj.get("status")) or "").lower()

    def _result(jj):
        d = jj.get("output") or jj
        url = jj.get("result_url") or d.get("result_url") or d.get("video_url")
        if not url:
            results = d.get("results")
            if isinstance(results, list) and results and isinstance(results[0], dict):
                url = results[0].get("url")
        if not url:
            raise ProviderError(f"laozhang_video(wan): no result_url in response: {str(jj)[:200]}")
        return url

    out = await _poll(client, f"https://api.laozhang.ai/v1/tasks/{tid}", h,
                      done=lambda jj: _status(jj) in ("completed", "succeeded", "success", "done"),
                      err=lambda jj: _status(jj) in ("failed", "error", "cancelled", "canceled", "unknown"),
                      result=_result)
    return await _fetch(client, out)   # signed upstream URL → no LaoZhang auth header (per docs)


async def _laozhang_video(client, op, slug, params):
    """LaoZhang video — THREE endpoint shapes by slug (doc-verified at docs.laozhang.ai/en):
      (a) VEO slugs (veo-3.1-generate-preview, veo-3.1-fast-generate-preview): the CURRENT LaoZhang Veo
          route is the OpenAI Videos API (the old custom /veo route is DEPRECATED) — same /v1/videos
          submit→poll→/content binary download as Sora. VERIFY: Veo-via-/v1/videos confirmed on the docs
          Veo page; i2v `images`[]/`metadata.lastFrame` field names are the JSON-request shape.
      (b) SORA slugs (sora-2, sora-2-pro): OpenAI Videos API — POST /v1/videos {model, prompt,
          seconds(str), size} → poll GET /v1/videos/{id} → BINARY at GET /v1/videos/{id}/content (no url).
      (c) WAN slugs (wan2.7-*, wan2.6-*, wan2.5-*): DashScope synthesis path → poll /v1/tasks/{id} →
          result_url.
    Auth = Authorization: Bearer _key('LAOZHANG_IMAGE_API_KEY') — the live LaoZhang account key (same
    one the image path uses; despite the 'IMAGE' name it is the general LaoZhang API key). Keeps ALL
    SSRF/OOM/redirect guards (_img_url on refs, _drain_capped on the binary stream, _fetch on the Wan
    result_url). Defensive: unknown params omitted; ProviderError on an unexpected shape so it fails over."""
    h = {"Authorization": f"Bearer {_key('LAOZHANG_IMAGE_API_KEY')}", "Content-Type": "application/json"}
    s = (slug or "").lower()
    if s.startswith("wan"):
        return await _laozhang_wan_video(client, h, slug, op, params)
    if s.startswith("sora") or s.startswith("veo"):
        return await _laozhang_openai_video(client, h, slug, op, params)
    raise ProviderError(f"laozhang_video: unrecognized slug '{slug}'")


_ADAPTERS = {
    "atlascloud": _atlascloud_video, "fal": _fal_video, "aiml": _aiml_video, "kie": _kie_video,
    "laozhang": _laozhang_video,
    # vertex is routed through the EXISTING Veo/Sora path (handled by the caller as the guaranteed legacy
    # tail of every chain) — it has NO adapter here, so a chain step naming it raises `no adapter for
    # '...'` → that step fails over → caller's legacy tail. See dispatch(). laozhang now HAS an adapter.
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
    # COGS booking uses the per-RESOLUTION cost for the SERVED resolution (v2): chain[0] (cheapest
    # aggregator) costs the per-res cogs_usd; any failover to a pricier source books the conservative
    # per-res official_usd. Resolves from the request's resolution → default_resolution → top-level anchor.
    if model:
        official, cogs = _res_basis(model, (params or {}).get("resolution"))
    else:
        cogs = official = None
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
    # DYNAMIC cheapest-first: re-order the failover chain by the pricing catalog (single source of truth)
    # instead of trusting the hand-typed registry order. Falls back to the registry order untouched when the
    # catalog doesn't cover this model (backward-compatible). Pure reorder — same steps, cheapest provider first.
    try:
        import pricing as _pricing
        chain = _pricing.reorder_steps(model_id, feature, list(chain))
    except Exception as _e:
        log.warning("dispatch: pricing-catalog reorder skipped (%s) — using registry order", _e)
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
                # COGS of the provider that ACTUALLY served — booked at its REAL per-provider price:
                #   1) an op-chain step carries its own cogs_usd;
                #   2) else the pricing catalog's price for (model, this provider) — the accurate number,
                #      so a failover to a pricier source books what we REALLY pay (not a cheap/official guess);
                #   3) else the legacy fallback (chain[0]=model cogs, else official).
                cost = step.get("cogs_usd")
                if cost is None:
                    try:
                        import pricing as _pricing
                        cost = _pricing.cost_for_provider(model_id, prov, feature)
                    except Exception:
                        cost = None
                if cost is None:
                    cost = cogs if idx == 0 else official
                return {"ref": ref, "data": data, "mime": mime, "provider": prov,
                        "model": model_id, "feature": feature, "cost_usd": cost, "step_index": idx}
            except ProviderError as e:
                errors.append(f"{prov}: {e}"); log.info("failover %s: %s", prov, e); continue
    raise ProviderError("all providers failed → " + " | ".join(errors))


# convenience for the banner / picker
def model_catalog() -> list:
    """Picker metadata (no secrets) for the studio model dropdown. v2: emits each model's FULL caps so
    the frontend can compute the badge locally (badge == charge) — durations[], default_duration,
    resolutions[{res,cogs_usd,official_usd}], default_resolution, audio (always|toggle|none) and
    audio_on_mult — plus label/sublabel/icon/features. Per-res prices are surfaced so the frontend's
    creditFor() runs the exact same double-floor × audio_mult formula as credit_catalog."""
    try:
        import pricing as _pricing
    except Exception:
        _pricing = None
    out = []
    for m in _REG["models"]:
        res = [{"res": r.get("res"), "cogs_usd": r.get("cogs_usd"), "official_usd": r.get("official_usd")}
               for r in (m.get("resolutions") or [])]
        # worst-case (max known) per-sec COGS across the model's provider chain — lets the frontend show
        # the SAME worst-case-hold badge the backend reserves under option A (else the optimistic badge
        # would read cheap then jump to the live worst-case estimate). None → FE falls back to cogs.
        worst = None
        if _pricing is not None:
            try:
                worst = _pricing.bounds_any(m["id"]).get("max_cost")
            except Exception:
                worst = None
        out.append({
            "id": m["id"], "label": m["label"], "sublabel": m.get("sublabel", ""),
            "icon": m.get("icon", ""), "features": m.get("features", []),
            "transparent": m.get("transparent", False),
            "durations": m.get("durations") or [],
            "default_duration": m.get("default_duration"),
            "resolutions": res,
            "default_resolution": m.get("default_resolution"),
            "audio": m.get("audio", "none"),
            "audio_on_mult": m.get("audio_on_mult", 1),
            "official_usd": m.get("official_usd"), "cogs_usd": m.get("cogs_usd"),
            "worst_cogs_usd": worst,
        })
    return out


def _res_basis(model: dict, resolution=None) -> tuple:
    """(official_usd, cogs_usd) for a model at `resolution`. Resolves the per-res entry from the model's
    resolutions[] (exact `res` match, normalised so '4K'=='4k', '1080p'=='1080'…), falling back to the
    model's default_resolution entry, then the first resolutions[] row, then the model's top-level
    cogs_usd/official_usd anchor. official falls back to cogs when a row omits it."""
    rows = model.get("resolutions") or []
    chosen = None
    if rows:
        want = _norm_res(resolution) if resolution else None
        if want:
            chosen = next((r for r in rows if _norm_res(r.get("res")) == want), None)
        if chosen is None:
            dflt = _norm_res(model.get("default_resolution"))
            chosen = next((r for r in rows if _norm_res(r.get("res")) == dflt), None)
        if chosen is None:
            chosen = rows[0]
    if chosen is not None:
        cogs = chosen.get("cogs_usd")
        official = chosen.get("official_usd") or cogs
        return official, cogs
    cogs = model.get("cogs_usd")
    return (model.get("official_usd") or cogs), cogs


def _norm_res(res) -> str:
    """Normalise a resolution token to a canonical lowercase key for matching: '4K'→'4k', '1080p'→'1080p',
    '1080'→'1080p', '720'→'720p', '480'→'480p'. Bare unknown tokens pass through lowercased/stripped."""
    s = str(res or "").strip().lower()
    if not s:
        return ""
    if "2160" in s or "3840" in s or "4k" in s or "uhd" in s:
        return "4k"
    if "1080" in s or "1920" in s:
        return "1080p"
    if "720" in s or "1280" in s:
        return "720p"
    if "480" in s:
        return "480p"
    return s


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


def price_basis(feature: str, model_id: str, resolution=None):
    """(official_usd, cogs_usd, sell_markup) PER-SECOND that drive the credit price for a (feature, model).
    v2: prompt-ops resolve the per-RESOLUTION basis from the model's resolutions[] (fallback
    default_resolution → first row → top-level anchor) via _res_basis. Op-tools (reframe/upscale/lip_sync)
    price off their op-chain's WORST (most expensive) step (model-independent, covers failover) and are
    resolution-invariant. Returns (None, None, None) when nothing matches."""
    if feature in _OP_TOOL_FEATURES:
        return _op_chain_basis(feature)
    m = _MODELS.get(model_id) or {}
    if not m:
        return None, None, None
    official, cogs = _res_basis(m, resolution)
    return official, cogs, None


def _audio_mult_for(model: dict, audio_on: bool) -> float:
    """The audio price multiplier for a model: model.audio_on_mult when audio is engaged — i.e. the caller
    asked for audio on, OR the model emits native always-on audio (audio=='always') — else 1.0. A 'none'
    (silent) model never charges the multiplier even if audio_on is mistakenly passed."""
    audio = (model or {}).get("audio", "none")
    if audio == "none":
        return 1.0
    engaged = bool(audio_on) or audio == "always"
    if not engaged:
        return 1.0
    try:
        return float(model.get("audio_on_mult", 1) or 1)
    except (TypeError, ValueError):
        return 1.0


def credits_per_sec(feature: str, model_id: str, resolution=None, audio_on: bool = False):
    """PER-SECOND sell credits for a (feature, model, resolution, audio) — a DISPLAY figure. NOTE the v2
    badge is the TOTAL (ceil5 applied once to the full clip), so the badge is credits_for(...), NOT
    credits_per_sec × seconds. This per-sec figure (ceil5 over one second) is kept only for any per-second
    display label. Returns int per-second credits, or None if unpriced."""
    import credit_catalog as _cat
    official, cogs, markup = price_basis(feature, model_id, resolution)
    if not official:
        return None
    am = _audio_mult_for(_MODELS.get(model_id) or {}, audio_on)
    return _cat.video_credits_for_usd(official, cogs, seconds=1, markup=markup, audio_mult=am)


def credits_for(feature: str, model_id: str, seconds: float = 5, resolution=None, audio_on: bool = False,
                cogs_override: float = None):
    """SINGLE SOURCE for the picker badge AND the metered debit. v2 per-model-caps: resolves the
    per-RESOLUTION price basis from the model's resolutions[] (fallback default_resolution), derives the
    audio multiplier (model.audio_on_mult when audio_on or model.audio=='always', else 1.0), and runs the
    full clip through catalog.video_credits_for_usd — double floor max(official×markup, 2×cogs) × audio_mult
    × seconds, ceil5 ONCE on the FULL-CLIP total. Returns int credits, or None if unpriced.

    cogs_override: price the clip off THIS per-second COGS instead of the registry's (cheapest) cogs — used
    by the worst-case-hold / charge-at-actual-provider billing (option A). When the override exceeds the
    first-party `official`, official is bumped to it so the 2×cogs margin floor governs (never under-prices
    a pricier provider); when it's below official, the result matches the normal cheapest-cogs price."""
    import credit_catalog as _cat
    official, cogs, markup = price_basis(feature, model_id, resolution)
    if not official:
        return None
    if cogs_override is not None and cogs_override > 0:
        cogs = float(cogs_override)
        official = max(official, cogs)
    am = _audio_mult_for(_MODELS.get(model_id) or {}, audio_on)
    return _cat.video_credits_for_usd(official, cogs, seconds=seconds, markup=markup, audio_mult=am)
