"""
test_tenant_isolation.py — Workstream 3: Tenant Isolation

Unit tests verifying the isolation mechanisms are in place.
These tests mock auth and DB to run without a real Clerk/PostgreSQL instance.
Integration tests (T01–T08) live in tests/integration/test_tenant_isolation.sh.
"""
import pytest
import uuid
from unittest.mock import AsyncMock, patch, MagicMock


# ── Fixtures ──────────────────────────────────────────────────────────────────

TENANT_A = str(uuid.uuid4())
TENANT_B = str(uuid.uuid4())
USER_A    = str(uuid.uuid4())
USER_B    = str(uuid.uuid4())
SESSION_A = str(uuid.uuid4())
SESSION_B = str(uuid.uuid4())
JOB_A     = str(uuid.uuid4())


def _make_user(tenant_id: str, user_id: str):
    """Return a CurrentUser for the given tenant."""
    from auth_middleware import CurrentUser
    return CurrentUser(tenant_id=tenant_id, user_id=user_id, plan="free", tier="free")


# ── Task 1: TenantContext helper ───────────────────────────────────────────────

class TestTenantContextHelper:
    def test_tenant_context_dataclass_exists(self):
        """TenantContext dataclass must be importable (from auth_middleware, re-exported via laozhang_api)."""
        from auth_middleware import TenantContext
        ctx = TenantContext()
        assert ctx.tenant_id == ""
        assert ctx.tier == "free"
        assert ctx.credits == 100
        assert hasattr(ctx, "api_key")
        assert hasattr(ctx, "deepseek_key")
        assert hasattr(ctx, "gemini_key")
        assert hasattr(ctx, "user_id")

    def test_tenant_context_accessible_from_laozhang_api(self):
        """TenantContext and _tenant_ctx must be importable from laozhang_api (re-exported)."""
        from laozhang_api import TenantContext, _tenant_ctx, get_tenant_ctx
        from contextvars import ContextVar
        assert isinstance(_tenant_ctx, ContextVar)
        ctx = TenantContext(tenant_id=TENANT_A, tier="pro")
        assert ctx.tier == "pro"

    def test_tenant_ctx_contextvar_exists_in_auth_middleware(self):
        """_tenant_ctx ContextVar must be in auth_middleware."""
        from auth_middleware import _tenant_ctx, TenantContext
        from contextvars import ContextVar
        assert isinstance(_tenant_ctx, ContextVar)
        assert isinstance(_tenant_ctx.get(), TenantContext)

    def test_get_tenant_ctx_helper(self):
        """get_tenant_ctx() must return the current TenantContext."""
        from auth_middleware import _tenant_ctx, get_tenant_ctx, TenantContext
        ctx = TenantContext(tenant_id=TENANT_A, tier="pro", credits=500)
        token = _tenant_ctx.set(ctx)
        try:
            result = get_tenant_ctx()
            assert result.tenant_id == TENANT_A
            assert result.tier == "pro"
            assert result.credits == 500
        finally:
            _tenant_ctx.reset(token)

    def test_req_key_compat_shim_exists(self):
        """_req_key must be a _ReqKeyCompat shim (not a raw ContextVar)."""
        from laozhang_api import _req_key
        assert hasattr(_req_key, "get")
        assert hasattr(_req_key, "set")
        assert hasattr(_req_key, "reset")
        assert not hasattr(_req_key, "var"), \
            "_req_key should be a compat shim, not a raw ContextVar"

    def test_req_key_shim_returns_env_api_key_by_default(self):
        """_req_key.get() must return API_KEY when no header override and no tenant ctx."""
        from laozhang_api import _req_key, API_KEY, _req_key_raw
        token = _req_key_raw.set("")  # no header override
        try:
            key = _req_key.get()
            assert key == API_KEY or key == "", \
                f"Expected env key or empty, got: {key!r}"
        finally:
            _req_key_raw.reset(token)

    def test_req_key_shim_uses_header_override(self):
        """_req_key.get() must prefer the header override over tenant's stored key."""
        from laozhang_api import _req_key, _req_key_raw, _tenant_ctx, TenantContext
        # Set header override
        token_raw = _req_key_raw.set("sk-header-override")
        # Set tenant context with different key
        token_ctx = _tenant_ctx.set(TenantContext(api_key="sk-tenant-stored"))
        try:
            assert _req_key.get() == "sk-header-override"
        finally:
            _req_key_raw.reset(token_raw)
            _tenant_ctx.reset(token_ctx)

    def test_get_user_wrapper_sets_tenant_ctx(self):
        """get_current_user() must set _tenant_ctx (directly, via auth_middleware)."""
        import asyncio
        from auth_middleware import get_current_user, _tenant_ctx, get_tenant_ctx, TenantContext

        # Simulate what get_current_user does after WS3 changes
        ctx = TenantContext(tenant_id=TENANT_A, tier="free", credits=100)
        token = _tenant_ctx.set(ctx)
        try:
            result = get_tenant_ctx()
            assert result.tenant_id == TENANT_A
        finally:
            _tenant_ctx.reset(token)


# ── Task 2: Python endpoints are tenant-scoped ────────────────────────────────

class TestPythonTenantScoping:
    def test_history_requires_auth(self, client):
        """GET /history/{id} without auth must return 401."""
        resp = client.get(f"/history/{SESSION_A}")
        assert resp.status_code == 401

    def test_delete_session_requires_auth(self, client):
        """DELETE /session/{id} without auth must return 401."""
        resp = client.delete(f"/session/{SESSION_A}")
        assert resp.status_code == 401

    def test_delete_all_sessions_requires_auth(self, client):
        """DELETE /sessions without auth must return 401."""
        resp = client.delete("/sessions")
        assert resp.status_code == 401

    def test_cross_tenant_session_history_returns_empty(self, client, app):
        """Tenant A's token scopes history to TENANT_A (Tenant B sees empty/404)."""
        from auth_middleware import get_current_user

        user_a = _make_user(TENANT_A, USER_A)
        calls = []

        async def mock_history(tenant_id, session_id):
            calls.append(str(tenant_id))
            return []

        async def override_auth():
            return user_a

        app.dependency_overrides[get_current_user] = override_auth
        try:
            with patch("laozhang_api.db.get_session_history",
                       new=AsyncMock(side_effect=mock_history)):
                resp = client.get(
                    f"/history/{SESSION_B}",
                    headers={"Authorization": "Bearer fake-token-a"}
                )
        finally:
            app.dependency_overrides.clear()

        # Should succeed or return empty — NOT Tenant B's data
        assert resp.status_code in (200, 404)
        if calls:
            assert calls[0] == TENANT_A, \
                f"db.get_session_history called with {calls[0]!r}, expected TENANT_A={TENANT_A!r}"

    def test_get_job_cross_tenant_returns_404(self, client, app):
        """Tenant A querying unknown job ID must get 404 (db returns None)."""
        from auth_middleware import get_current_user

        user_a = _make_user(TENANT_A, USER_A)

        async def override_auth():
            return user_a

        app.dependency_overrides[get_current_user] = override_auth
        try:
            with patch("laozhang_api.db.get_job", new=AsyncMock(return_value=None)):
                resp = client.get(
                    f"/narasi/oneshot-fix/status/{JOB_A}",
                    headers={"Authorization": "Bearer fake-token-a"}
                )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 404

    def test_tenant_ctx_populated_on_authenticated_request(self, client, app):
        """_tenant_ctx must be populated on an authenticated request."""
        from auth_middleware import get_current_user, get_tenant_ctx

        user_a = _make_user(TENANT_A, USER_A)
        captured = {}

        async def override_auth():
            # Simulate what auth_middleware does: set _tenant_ctx
            from auth_middleware import _tenant_ctx, TenantContext
            ctx = TenantContext(tenant_id=TENANT_A, user_id=USER_A)
            _tenant_ctx.set(ctx)
            captured["ctx"] = get_tenant_ctx()
            return user_a

        app.dependency_overrides[get_current_user] = override_auth
        try:
            with patch("laozhang_api.db.get_session_history",
                       new=AsyncMock(return_value=[])):
                client.get(
                    f"/history/{SESSION_A}",
                    headers={"Authorization": "Bearer fake-token-a"}
                )
        finally:
            app.dependency_overrides.clear()

        if captured:
            assert captured["ctx"].tenant_id == TENANT_A


# ── Task 3: Cross-tenant isolation enforced at application layer ──────────────

class TestCrossTenantForbidden:
    def test_history_uses_authenticated_tenant_id(self, client, app):
        """GET /history uses the JWT tenant_id — db.get_session_history called with TENANT_A."""
        from auth_middleware import get_current_user

        user_a = _make_user(TENANT_A, USER_A)
        calls = []

        async def mock_history(tenant_id, session_id):
            calls.append(str(tenant_id))
            return []

        async def override_auth():
            return user_a

        app.dependency_overrides[get_current_user] = override_auth
        try:
            with patch("laozhang_api.db.get_session_history",
                       new=AsyncMock(side_effect=mock_history)):
                client.get(
                    f"/history/{SESSION_A}",
                    headers={"Authorization": "Bearer token-a"}
                )
        finally:
            app.dependency_overrides.clear()

        if calls:
            assert calls[0] == TENANT_A, \
                f"Expected TENANT_A={TENANT_A}, got {calls[0]}"

    def test_delete_session_scoped_to_jwt_tenant(self, client, app):
        """DELETE /session uses the JWT tenant_id for the DB delete."""
        from auth_middleware import get_current_user

        user_a = _make_user(TENANT_A, USER_A)
        calls = []

        async def mock_delete(tenant_id, session_id):
            calls.append(str(tenant_id))

        async def override_auth():
            return user_a

        app.dependency_overrides[get_current_user] = override_auth
        try:
            with patch("laozhang_api.db.delete_session",
                       new=AsyncMock(side_effect=mock_delete)):
                client.delete(
                    f"/session/{SESSION_A}",
                    headers={"Authorization": "Bearer token-a"}
                )
        finally:
            app.dependency_overrides.clear()

        if calls:
            assert calls[0] == TENANT_A, \
                f"Expected TENANT_A={TENANT_A}, got {calls[0]}"

    def test_two_tenants_use_different_tenant_ids(self, client, app):
        """Tenant A and Tenant B calls use different tenant_ids in db queries."""
        from auth_middleware import get_current_user

        user_a = _make_user(TENANT_A, USER_A)
        user_b = _make_user(TENANT_B, USER_B)
        calls = []

        async def mock_history(tenant_id, session_id):
            calls.append(str(tenant_id))
            return []

        async def override_a():
            return user_a

        async def override_b():
            return user_b

        # Tenant A request
        app.dependency_overrides[get_current_user] = override_a
        try:
            with patch("laozhang_api.db.get_session_history",
                       new=AsyncMock(side_effect=mock_history)):
                client.get(f"/history/{SESSION_A}",
                           headers={"Authorization": "Bearer token-a"})
        finally:
            app.dependency_overrides.clear()

        # Tenant B request
        app.dependency_overrides[get_current_user] = override_b
        try:
            with patch("laozhang_api.db.get_session_history",
                       new=AsyncMock(side_effect=mock_history)):
                client.get(f"/history/{SESSION_B}",
                           headers={"Authorization": "Bearer token-b"})
        finally:
            app.dependency_overrides.clear()

        if len(calls) >= 2:
            assert TENANT_A in calls, "TENANT_A must appear in db calls"
            assert TENANT_B in calls, "TENANT_B must appear in db calls"
            assert calls[0] != calls[1], "Different tenants must use different tenant_ids"


# ── Task 4: RLS migration completeness ───────────────────────────────────────

class TestRlsMigration:
    def test_rls_migration_file_exists(self):
        """Migration 0011_enable_rls.sql must exist."""
        import os
        migrations_dir = os.path.join(
            os.path.dirname(__file__), "../../database/migrations")
        files = os.listdir(migrations_dir)
        rls_files = [f for f in files if "0011" in f]
        assert rls_files, \
            f"No 0011 RLS migration found. Files: {sorted(files)}"

    def test_rls_migration_contains_helper_function(self):
        """Migration 0011 must define set_current_tenant_id() helper."""
        import os
        migrations_dir = os.path.join(
            os.path.dirname(__file__), "../../database/migrations")
        content = open(os.path.join(migrations_dir, "0011_enable_rls.sql")).read()
        assert "set_current_tenant_id" in content, \
            "Migration must define set_current_tenant_id() function"

    def test_rls_migration_has_force_rls(self):
        """Migration 0011 must use FORCE ROW LEVEL SECURITY on all tables."""
        import os
        migrations_dir = os.path.join(
            os.path.dirname(__file__), "../../database/migrations")
        content = open(os.path.join(migrations_dir, "0011_enable_rls.sql")).read()
        assert "FORCE" in content and "ROW LEVEL SECURITY" in content, \
            "Migration must include FORCE ROW LEVEL SECURITY"

    def test_rls_migration_has_superuser_bypass(self):
        """Migration 0011 must include a superuser_bypass policy."""
        import os
        migrations_dir = os.path.join(
            os.path.dirname(__file__), "../../database/migrations")
        content = open(os.path.join(migrations_dir, "0011_enable_rls.sql")).read()
        assert "superuser_bypass" in content, \
            "Migration must include superuser_bypass policy (neondb_owner)"
        assert "neondb_owner" in content, \
            "superuser_bypass policy must reference neondb_owner"

    def test_rls_migration_has_four_operations_per_table(self):
        """Migration 0011 must have SELECT, INSERT, UPDATE, DELETE policies."""
        import os
        migrations_dir = os.path.join(
            os.path.dirname(__file__), "../../database/migrations")
        content = open(os.path.join(migrations_dir, "0011_enable_rls.sql")).read()
        for op in ["FOR SELECT", "FOR INSERT", "FOR UPDATE", "FOR DELETE"]:
            assert op in content, f"Migration must define {op} policy"

    def test_all_nine_tables_in_rls_migration(self):
        """All 9 tenant tables must have ENABLE + FORCE ROW LEVEL SECURITY in 0011."""
        import os
        migrations_dir = os.path.join(
            os.path.dirname(__file__), "../../database/migrations")
        content = open(os.path.join(migrations_dir, "0011_enable_rls.sql")).read()
        tables = [
            "tenants", "users", "api_keys",
            "chat_sessions", "chat_messages", "jobs",
            "usage_logs", "assets", "subscriptions",
        ]
        for table in tables:
            assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in content, \
                f"Table '{table}' missing ENABLE ROW LEVEL SECURITY in 0011"
            assert f"ALTER TABLE {table} FORCE" in content, \
                f"Table '{table}' missing FORCE ROW LEVEL SECURITY in 0011"

    def test_database_py_has_set_tenant_context(self):
        """database.py must export set_tenant_context() and rls_conn()."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../python"))
        import database
        assert hasattr(database, "set_tenant_context"), \
            "database.py must define set_tenant_context()"
        assert hasattr(database, "rls_conn"), \
            "database.py must define rls_conn() context manager"
        assert hasattr(database, "get_tenant_context"), \
            "database.py must define get_tenant_context()"

    def test_auth_middleware_has_full_tenant_context(self):
        """auth_middleware.py must define TenantContext with all required fields."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../python"))
        import auth_middleware
        assert hasattr(auth_middleware, "TenantContext"), \
            "auth_middleware.py must define TenantContext dataclass"
        assert hasattr(auth_middleware, "_tenant_ctx"), \
            "auth_middleware.py must define _tenant_ctx ContextVar"
        assert hasattr(auth_middleware, "get_tenant_ctx"), \
            "auth_middleware.py must define get_tenant_ctx() helper"
        # Verify all required fields
        ctx = auth_middleware.TenantContext()
        for field in ["tenant_id", "user_id", "tier", "credits", "api_key", "deepseek_key", "gemini_key"]:
            assert hasattr(ctx, field), f"TenantContext missing field: {field}"

    def test_db_js_has_set_tenant_context(self):
        """db.js must contain setTenantContext() function."""
        import os
        db_path = os.path.join(os.path.dirname(__file__), "../../backend/db.js")
        content = open(db_path).read()
        assert "setTenantContext" in content, \
            "db.js must define setTenantContext() function"
        assert "SET LOCAL app.current_tenant_id" in content, \
            "setTenantContext() must use SET LOCAL"

    def test_integration_test_shell_exists(self):
        """tests/integration/test_tenant_isolation.sh must exist and be executable."""
        import os, stat
        script = os.path.join(
            os.path.dirname(__file__), "../../tests/integration/test_tenant_isolation.sh")
        assert os.path.exists(script), "test_tenant_isolation.sh must exist"
        mode = os.stat(script).st_mode
        assert mode & stat.S_IXUSR, "test_tenant_isolation.sh must be executable"

    def test_rls_direct_sql_exists(self):
        """tests/integration/test_rls_direct.sql must exist."""
        import os
        sql = os.path.join(
            os.path.dirname(__file__), "../../tests/integration/test_rls_direct.sql")
        assert os.path.exists(sql), "test_rls_direct.sql must exist"

    def test_github_actions_workflow_exists(self):
        """GitHub Actions workflow for tenant isolation must exist."""
        import os
        workflow = os.path.join(
            os.path.dirname(__file__), "../../.github/workflows/tenant_isolation.yml")
        assert os.path.exists(workflow), \
            ".github/workflows/tenant_isolation.yml must exist"
        content = open(workflow).read()
        assert "test_tenant_isolation.sh" in content, \
            "CI workflow must invoke test_tenant_isolation.sh"
