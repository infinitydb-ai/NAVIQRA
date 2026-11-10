"""Real PostgreSQL contract tests; enabled in CI by NAVIQRA_TEST_POSTGRES_DSN."""
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest

from naviqra.database import Database
from naviqra.platform.core import PlatformStore
from naviqra.store import SQLiteStore

DSN = os.getenv("NAVIQRA_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="PostgreSQL integration DSN not configured")


def _get_app_dsn(dsn: str, password: str = "") -> str:
    parsed = urlsplit(dsn)
    cred = "naviqra_app" if not password else f"naviqra_app:{password}"
    netloc = f"{cred}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def test_postgres_migration_and_tenant_store_roundtrip():
    assert DSN
    migration = Path(__file__).parents[1] / "migrations" / "0001_postgres_foundation.sql"
    database = Database(DSN)
    with database.connect() as conn:
        conn.executescript(migration.read_text(encoding="utf-8"))

    legacy_store = SQLiteStore(DSN, initialize=False)
    platform_store = PlatformStore(DSN, initialize=False)
    legacy_store.check()
    platform_store.check()

    tenant = f"ci-{uuid4()}"
    traveler = f"traveler-{uuid4()}"
    platform_store.put_memory(tenant, traveler, "profile", "seat", "aisle")
    assert platform_store.get_memories(tenant, traveler)[0]["value"] == "aisle"

    node_a = platform_store.add_node(tenant, "traveler", "Traveler", {})
    node_b = platform_store.add_node(tenant, "destination", "Kyoto", {})
    platform_store.add_edge(tenant, node_a["id"], "interested_in", node_b["id"])
    assert len(platform_store.graph(tenant)["edges"]) == 1

    platform_store.vote(tenant, "group-1", "member-1", "candidate-a", 0.9)
    assert platform_store.consensus(tenant, "group-1")["ranked_candidates"][0]["candidate_id"] == "candidate-a"

    platform_store.audit(tenant, "ci", "test.write", "memory", traveler, {})
    assert platform_store.audit_events(tenant)[0]["action"] == "test.write"
    assert platform_store.delete_traveler(tenant, traveler)["memory_records_deleted"] == 1


def test_postgres_recovery_lifecycle_and_migration_upgrade():
    assert DSN
    mig_0001 = Path(__file__).parents[1] / "migrations" / "0001_postgres_foundation.sql"
    mig_0002 = Path(__file__).parents[1] / "migrations" / "0002_recovery_lifecycle.sql"
    mig_0003 = Path(__file__).parents[1] / "migrations" / "0003_row_level_security.sql"
    database = Database(DSN)
    with database.connect() as conn:
        conn.executescript(mig_0001.read_text(encoding="utf-8"))
        # Execute migration 0002 twice to verify rerun idempotency
        conn.executescript(mig_0002.read_text(encoding="utf-8"))
        conn.executescript(mig_0002.read_text(encoding="utf-8"))
        conn.executescript(mig_0003.read_text(encoding="utf-8"))

    platform_store = PlatformStore(DSN, initialize=False)
    tenant = f"ci-rec-{uuid4()}"
    options = [
        {"id": "alt-pg-1", "type": "flight", "price": 400.0, "currency": "USD"},
        {"id": "alt-pg-2", "type": "flight", "price": 450.0, "currency": "USD"}
    ]
    case = platform_store.create_recovery(tenant, "trip-pg-1", "weather delay", options, {"pnr": "PG123"})
    assert case["status"] == "awaiting_selection"
    assert case["context"]["pnr"] == "PG123"

    # Select
    selected = platform_store.select_recovery_option(tenant, case["case_id"], "alt-pg-1", "prefer morning")
    assert selected["selected_option"]["id"] == "alt-pg-1"

    # Approve
    approved = platform_store.decide_recovery(tenant, case["case_id"], True, "pg-auditor", "approved via policy")
    assert approved["status"] == "approved"

    # Execute
    resolved = platform_store.execute_recovery(tenant, case["case_id"], "pg-idempotency-key-01")
    assert resolved["status"] == "resolved"
    assert resolved["resolution_outcome"]["confirmation_id"].startswith("SIM-REC-")

    # Replay idempotency
    audit_events_before_replay = platform_store.recovery_audit(tenant, case["case_id"])
    replay = platform_store.execute_recovery(tenant, case["case_id"], "pg-idempotency-key-01")
    audit_events_after_replay = platform_store.recovery_audit(tenant, case["case_id"])

    # Replay must return the original confirmation ID
    assert replay["resolution_outcome"]["confirmation_id"] == resolved["resolution_outcome"]["confirmation_id"]

    # Replay must not create additional audit events
    assert len(audit_events_after_replay) == len(audit_events_before_replay)

    # Verify audit events
    audit_events = platform_store.recovery_audit(tenant, case["case_id"])
    assert len(audit_events) == 5
    assert [e["action"] for e in audit_events] == [
        "recovery.created",
        "recovery.option_selected",
        "recovery.approved",
        "recovery.execution_started",
        "recovery.resolved",
    ]
    assert sum(1 for e in audit_events if e["action"] == "recovery.execution_started") == 1
    assert sum(1 for e in audit_events if e["action"] == "recovery.resolved") == 1


def test_postgres_row_level_security_enforcement():
    """Verify database-enforced Row-Level Security (RLS) across all security criteria."""
    assert DSN
    mig_0001 = Path(__file__).parents[1] / "migrations" / "0001_postgres_foundation.sql"
    mig_0002 = Path(__file__).parents[1] / "migrations" / "0002_recovery_lifecycle.sql"
    mig_0003 = Path(__file__).parents[1] / "migrations" / "0003_row_level_security.sql"

    # Migration rerun safety (0003 executed multiple times consecutively)
    admin_db = Database(DSN)
    with admin_db.connect() as conn:
        conn.executescript(mig_0001.read_text(encoding="utf-8"))
        conn.executescript(mig_0002.read_text(encoding="utf-8"))
        conn.executescript(mig_0003.read_text(encoding="utf-8"))
        conn.executescript(mig_0003.read_text(encoding="utf-8"))
        conn.executescript(mig_0003.read_text(encoding="utf-8"))

    # Provision ephemeral test password for naviqra_app runtime role (no hardcoded password in migration)
    test_app_password = f"ci-test-pw-{uuid4().hex}"
    with admin_db.connect() as conn:
        conn.execute(f"ALTER ROLE naviqra_app WITH LOGIN PASSWORD '{test_app_password}'")

    # Verify Table Ownership: runtime role must NOT be table owner
    with admin_db.connect() as conn:
        owner_row = conn.execute("SELECT tableowner FROM pg_tables WHERE schemaname = 'public' AND tablename = 'recovery_cases'").fetchone()
        assert owner_row["tableowner"] != "naviqra_app"

    # Verify Role Privileges: naviqra_app is NOT superuser, has NO BYPASSRLS, cannot create roles or databases
    with admin_db.connect() as conn:
        app_role = conn.execute("SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = 'naviqra_app'").fetchone()
        assert app_role["rolsuper"] is False
        assert app_role["rolbypassrls"] is False
        assert app_role["rolcreaterole"] is False
        assert app_role["rolcreatedb"] is False

    # FORCE ROW LEVEL SECURITY verification on all 7 tenant tables
    with admin_db.connect() as conn:
        tables = [
            "t_memory", "graph_nodes", "graph_edges", "group_votes",
            "agent_runs", "audit_events", "recovery_cases"
        ]
        placeholders = ",".join(["?"] * len(tables))
        rows = conn.execute(
            f"""SELECT relname, relrowsecurity, relforcerowsecurity
                FROM pg_class
                WHERE relname IN ({placeholders})""",
            tuple(tables)
        ).fetchall()
        assert len(rows) == 7
        for r in rows:
            assert r["relrowsecurity"] is True, f"RLS not enabled on {r['relname']}"
            assert r["relforcerowsecurity"] is True, f"RLS not forced on {r['relname']}"

    app_dsn = _get_app_dsn(DSN, test_app_password)
    app_db = Database(app_dsn)
    store = PlatformStore(app_dsn, initialize=False)

    # Runtime role least-privilege boundary checks:
    # 1. naviqra_app cannot alter or drop RLS policies
    with app_db.connect() as conn:
        with pytest.raises(Exception) as exc:
            conn.execute("DROP POLICY tenant_isolation_policy ON recovery_cases")
        assert "must be owner" in str(exc.value).lower() or "permission denied" in str(exc.value).lower()
        with pytest.raises(Exception) as exc:
            conn.execute("CREATE POLICY rogue ON recovery_cases FOR ALL USING (true)")
        assert "must be owner" in str(exc.value).lower() or "permission denied" in str(exc.value).lower()

    # 2. naviqra_app cannot drop or alter tables
    with app_db.connect() as conn:
        with pytest.raises(Exception) as exc:
            conn.execute("DROP TABLE recovery_cases")
        assert "must be owner" in str(exc.value).lower() or "permission denied" in str(exc.value).lower()
        with pytest.raises(Exception) as exc:
            conn.execute("ALTER TABLE recovery_cases ADD COLUMN rogue TEXT")
        assert "must be owner" in str(exc.value).lower() or "permission denied" in str(exc.value).lower()

    # 3. naviqra_app cannot create roles
    with app_db.connect() as conn:
        with pytest.raises(Exception) as exc:
            conn.execute("CREATE ROLE rogue_role")
        assert "permission denied" in str(exc.value).lower()

    tenant_a = f"tenant-a-{uuid4()}"
    tenant_b = f"tenant-b-{uuid4()}"

    # Seed Tenant A and Tenant B data via store (using app role)
    options_a = [{"id": "opt-a", "type": "flight", "price": 300.0, "currency": "USD"}]
    options_b = [{"id": "opt-b", "type": "rail", "price": 120.0, "currency": "USD"}]
    case_a = store.create_recovery(tenant_a, "trip-a", "cancelled flight", options_a)
    case_b = store.create_recovery(tenant_b, "trip-b", "delayed rail", options_b)

    store.put_memory(tenant_a, "traveler-a", "profile", "seat", "window")
    store.put_memory(tenant_b, "traveler-b", "profile", "seat", "aisle")

    node_a1 = store.add_node(tenant_a, "traveler", "Alice", {})
    node_a2 = store.add_node(tenant_a, "destination", "Tokyo", {})
    store.add_edge(tenant_a, node_a1["id"], "visiting", node_a2["id"])
    node_b1 = store.add_node(tenant_b, "traveler", "Bob", {})

    store.vote(tenant_a, "group-a", "member-a1", "choice-a", 1.0)
    store.vote(tenant_b, "group-b", "member-b1", "choice-b", 0.8)

    store.save_run(tenant_a, f"run-a-{uuid4()}", "planning", "succeeded", {"plan": "A"})
    store.save_run(tenant_b, f"run-b-{uuid4()}", "planning", "succeeded", {"plan": "B"})

    # Criterion 1: Cross-tenant reads: Tenant A cannot see Tenant B's recovery cases
    assert store.get_recovery(tenant_a, case_b["case_id"]) is None
    with app_db.connect(tenant=tenant_a) as conn:
        cases = conn.execute("SELECT * FROM recovery_cases").fetchall()
        assert all(c["tenant"] == tenant_a for c in cases)
        assert not any(c["case_id"] == case_b["case_id"] for c in cases)

    # Criterion 2: Cross-tenant updates: Tenant A cannot update Tenant B records
    with app_db.connect(tenant=tenant_a) as conn:
        cur1 = conn.execute("UPDATE recovery_cases SET disruption='tampered' WHERE case_id=?", (case_b["case_id"],))
        assert cur1.rowcount == 0
        cur2 = conn.execute("UPDATE t_memory SET value='\"tampered\"' WHERE traveler=?", ("traveler-b",))
        assert cur2.rowcount == 0

    assert store.get_recovery(tenant_b, case_b["case_id"])["disruption"] == "delayed rail"
    assert store.get_memories(tenant_b, "traveler-b")[0]["value"] == "aisle"

    # Criterion 3: Cross-tenant deletes: Tenant A cannot delete Tenant B data
    with app_db.connect(tenant=tenant_a) as conn:
        cur = conn.execute("DELETE FROM recovery_cases WHERE case_id=?", (case_b["case_id"],))
        assert cur.rowcount == 0
    del_res = store.delete_traveler(tenant_a, "traveler-b")
    assert del_res["memory_records_deleted"] == 0
    assert len(store.get_memories(tenant_b, "traveler-b")) == 1

    # Criterion 4: Audit isolation: Tenant A cannot read Tenant B audit events
    with app_db.connect(tenant=tenant_a) as conn:
        events = conn.execute("SELECT * FROM audit_events").fetchall()
        assert all(e["tenant"] == tenant_a for e in events)
    assert not any(e["metadata"].get("disruption") == "delayed rail" for e in store.audit_events(tenant_a))
    with pytest.raises(Exception):
        store.recovery_audit(tenant_a, case_b["case_id"])

    # Criterion 5: Memory isolation: Tenant A cannot read Tenant B memory tiers
    with app_db.connect(tenant=tenant_a) as conn:
        mems = conn.execute("SELECT * FROM t_memory").fetchall()
        assert all(m["tenant"] == tenant_a for m in mems)
    assert store.get_memories(tenant_a, "traveler-b") == []

    # Criterion 6: Knowledge graph isolation: Tenant A cannot see or connect to Tenant B nodes
    with app_db.connect(tenant=tenant_a) as conn:
        nodes = conn.execute("SELECT * FROM graph_nodes").fetchall()
        assert all(n["tenant"] == tenant_a for n in nodes)
        edges = conn.execute("SELECT * FROM graph_edges").fetchall()
        assert all(e["tenant"] == tenant_a for e in edges)
    with pytest.raises(ValueError, match="both graph endpoints must exist in this tenant"):
        store.add_edge(tenant_a, node_a1["id"], "cross_tenant", node_b1["id"])

    # Criterion 7: Missing tenant context: Connection without tenant receives 0 rows and cannot insert
    with app_db.connect() as conn:
        assert len(conn.execute("SELECT * FROM recovery_cases").fetchall()) == 0
        assert len(conn.execute("SELECT * FROM t_memory").fetchall()) == 0
        assert len(conn.execute("SELECT * FROM graph_nodes").fetchall()) == 0
        assert len(conn.execute("SELECT * FROM audit_events").fetchall()) == 0
        with pytest.raises(Exception) as exc_info:
            conn.execute(
                "INSERT INTO recovery_cases(tenant, case_id, trip_id, disruption, options, status, created_at, updated_at) "
                "VALUES ('unauth', 'c-missing', 'trip', 'd', '[]', 'awaiting_selection', '2026-01-01', '2026-01-01')"
            )
        assert "violates row-level security policy" in str(exc_info.value).lower()

    # Criterion 8: Invalid tenant context: Inserting data with tenant != current_setting is rejected by WITH CHECK
    with app_db.connect(tenant=tenant_a) as conn:
        with pytest.raises(Exception) as exc_info:
            conn.execute(
                "INSERT INTO recovery_cases(tenant, case_id, trip_id, disruption, options, status, created_at, updated_at) "
                "VALUES (?, 'c-invalid', 'trip', 'd', '[]', 'awaiting_selection', '2026-01-01', '2026-01-01')",
                (tenant_b,)
            )
        assert "violates row-level security policy" in str(exc_info.value).lower()

    # Criterion 9: Context reset on COMMIT
    with app_db.connect(tenant=tenant_a) as conn:
        conn.execute("SELECT 1")
    with app_db.connect() as conn:
        cur_val = conn.execute("SELECT NULLIF(current_setting('app.current_tenant', true), '') AS t").fetchone()["t"]
        assert cur_val is None

    # Criterion 10: Context reset on ROLLBACK
    try:
        with app_db.connect(tenant=tenant_a) as conn:
            conn.execute("SELECT 1")
            raise RuntimeError("force_rollback_test")
    except RuntimeError:
        pass
    with app_db.connect() as conn:
        cur_val = conn.execute("SELECT NULLIF(current_setting('app.current_tenant', true), '') AS t").fetchone()["t"]
        assert cur_val is None

    # Criterion 11: Connection reuse: Subsequent queries on the same connection cannot read prior tenant data
    with app_db.connect(tenant=tenant_a) as conn:
        rows_a = conn.execute("SELECT * FROM recovery_cases").fetchall()
        assert len(rows_a) >= 1
    with app_db.connect() as conn:
        rows_none = conn.execute("SELECT * FROM recovery_cases").fetchall()
        assert len(rows_none) == 0

    # Criterion 12: Session Role Switching (SET ROLE naviqra_app) within admin connection
    with admin_db.connect() as conn:
        conn.execute("SET ROLE naviqra_app")
        assert len(conn.execute("SELECT * FROM recovery_cases").fetchall()) == 0
        conn.execute("RESET ROLE")

