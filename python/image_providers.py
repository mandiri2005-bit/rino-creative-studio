"""
image_providers.py — multi-provider image backend with per-model failover.

Registry: image_registry.json beside this module (model -> picker meta + COGS + ordered chain
[cheapest aggregator -> 2nd -> first-party source]). dispatch() walks the chain:
each provider gets 1 call + 1 retry; on failure it advances to the next. Output bytes are
re-hosted to R2 (provider URLs expire: BFL ~10min, kie ~14d); dispatch() returns the bytes
PLUS the stable R2 object KEY — storage.aupload_bytes returns a KEY, not a URL, so the caller
signs it on read (never fetch the key as a URL). API keys = env vars (see registry._env); a
missing/empty key makes the adapter raise immediately -> auto-failover to LaoZhang/Vertex.

Normalized op contract — every adapter returns (bytes, mime); dispatch() re-hosts and returns
    {"ref": <R2 key | data: URI>, "data": <bytes>, "mime": str, "provider", "cost_usd", ...}
where op ∈ create_raster|create_vector|edit|reframe|upscale|vectorize|bg_remove and
params carries: prompt, ref_images[{url|b64}], mask{url|b64}, size, aspect, n,
transparent, expand (reframe), upscale_factor.

NOTE: structurally complete against the 2026-06-28 adapter spec
(~/docs/wimba-image-providers-adapter-spec.md) but NOT yet live-tested (no API keys
in dev) — verify each provider once its key is set in Railway.
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

log = logging.getLogger("image_providers")


def _load_registry() -> dict:
    """Locate image_registry.json robustly. It lives NEXT TO this module (python/image_registry.json) so
    it is inside the Python service's Docker build context (`COPY *.py` + an explicit COPY); the repo-root
    config/ copy is NOT in that context. Falls back to ../config for any legacy/dev layout + an env override."""
    here = Path(__file__).resolve().parent
    for c in (os.getenv("IMAGE_REGISTRY_PATH"),
              str(here / "image_registry.json"),                      # bundled beside this module (prod image + repo)
              str(here.parent / "config" / "image_registry.json")):   # legacy: config/ at repo root
        if c and Path(c).exists():
            return json.loads(Path(c).read_text())
    raise FileNotFoundError("image_registry.json not found beside image_providers.py or in ../config/")


_REG = _load_registry()
_MODELS = {m["id"]: m for m in _REG["models"]}
_OP_CHAINS = _REG.get("op_chains", {})

_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)
_POLL_EVERY = 2.0
_POLL_MAX = 180.0


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
_MAX_FETCH_BYTES = int(os.getenv("IMAGE_MAX_REF_BYTES", str(12 * 1024 * 1024))) * 3   # generous for results


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
        raise ProviderError("ref image URL not allowed")
    port = u.port or (443 if u.scheme == "https" else 80)
    pinned = None
    for info in _sock.getaddrinfo(u.hostname, port, proto=_sock.IPPROTO_TCP):
        ip = _ipx.ip_address(info[4][0])
        if not _ip_is_public(ip):
            raise ProviderError("ref image URL resolves to a non-public address")
        if pinned is None:
            pinned = ip
    if pinned is None:
        raise ProviderError("ref image URL did not resolve")
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
        raise ProviderError("image too large")   # honest oversized declaration → reject before download
    buf, total = bytearray(), 0
    async for chunk in r.aiter_bytes():
        total += len(chunk)
        if total > _MAX_FETCH_BYTES:
            raise ProviderError("image too large")   # chunked / lying Content-Length → abort mid-stream
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
            return await _drain_capped(r), r.headers.get("content-type", "image/png")
    async with client.stream("GET", url) as r:
        r.raise_for_status()
        return await _drain_capped(r), r.headers.get("content-type", "image/png")


# ── input helpers — providers disagree on URL vs base64 vs multipart file ──────
async def _img_bytes(client: httpx.AsyncClient, img: dict) -> tuple[bytes, str]:
    """Fetch an input image {url|b64} → (bytes, mime). USER url → SSRF-validated + size-capped."""
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
    ext = {"image/svg+xml": "svg", "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(mime, "png")
    # Gate on is_configured(), NOT just `storage is None` (None only on import failure). If the module
    # imported but R2 env is unset/misconfigured, aupload_bytes would raise → _try treats it as transient
    # → whole provider chain fails over → a WORKING provider is turned into an outage (and the 5 tool ops,
    # which have no legacy tail, hard-502). Fall back to an inline data: URI instead. Mirrors _persist_asset.
    if storage is None or not storage.is_configured():
        return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode())
    return await storage.aupload_bytes(f"img/{op_id}.{ext}", data, mime)


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
    return params.get("aspect") or "1:1"


# ══════════════════════════ adapters ══════════════════════════
# Each returns (bytes, mime); dispatch() re-hosts.

async def _atlascloud(client, op, slug, params):
    h = {"Authorization": f"Bearer {_key('ATLASCLOUD_API_KEY')}", "Content-Type": "application/json"}
    base = "https://api.atlascloud.ai/api/v1"
    body = {"model": slug, "enable_sync_mode": False, "output_format": "png"}
    if op in ("create_raster", "create_vector"):
        body |= {"prompt": params["prompt"], "aspect_ratio": _aspect(params)}
    elif op == "edit":
        body |= {"prompt": params["prompt"], "images": [_img_url(i) for i in params.get("ref_images", [])],
                 "aspect_ratio": _aspect(params)}
    elif op == "upscale":
        body = {"model": slug, "image": _img_url(params["ref_images"][0]), "outscale": params.get("upscale_factor", 4)}
    elif op == "bg_remove":
        body = {"model": slug, "image": _img_url(params["ref_images"][0])}
    else:
        raise ProviderError(f"atlascloud: op {op} unsupported")
    r = await client.post(f"{base}/model/generateImage", headers=h, json=body); r.raise_for_status()
    d = r.json().get("data", r.json())
    if d.get("outputs"):
        return await _fetch(client, d["outputs"][0])
    pid = d["id"]
    out = await _poll(client, f"{base}/model/prediction/{pid}", h,
                      done=lambda j: (j.get("data") or j).get("status") == "completed",
                      err=lambda j: (j.get("data") or j).get("status") == "failed",
                      result=lambda j: (j.get("data") or j)["outputs"][0])
    return await _fetch(client, out)


async def _kie(client, op, slug, params):
    h = {"Authorization": f"Bearer {_key('KIE_API_KEY')}", "Content-Type": "application/json"}
    base = "https://api.kie.ai/api/v1"
    inp = {}
    if op in ("create_raster", "create_vector"):
        inp = {"prompt": params["prompt"], "aspect_ratio": _aspect(params), "output_format": "png"}
    elif op == "edit":
        inp = {"prompt": params["prompt"], "image_urls": [_img_url(i) for i in params.get("ref_images", [])],
               "output_format": "png"}
    elif op in ("upscale", "bg_remove"):
        inp = {"image": _img_url(params["ref_images"][0])}
    r = await client.post(f"{base}/jobs/createTask", headers=h, json={"model": slug, "input": inp}); r.raise_for_status()
    tid = r.json()["data"]["taskId"]
    def _res(j):
        d = j["data"]; rj = json.loads(d["resultJson"])  # STRINGIFIED — must parse
        return rj.get("resultUrls", rj.get("result_urls", []))[0]
    out = await _poll(client, f"{base}/jobs/recordInfo?taskId={tid}", h,
                      done=lambda j: j["data"].get("state") == "success",
                      err=lambda j: j["data"].get("state") == "fail",
                      result=_res)
    return await _fetch(client, out)


async def _fal(client, op, slug, params):
    h = {"Authorization": f"Key {_key('FAL_API_KEY')}", "Content-Type": "application/json"}
    body = {}
    if op in ("create_raster",):
        body = {"prompt": params["prompt"], "image_size": params.get("size", "square_hd")}
    elif op == "edit":
        body = {"prompt": params["prompt"], "image_urls": [_img_url(i) for i in params.get("ref_images", [])]}
    elif op == "reframe":
        body = {"image_url": _img_url(params["ref_images"][0]), "image_size": params.get("size", "landscape_16_9")}
    elif op == "upscale":
        body = {"image_url": _img_url(params["ref_images"][0]), "upscale_factor": params.get("upscale_factor", 2)}
    elif op == "vectorize":
        body = {"image_url": _img_url(params["ref_images"][0])}
    elif op == "bg_remove":
        body = {"image_url": _img_url(params["ref_images"][0])}
    r = await client.post(f"https://queue.fal.run/{slug}", headers=h, json=body); r.raise_for_status()
    j = r.json(); status_url, resp_url = j["status_url"], j["response_url"]
    await _poll(client, status_url, h, done=lambda s: s.get("status") == "COMPLETED",
                err=lambda s: s.get("status") in ("FAILED", "ERROR"), result=lambda s: True)
    rr = await client.get(resp_url, headers=h); rr.raise_for_status(); out = rr.json()
    url = (out.get("images") or [out.get("image")])[0]["url"]
    return await _fetch(client, url)


async def _openai(client, op, slug, params):
    h = {"Authorization": f"Bearer {_key('OPENAI_API_KEY')}"}
    transparent = params.get("transparent")
    size = params.get("size", "1024x1024"); quality = params.get("quality", "medium")
    if op == "create_raster":
        body = {"model": slug, "prompt": params["prompt"], "size": size, "quality": quality, "n": 1}
        if transparent: body |= {"background": "transparent", "output_format": "png"}
        r = await client.post("https://api.openai.com/v1/images/generations",
                              headers={**h, "Content-Type": "application/json"}, json=body)
    elif op == "edit":
        files = []
        for i in params.get("ref_images", []):
            b, mime = await _img_bytes(client, i); files.append(("image[]", ("ref.png", b, mime)))
        data = {"model": slug, "prompt": params["prompt"], "size": size, "quality": quality}
        if transparent: data |= {"background": "transparent", "output_format": "png"}
        r = await client.post("https://api.openai.com/v1/images/edits", headers=h, data=data, files=files)
    else:
        raise ProviderError(f"openai: op {op} unsupported")
    r.raise_for_status()
    return base64.b64decode(r.json()["data"][0]["b64_json"]), "image/png"


async def _bfl(client, op, slug, params):
    h = {"x-key": _key("BFL_API_KEY"), "Content-Type": "application/json", "accept": "application/json"}
    base = "https://api.bfl.ai"; body = {"output_format": "png", "safety_tolerance": 2}
    if op == "create_raster":
        body |= {"prompt": params["prompt"], "aspect_ratio": _aspect(params)}
    elif op == "edit":
        body |= {"prompt": params["prompt"], "input_image": await _img_b64(client, params["ref_images"][0]),
                 "aspect_ratio": _aspect(params)}
    elif op == "reframe":
        slug = "/v1/flux-pro-1.0-expand"
        body = {"image": await _img_b64(client, params["ref_images"][0]), "prompt": params.get("prompt", "")}
    r = await client.post(f"{base}{slug}", headers=h, json=body); r.raise_for_status()
    j = r.json(); poll_url = j["polling_url"]
    out = await _poll(client, poll_url, h, done=lambda x: x.get("status") == "Ready",
                      err=lambda x: x.get("status") in ("Error", "Request Moderated", "Content Moderated"),
                      result=lambda x: x["result"]["sample"], interval=0.5)
    return await _fetch(client, out)  # signed URL, no CORS — server-side fetch ok


async def _byteplus(client, op, slug, params):
    h = {"Authorization": f"Bearer {_key('BYTEPLUS_API_KEY')}", "Content-Type": "application/json"}
    base = "https://ark.ap-southeast.bytepluses.com/api/v3"
    body = {"model": slug, "prompt": params["prompt"], "size": params.get("size", "2K"), "response_format": "url"}
    if op == "edit":
        body["image"] = [_img_url(i) for i in params.get("ref_images", [])]
    r = await client.post(f"{base}/images/generations", headers=h, json=body); r.raise_for_status()
    return await _fetch(client, r.json()["data"][0]["url"])


async def _recraft(client, op, slug, params):
    h = {"Authorization": f"Bearer {_key('RECRAFT_API_KEY')}"}
    base = "https://external.api.recraft.ai/v1"
    if op in ("create_raster", "create_vector"):
        body = {"prompt": params["prompt"], "model": slug, "n": 1}
        if op == "create_vector": body["style"] = "vector_illustration"
        if params.get("transparent"): body["style"] = body.get("style", "icon")
        if params.get("size"): body["size"] = params["size"]
        r = await client.post(f"{base}/images/generations", headers={**h, "Content-Type": "application/json"}, json=body)
        r.raise_for_status(); return await _fetch(client, r.json()["data"][0]["url"])
    # multipart utility/edit ops
    img, mime = await _img_bytes(client, params["ref_images"][0])
    path = {"edit": "imageToImage", "reframe": "outpaint", "upscale": slug,
            "vectorize": "vectorize", "bg_remove": "removeBackground"}.get(op, slug)
    field = "file" if op in ("upscale", "vectorize", "bg_remove") else "image"
    files = {field: ("in.png", img, mime)}
    data = {}
    if op == "edit": data = {"prompt": params["prompt"], "strength": str(params.get("strength", 0.5))}
    if op == "reframe":
        data = {"prompt": params.get("prompt", "")}
        for k in ("expand_left", "expand_right", "expand_top", "expand_bottom"):
            if params.get("expand", {}).get(k): data[k] = str(params["expand"][k])
    r = await client.post(f"{base}/images/{path}", headers=h, data=data, files=files); r.raise_for_status()
    j = r.json(); url = (j.get("data") or [j.get("image")])[0]["url"]
    return await _fetch(client, url)


async def _vectorizer(client, op, slug, params):
    aid, sec = _key("VECTORIZER_API_ID"), _key("VECTORIZER_API_SECRET")
    img, mime = await _img_bytes(client, params["ref_images"][0])
    async with client.stream("POST", "https://vectorizer.ai/api/v1/vectorize", auth=(aid, sec),
                             files={"image": ("in.png", img, mime)},
                             data={"mode": os.getenv("VECTORIZER_MODE", "production"),
                                   "output.file_format": "svg"}) as r:
        r.raise_for_status()
        return await _drain_capped(r), "image/svg+xml"  # raw SVG inline, size-capped (OOM guard)


async def _aiml(client, op, slug, params):
    # AI/ML API — OpenAI-compatible image API, model-switched on /v1/images/generations (synchronous for
    # the FLUX/gpt-image models we use). Verified vs docs.aimlapi.com/api-references/image-models 2026-06-28:
    # text-to-image vs image-to-image are DISTINCT slugs (flux/kontext-pro/{text,image}-to-image) — the
    # registry step carries the right slug per op; edit refs go in `image_url`. Size/aspect is OMITTED
    # (AIML's per-model size param name is inconsistent → avoid 400s; this is a failover, default size is OK).
    h = {"Authorization": f"Bearer {_key('AIMLAPI_API_KEY')}", "Content-Type": "application/json"}
    body = {"model": slug, "prompt": params.get("prompt") or ""}
    if op in ("edit", "reframe"):
        refs = [_img_url(i) for i in params.get("ref_images", [])]
        if refs:
            body["image_url"] = refs[0] if len(refs) == 1 else refs
    r = await client.post("https://api.aimlapi.com/v1/images/generations", headers=h, json=body)
    r.raise_for_status()
    j = r.json()
    d = (j.get("data") or j.get("images") or [j])[0]
    if isinstance(d, dict) and d.get("url"):
        return await _fetch(client, d["url"])
    b64 = d.get("b64_json") if isinstance(d, dict) else None
    if b64:
        return base64.b64decode(b64), "image/png"
    raise ProviderError(f"aiml: no image url/b64 in response: {str(j)[:200]}")


_ADAPTERS = {
    "atlascloud": _atlascloud, "kie": _kie, "fal": _fal, "openai": _openai, "aiml": _aiml,
    "bfl": _bfl, "byteplus": _byteplus, "recraft": _recraft, "vectorizer": _vectorizer,
    # laozhang + vertex are routed through the EXISTING /generate-image path (handled by the
    # caller as the guaranteed tail of every chain); see dispatch() fallback note.
}


async def _try(client, provider, op, slug, params, op_id):
    """One provider, 1 call + 1 retry. Returns (ref_key, bytes, mime) or raises ProviderError.
    ref_key = the R2 object key from _rehost (or a data: URI when storage is unset) — the rendered
    bytes are threaded back alongside it so the caller never has to re-fetch the key as a URL."""
    fn = _ADAPTERS.get(provider)
    if fn is None:
        raise ProviderError(f"no adapter for '{provider}' (use legacy /generate-image)")
    last = None
    for attempt in (1, 2):
        try:
            data, mime = await fn(client, op, slug, params)
            ref = await _rehost(data, mime, f"{op_id}-{provider}")  # R2 KEY (or data: URI if no storage)
            return ref, data, mime
        except ProviderError:
            raise  # config/auth error — don't retry, failover now
        except Exception as e:  # transient (network/5xx) — retry once
            last = e; log.warning("img %s/%s attempt %d failed: %s", provider, op, attempt, e)
            await asyncio.sleep(0.4)
    raise ProviderError(f"{provider} failed after retry: {last}")


def _slug_for(step: dict, op: str) -> Optional[str]:
    # reframe is a DISTINCT op (outpainting) — NEVER fall back to the create/text-to-image slug for it. A
    # model-chain step with no explicit reframe slug must yield None (→ dispatch skips it → fails over, →
    # 502+refund if nothing in the chain can outpaint) rather than POST a text-to-image gen and charge a
    # WRONG image as reframe (#1). op_chains.reframe steps carry an explicit "slug" so they still resolve.
    if op == "reframe":
        return step.get("reframe") or step.get("slug")
    return step.get(op) or step.get("slug") or step.get("create")


async def dispatch(feature: str, model_id: str, params: dict, op_id: str) -> dict:
    """Run `feature` for `model_id` through its failover chain. Returns
    {"ref", "data", "mime", "provider", "model", "feature", "cost_usd", "step_index"} — `data` is
    the rendered image bytes (threaded back so the caller never re-fetches `ref` as a URL — `ref`
    is a bare R2 object key, not a signed URL); cost_usd = the WINNING provider's real upstream cost
    (for accurate COGS/margin in usage_logs). Raises if the whole chain fails (caller then falls
    back to the legacy /generate-image LaoZhang/Vertex path)."""
    model = _MODELS.get(model_id)
    cogs = (model or {}).get("cogs_usd")
    official = (model or {}).get("official_usd") or cogs
    # Tool ops (upscale/vectorize/bg) are PRICED off the op-chain (price_basis→_op_chain_basis), so they MUST
    # also dispatch + book COGS off the op-chain — never a model chain. Else a client posting model=recraft-v3
    # to a tool op runs the recraft MODEL chain and books the model COGS, undercutting the margin floor (#3).
    # Prompt ops (create*/edit/reframe) use the selected model's chain (reframe with no model reframe-slug
    # then fails over / 502+refunds via _slug_for, never charging a wrong image).
    model_chain = None if feature in _OP_TOOL_FEATURES else \
        (model["chain"] if model and feature in model.get("features", []) else None)
    chain = model_chain or _OP_CHAINS.get(feature) or _OP_CHAINS.get({"upscale": "upscale_crisp"}.get(feature, feature))
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


# op-tools are priced model-independently by their op-chain; prompt-ops by the model.
_OP_TOOL_FEATURES = {"upscale", "upscale_crisp", "upscale_creative", "vectorize", "bg_remove"}


def _op_chain_basis(feature: str):
    """(worst_official, worst_cogs, sell_markup) across ALL steps of a tool's op-chain — so the SELL price
    covers the MOST EXPENSIVE provider we might fail over to (never sell off step[0] alone, which would be
    negative-margin the moment step[0] is down and we fall to a pricier source). Every op_chain step now
    carries cogs_usd/official_usd; markup = the first step that declares one (e.g. upscale_creative 1.30)."""
    key = {"upscale": "upscale_crisp"}.get(feature, feature)
    steps = _OP_CHAINS.get(key) or []
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
    """(official_usd, cogs_usd, sell_markup) that drive the credit price for a (feature, model).
    Op-tools (upscale/vectorize/bg) price off their op-chain's WORST (most expensive) step
    (model-independent, covers failover); every prompt-op (create*/edit/reframe) prices by the
    selected model. Returns (None, None, None) when nothing matches."""
    if feature in _OP_TOOL_FEATURES:
        return _op_chain_basis(feature)
    m = _MODELS.get(model_id) or {}
    return (m.get("official_usd") or m.get("cogs_usd")), m.get("cogs_usd"), None


def credits_for(feature: str, model_id: str):
    """SINGLE SOURCE for the picker badge AND the metered debit (badge == charge). Resolves the
    price basis from the registry and runs it through catalog.image_credits_for_usd (first-party ×
    markup with the 50%-margin floor + round-up-5). Returns int credits, or None if unpriced."""
    import credit_catalog as _cat
    official, cogs, markup = price_basis(feature, model_id)
    if not official:
        return None
    return _cat.image_credits_for_usd(official, cogs, markup=markup)
