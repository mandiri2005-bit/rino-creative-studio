"""
Step 2 (object storage) — storage abstraction + asset durability.

Runs against the real R2 bucket when STORAGE_* creds are present in the env;
skips cleanly otherwise (so the suite stays green without secrets).

The durability test is the core Step 2 guarantee: an object in R2 is fetchable
via a signed URL with NO local file present — i.e. it survives a redeploy that
wipes the local Docker volumes.
"""
import urllib.request
import pytest

import storage  # importable via conftest sys.path insert of ../../python

requires_r2 = pytest.mark.skipif(
    not storage.is_configured(),
    reason="STORAGE_* not configured — set R2 creds to run object-storage tests",
)


def test_build_key_format():
    assert storage.build_key("T", "J", "image", "a.png") == "tenants/T/jobs/J/image/a.png"
    # leading slashes on the filename are stripped
    assert storage.build_key("T", "J", "video", "/x.mp4") == "tenants/T/jobs/J/video/x.mp4"


@requires_r2
def test_upload_exists_download_delete():
    key = storage.build_key("test-tenant", "test-job", "other", "e2e.txt")
    storage.upload_bytes(key, b"e2e-bytes", "text/plain")
    try:
        assert storage.exists(key) is True
        assert storage.download_bytes(key) == b"e2e-bytes"
    finally:
        storage.delete(key)
    assert storage.exists(key) is False


@requires_r2
def test_durability_signed_url_no_local_file():
    """Object in R2 is retrievable via signed URL with no local file at all."""
    key = storage.build_key("test-tenant", "test-job", "video", "durable.bin")
    payload = b"survives-redeploy"
    storage.upload_bytes(key, payload, "application/octet-stream")
    try:
        url = storage.signed_url(key, 600)
        assert url.startswith("https://")
        with urllib.request.urlopen(url) as r:
            assert r.read() == payload
    finally:
        storage.delete(key)
    assert storage.exists(key) is False
