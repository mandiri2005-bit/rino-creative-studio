"""
Dalang v2 — Slice 0 IDOR fix on /narasi/rate (hermetic).

The prod role app_user is NOBYPASSRLS (migration 0016) so the tenant_isolation RLS policy
applies; the explicit tenant-owned SQL predicate is kept as defense-in-depth ALONGSIDE it
(never removed). Two guards:
  1. Source inspection of database.save_approval / save_approval_all — the tenant scope
     is present in the SQL (matches the repo's RLS-migration test convention). A revert
     of the fix fails these.
  2. Route behavior — /narasi/rate stamps the write with the AUTHENTICATED tenant (never
     a client value) and surfaces "not found" (no write) when the DB denies ownership.
"""
import uuid
import inspect
import pytest

import laozhang_api
import database


TENANT_A = str(uuid.uuid4())
USER_A = str(uuid.uuid4())


def _make_user(tenant_id=TENANT_A, user_id=USER_A):
    from auth_middleware import CurrentUser
    return CurrentUser(tenant_id=tenant_id, user_id=user_id, plan="free", tier="free")


# ── 1. SQL is tenant-scoped ──────────────────────────────────────────────────

class TestApprovalSqlScoped:
    def test_save_approval_tenant_scoped(self):
        src = inspect.getsource(database.save_approval)
        assert "nc.tenant_id=$1" in src, "INSERT must be gated on chapter ownership"
        assert "AND tenant_id=$3" in src, "UPDATE must carry an explicit tenant guard"
        assert "RETURNING id" in src, "INSERT…SELECT must RETURN so cross-tenant → no row"

    def test_save_approval_all_tenant_scoped(self):
        src = inspect.getsource(database.save_approval_all)
        assert "nc.tenant_id=$1" in src, "INSERT must be tenant-scoped (defense-in-depth)"
        assert "AND tenant_id=$3" in src, "UPDATE must carry an explicit tenant guard"


# ── 2. Route stamps the authenticated tenant + denies cross-tenant ───────────

class TestRateRouteTenantStamping:
    def _run(self, client, app, save_approval_impl):
        from auth_middleware import get_current_user
        user_a = _make_user()

        async def _dep():
            return user_a

        async def _fake_resolve(_t, _u):
            return USER_A

        app.dependency_overrides[get_current_user] = _dep
        try:
            _orig_resolve = laozhang_api._resolve_user_uuid
            _orig_save = laozhang_api.db.save_approval
            laozhang_api._resolve_user_uuid = _fake_resolve
            laozhang_api.db.save_approval = save_approval_impl
            try:
                return client.post(
                    "/narasi/rate",
                    json={"chapter_id": "victim-tenant-chapter-uuid", "rating": 5},
                    headers={"Authorization": "Bearer a"})
            finally:
                laozhang_api._resolve_user_uuid = _orig_resolve
                laozhang_api.db.save_approval = _orig_save
        finally:
            app.dependency_overrides.clear()

    def test_cross_tenant_write_denied_returns_not_found(self, client, app):
        seen = []

        async def _denied(tenant_id, user_id, chapter_id, rating):
            # Simulate the tenant-scoped SQL finding no owned chapter → "" (no write).
            seen.append(str(tenant_id))
            return ""

        resp = self._run(client, app, _denied)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("ok") is False
        assert "not found" in (body.get("error") or "")
        # The write was attempted with the AUTHENTICATED tenant, not any client value.
        assert seen and seen[0] == TENANT_A

    def test_same_tenant_write_succeeds(self, client, app):
        async def _ok(tenant_id, user_id, chapter_id, rating):
            return "approval-abc123"

        resp = self._run(client, app, _ok)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("ok") is True
        assert body.get("approval_id") == "approval-abc123"
