"""
Dalang v2 — Slice 0 prod Clerk boot-check (hermetic).

_require_prod_auth() must refuse to boot when NODE_ENV=production AND CLERK_JWT_ISSUER is
unset — the state where auth_middleware.verify_clerk_jwt authenticates ANY bearer token
as the fixed dev tenant. It must be a no-op in dev/staging or when the issuer is set.

Also asserts (by source inspection, matching the repo's RLS-test convention) that the
lifespan boot hook actually invokes the guard, gated behind the admission flag.
"""
import inspect
import pytest
import laozhang_api
import auth_middleware


class TestRequireProdAuth:
    def test_prod_unset_issuer_refuses_boot(self, monkeypatch):
        monkeypatch.setenv("NODE_ENV", "production")
        monkeypatch.setattr(auth_middleware, "CLERK_JWT_ISSUER", "")
        with pytest.raises(RuntimeError) as ei:
            laozhang_api._require_prod_auth()
        assert "CLERK_JWT_ISSUER" in str(ei.value)

    def test_prod_with_issuer_ok(self, monkeypatch):
        monkeypatch.setenv("NODE_ENV", "production")
        monkeypatch.setattr(auth_middleware, "CLERK_JWT_ISSUER", "https://clerk.example.com")
        laozhang_api._require_prod_auth()  # must not raise

    def test_dev_unset_issuer_ok(self, monkeypatch):
        monkeypatch.setenv("NODE_ENV", "development")
        monkeypatch.setattr(auth_middleware, "CLERK_JWT_ISSUER", "")
        laozhang_api._require_prod_auth()  # dev is allowed to run without Clerk

    def test_staging_unset_issuer_ok(self, monkeypatch):
        monkeypatch.setenv("NODE_ENV", "staging")
        monkeypatch.setattr(auth_middleware, "CLERK_JWT_ISSUER", "")
        laozhang_api._require_prod_auth()  # only 'production' is guarded

    def test_default_env_unset_issuer_ok(self, monkeypatch):
        monkeypatch.delenv("NODE_ENV", raising=False)  # defaults to 'development'
        monkeypatch.setattr(auth_middleware, "CLERK_JWT_ISSUER", "")
        laozhang_api._require_prod_auth()


class TestLifespanWiring:
    def test_lifespan_invokes_guard_gated_by_flag(self):
        src = inspect.getsource(laozhang_api.lifespan)
        assert "_require_prod_auth" in src, "lifespan must call the boot guard"
        assert "_dalang_admission_enabled" in src, "boot guard must be flag-gated in lifespan"
