"""Real PostgreSQL contract tests; enabled in CI by NAVIQRA_TEST_POSTGRES_DSN."""
import os
from pathlib import Path
from uuid import uuid4

import pytest

from naviqra.database import Database
from naviqra.platform.core import PlatformStore
from naviqra.store import SQLiteStore

DSN = os.getenv("NAVIQRA_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="PostgreSQL integration DSN not configured")


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
    database = Database(DSN)
    with database.connect() as conn:
        conn.executescript(mig_0001.read_text(encoding="utf-8"))
        # Execute migration 0002 twice to verify rerun idempotency
        conn.executescript(mig_0002.read_text(encoding="utf-8"))
        conn.executescript(mig_0002.read_text(encoding="utf-8"))

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

