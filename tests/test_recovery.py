import os
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

os.environ["NAVIQRA_DB"] = str(Path(__file__).parent / "recovery-test.sqlite")
from naviqra.api import app  # noqa: E402
from naviqra import api as api_module  # noqa: E402
from naviqra.platform.core import PlatformStore, AgentRuntime  # noqa: E402
from naviqra.database import Database  # noqa: E402

client = TestClient(app)

TENANT_A = "airline-ops"
HEADERS_A = {"X-Tenant-ID": TENANT_A, "X-Tenant-Key": "local-dev-key"}
TENANT_B = "hotel-partner"
HEADERS_B = {"X-Tenant-ID": TENANT_B, "X-Tenant-Key": "local-dev-key"}


@pytest.fixture(autouse=True)
def isolated_platform(tmp_path, monkeypatch):
    store = PlatformStore(tmp_path / "recovery-platform.sqlite")
    monkeypatch.setattr(api_module, "platform_store", store)
    monkeypatch.setattr(api_module, "agent_runtime", AgentRuntime(store))
    return store


def url_a(path: str) -> str:
    return f"/v2/tenants/{TENANT_A}{path}"


def url_b(path: str) -> str:
    return f"/v2/tenants/{TENANT_B}{path}"


def sample_options():
    return [
        {
            "id": "opt-flight-101",
            "type": "flight",
            "provider": "sandbox",
            "departure": "2026-11-15T09:00:00Z",
            "price": 320.0,
            "currency": "USD"
        },
        {
            "id": "opt-flight-202",
            "type": "flight",
            "provider": "sandbox",
            "departure": "2026-11-15T14:30:00Z",
            "price": 280.0,
            "currency": "USD"
        }
    ]


def test_recovery_creation_lifecycle_initial_state():
    payload = {
        "trip_id": "trip-del-sfo-88",
        "disruption": "Inbound flight cancelled due to severe weather",
        "options": sample_options(),
        "context": {"pnr": "XY789K", "cabin": "business", "vip": True}
    }
    resp = client.post(url_a("/recovery"), headers=HEADERS_A, json=payload)
    assert resp.status_code == 201
    case = resp.json()

    assert isinstance(case["case_id"], str) and len(case["case_id"]) == 36
    assert case["tenant"] == TENANT_A
    assert case["trip_id"] == "trip-del-sfo-88"
    assert case["disruption"] == "Inbound flight cancelled due to severe weather"
    assert case["status"] == "awaiting_selection"
    assert len(case["options"]) == 2
    assert case["selected_option"] is None
    assert case["approval_metadata"] is None
    assert case["execution_metadata"] is None
    assert case["resolution_outcome"] is None
    assert case["idempotency_key"] is None
    assert case["context"]["pnr"] == "XY789K"
    assert case["created_at"] != ""
    assert case["updated_at"] != ""

    # Verify audit event was logged
    audit_resp = client.get(url_a(f"/recovery/{case['case_id']}/audit"), headers=HEADERS_A)
    assert audit_resp.status_code == 200
    events = audit_resp.json()["events"]
    assert len(events) == 1
    assert events[0]["action"] == "recovery.created"
    assert events[0]["resource_id"] == case["case_id"]


def test_recovery_creation_validation_errors():
    # Missing trip_id
    bad1 = {"disruption": "cancelled", "options": sample_options()}
    assert client.post(url_a("/recovery"), headers=HEADERS_A, json=bad1).status_code == 422

    # Missing disruption
    bad2 = {"trip_id": "t-1", "options": sample_options()}
    assert client.post(url_a("/recovery"), headers=HEADERS_A, json=bad2).status_code == 422

    # Empty options list
    bad3 = {"trip_id": "t-1", "disruption": "cancelled", "options": []}
    assert client.post(url_a("/recovery"), headers=HEADERS_A, json=bad3).status_code == 422

    # Option missing 'id'
    bad4 = {"trip_id": "t-1", "disruption": "cancelled", "options": [{"price": 100}]}
    assert client.post(url_a("/recovery"), headers=HEADERS_A, json=bad4).status_code == 422


def test_recovery_get_and_list_endpoints():
    payload = {
        "trip_id": "trip-list-1",
        "disruption": "Delayed 6 hours",
        "options": sample_options()
    }
    case1 = client.post(url_a("/recovery"), headers=HEADERS_A, json=payload).json()
    case2 = client.post(url_a("/recovery"), headers=HEADERS_A, json={
        "trip_id": "trip-list-2",
        "disruption": "Missed connection",
        "options": sample_options()
    }).json()

    # Get single
    get_resp = client.get(url_a(f"/recovery/{case1['case_id']}"), headers=HEADERS_A)
    assert get_resp.status_code == 200
    assert get_resp.json()["case_id"] == case1["case_id"]

    # 404 on nonexistent case
    assert client.get(url_a("/recovery/nonexistent-case-id"), headers=HEADERS_A).status_code == 404

    # List all
    list_resp = client.get(url_a("/recovery"), headers=HEADERS_A)
    assert list_resp.status_code == 200
    cases = list_resp.json()["cases"]
    assert len(cases) >= 2
    case_ids = [c["case_id"] for c in cases]
    assert case1["case_id"] in case_ids
    assert case2["case_id"] in case_ids

    # List with status filter
    filtered = client.get(url_a("/recovery?status=awaiting_selection"), headers=HEADERS_A)
    assert filtered.status_code == 200
    assert len(filtered.json()["cases"]) >= 2

    # Invalid limit
    assert client.get(url_a("/recovery?limit=0"), headers=HEADERS_A).status_code == 422
    assert client.get(url_a("/recovery?limit=999"), headers=HEADERS_A).status_code == 422


def test_recovery_selection_of_alternative():
    case = client.post(url_a("/recovery"), headers=HEADERS_A, json={
        "trip_id": "trip-sel",
        "disruption": "cancelled",
        "options": sample_options()
    }).json()

    # Select non-matching option id -> 409
    bad_sel = client.post(url_a(f"/recovery/{case['case_id']}/select"), headers=HEADERS_A, json={
        "selected_option_id": "non-existent-opt",
        "notes": "not found"
    })
    assert bad_sel.status_code == 409

    # Select valid option
    good_sel = client.post(url_a(f"/recovery/{case['case_id']}/select"), headers=HEADERS_A, json={
        "selected_option_id": "opt-flight-202",
        "notes": "Passenger preferred afternoon departure"
    })
    assert good_sel.status_code == 200
    updated = good_sel.json()
    assert updated["selected_option"]["id"] == "opt-flight-202"
    assert updated["status"] == "awaiting_selection"  # selection does not auto-approve

    # Audit logged
    events = client.get(url_a(f"/recovery/{case['case_id']}/audit"), headers=HEADERS_A).json()["events"]
    actions = [e["action"] for e in events]
    assert "recovery.option_selected" in actions


def test_recovery_approval_gate_and_rejection():
    # Attempt to approve without selecting alternative first -> 409
    case = client.post(url_a("/recovery"), headers=HEADERS_A, json={
        "trip_id": "trip-appr",
        "disruption": "gate change delay",
        "options": sample_options()
    }).json()
    premature_appr = client.post(url_a(f"/recovery/{case['case_id']}/decision"), headers=HEADERS_A, json={
        "approved": True,
        "approver": "ops-manager-dan",
        "notes": "Should fail because no option is selected yet"
    })
    assert premature_appr.status_code == 409

    # Select alternative
    client.post(url_a(f"/recovery/{case['case_id']}/select"), headers=HEADERS_A, json={
        "selected_option_id": "opt-flight-101"
    })

    # Approve
    appr_resp = client.post(url_a(f"/recovery/{case['case_id']}/decision"), headers=HEADERS_A, json={
        "approved": True,
        "approver": "ops-manager-dan",
        "notes": "Authorized travel policy exception"
    })
    assert appr_resp.status_code == 200
    approved_case = appr_resp.json()
    assert approved_case["status"] == "approved"
    assert approved_case["approval_metadata"]["approver"] == "ops-manager-dan"
    assert approved_case["approval_metadata"]["decision"] == "approved"

    # Cannot approve again -> 409
    dup_appr = client.post(url_a(f"/recovery/{case['case_id']}/decision"), headers=HEADERS_A, json={
        "approved": True,
        "approver": "ops-manager-dan"
    })
    assert dup_appr.status_code == 409

    # Test rejection flow on another case
    case_rej = client.post(url_a("/recovery"), headers=HEADERS_A, json={
        "trip_id": "trip-rej",
        "disruption": "cancelled",
        "options": sample_options()
    }).json()
    rej_resp = client.post(url_a(f"/recovery/{case_rej['case_id']}/decision"), headers=HEADERS_A, json={
        "approved": False,
        "approver": "finance-auditor",
        "notes": "Exceeds per-diem recovery threshold"
    })
    assert rej_resp.status_code == 200
    rejected_case = rej_resp.json()
    assert rejected_case["status"] == "rejected"
    assert rejected_case["approval_metadata"]["decision"] == "rejected"

    # Cannot select option or execute a rejected case
    assert client.post(url_a(f"/recovery/{case_rej['case_id']}/select"), headers=HEADERS_A, json={
        "selected_option_id": "opt-flight-101"
    }).status_code == 409
    assert client.post(url_a(f"/recovery/{case_rej['case_id']}/execute"), headers=HEADERS_A, json={
        "idempotency_key": "idem-rej-123456"
    }).status_code == 409


def test_recovery_execution_requires_approval_and_truthful_simulation():
    case = client.post(url_a("/recovery"), headers=HEADERS_A, json={
        "trip_id": "trip-exec",
        "disruption": "cancelled",
        "options": sample_options()
    }).json()

    # Attempt execute while awaiting_selection -> 409
    unapproved = client.post(url_a(f"/recovery/{case['case_id']}/execute"), headers=HEADERS_A, json={
        "idempotency_key": "exec-key-12345678"
    })
    assert unapproved.status_code == 409

    # Select and approve
    client.post(url_a(f"/recovery/{case['case_id']}/select"), headers=HEADERS_A, json={
        "selected_option_id": "opt-flight-101"
    })
    client.post(url_a(f"/recovery/{case['case_id']}/decision"), headers=HEADERS_A, json={
        "approved": True,
        "approver": "chief-dispatcher"
    })

    # Execute successfully
    exec_resp = client.post(url_a(f"/recovery/{case['case_id']}/execute"), headers=HEADERS_A, json={
        "idempotency_key": "exec-key-12345678",
        "provider": "sandbox",
        "details": {"seat_assignment": "12B"}
    })
    assert exec_resp.status_code == 200
    resolved = exec_resp.json()

    assert resolved["status"] == "resolved"
    assert resolved["idempotency_key"] == "exec-key-12345678"
    assert resolved["execution_metadata"]["mode"] == "simulation"
    assert resolved["execution_metadata"]["booking_mode"] == "simulated_non_bookable"

    # Truthful simulation guarantees
    outcome = resolved["resolution_outcome"]
    assert outcome["outcome"] == "simulated_rebooking_confirmed"
    assert outcome["confirmation_id"].startswith("SIM-REC-")
    assert outcome["live_inventory"] is False
    assert outcome["booking_mode"] == "simulated_non_bookable"
    assert "Simulated sandbox execution" in outcome["notice"]


def test_recovery_execution_idempotency_and_key_collision():
    # Setup approved case
    case1 = client.post(url_a("/recovery"), headers=HEADERS_A, json={
        "trip_id": "trip-idem-1", "disruption": "flight delay", "options": sample_options()
    }).json()
    client.post(url_a(f"/recovery/{case1['case_id']}/select"), headers=HEADERS_A, json={"selected_option_id": "opt-flight-101"})
    client.post(url_a(f"/recovery/{case1['case_id']}/decision"), headers=HEADERS_A, json={"approved": True, "approver": "ops-lead"})

    key = "idem-unique-key-9999"
    first_exec = client.post(url_a(f"/recovery/{case1['case_id']}/execute"), headers=HEADERS_A, json={
        "idempotency_key": key
    }).json()
    first_conf = first_exec["resolution_outcome"]["confirmation_id"]

    # Replay on same case returns identical result without re-executing
    replay = client.post(url_a(f"/recovery/{case1['case_id']}/execute"), headers=HEADERS_A, json={
        "idempotency_key": key
    })
    assert replay.status_code == 200
    assert replay.json()["resolution_outcome"]["confirmation_id"] == first_conf

    # Attempting to use the SAME key on another case -> 409
    case2 = client.post(url_a("/recovery"), headers=HEADERS_A, json={
        "trip_id": "trip-idem-2", "disruption": "cancelled", "options": sample_options()
    }).json()
    client.post(url_a(f"/recovery/{case2['case_id']}/select"), headers=HEADERS_A, json={"selected_option_id": "opt-flight-202"})
    client.post(url_a(f"/recovery/{case2['case_id']}/decision"), headers=HEADERS_A, json={"approved": True, "approver": "ops-lead"})

    colliding = client.post(url_a(f"/recovery/{case2['case_id']}/execute"), headers=HEADERS_A, json={
        "idempotency_key": key
    })
    assert colliding.status_code == 409


def test_recovery_simulated_execution_failure():
    case = client.post(url_a("/recovery"), headers=HEADERS_A, json={
        "trip_id": "trip-fail", "disruption": "missed connection", "options": sample_options()
    }).json()
    client.post(url_a(f"/recovery/{case['case_id']}/select"), headers=HEADERS_A, json={"selected_option_id": "opt-flight-101"})
    client.post(url_a(f"/recovery/{case['case_id']}/decision"), headers=HEADERS_A, json={"approved": True, "approver": "ops-lead"})

    fail_exec = client.post(url_a(f"/recovery/{case['case_id']}/execute"), headers=HEADERS_A, json={
        "idempotency_key": "fail-key-00000001",
        "simulate_failure": True
    })
    assert fail_exec.status_code == 200
    failed_case = fail_exec.json()
    assert failed_case["status"] == "failed"
    assert failed_case["resolution_outcome"]["outcome"] == "failed"
    assert "Simulated supplier error" in failed_case["resolution_outcome"]["error"]

    # Audit event logged
    events = client.get(url_a(f"/recovery/{case['case_id']}/audit"), headers=HEADERS_A).json()["events"]
    actions = [e["action"] for e in events]
    assert "recovery.execution_failed" in actions


def test_recovery_cancellation_workflow():
    # Cancel from awaiting_selection
    case1 = client.post(url_a("/recovery"), headers=HEADERS_A, json={
        "trip_id": "trip-cancel-1", "disruption": "cancelled", "options": sample_options()
    }).json()
    cancel1 = client.post(url_a(f"/recovery/{case1['case_id']}/cancel"), headers=HEADERS_A, json={
        "reason": "Traveler arranged alternate transportation independently",
        "cancelled_by": "support-agent-bob"
    })
    assert cancel1.status_code == 200
    assert cancel1.json()["status"] == "cancelled"

    # Cancel from approved
    case2 = client.post(url_a("/recovery"), headers=HEADERS_A, json={
        "trip_id": "trip-cancel-2", "disruption": "delay", "options": sample_options()
    }).json()
    client.post(url_a(f"/recovery/{case2['case_id']}/select"), headers=HEADERS_A, json={"selected_option_id": "opt-flight-101"})
    client.post(url_a(f"/recovery/{case2['case_id']}/decision"), headers=HEADERS_A, json={"approved": True, "approver": "ops-lead"})

    cancel2 = client.post(url_a(f"/recovery/{case2['case_id']}/cancel"), headers=HEADERS_A, json={
        "reason": "Flight unexpectedly departed on time after delay resolved",
        "cancelled_by": "gate-agent"
    })
    assert cancel2.status_code == 200
    assert cancel2.json()["status"] == "cancelled"

    # Cannot cancel from resolved
    case3 = client.post(url_a("/recovery"), headers=HEADERS_A, json={
        "trip_id": "trip-cancel-3", "disruption": "delay", "options": sample_options()
    }).json()
    client.post(url_a(f"/recovery/{case3['case_id']}/select"), headers=HEADERS_A, json={"selected_option_id": "opt-flight-101"})
    client.post(url_a(f"/recovery/{case3['case_id']}/decision"), headers=HEADERS_A, json={"approved": True, "approver": "ops-lead"})
    client.post(url_a(f"/recovery/{case3['case_id']}/execute"), headers=HEADERS_A, json={"idempotency_key": "res-cancel-key-01"})

    cancel3 = client.post(url_a(f"/recovery/{case3['case_id']}/cancel"), headers=HEADERS_A, json={
        "reason": "try to cancel after resolved",
        "cancelled_by": "support-agent"
    })
    assert cancel3.status_code == 409


def test_recovery_tenant_isolation():
    # Tenant A creates a case
    case_a = client.post(url_a("/recovery"), headers=HEADERS_A, json={
        "trip_id": "trip-tenant-iso", "disruption": "cancelled", "options": sample_options()
    }).json()
    case_id = case_a["case_id"]

    # Header mismatch rejected by middleware
    mismatch = client.get(f"/v2/tenants/{TENANT_A}/recovery/{case_id}", headers=HEADERS_B)
    assert mismatch.status_code == 401

    # Tenant B querying their own endpoint with Tenant A's case_id -> 404 (does not exist for Tenant B)
    assert client.get(url_b(f"/recovery/{case_id}"), headers=HEADERS_B).status_code == 404

    # Tenant B cannot select, decide, execute, or cancel Tenant A's case
    assert client.post(url_b(f"/recovery/{case_id}/select"), headers=HEADERS_B, json={
        "selected_option_id": "opt-flight-101"
    }).status_code == 404

    assert client.post(url_b(f"/recovery/{case_id}/decision"), headers=HEADERS_B, json={
        "approved": True, "approver": "tenant-b-actor"
    }).status_code == 404

    assert client.post(url_b(f"/recovery/{case_id}/execute"), headers=HEADERS_B, json={
        "idempotency_key": "iso-exec-key-001"
    }).status_code == 404

    assert client.post(url_b(f"/recovery/{case_id}/cancel"), headers=HEADERS_B, json={
        "reason": "iso test", "cancelled_by": "tenant-b-actor"
    }).status_code == 404

    # Tenant B listing does not show Tenant A's case
    b_cases = client.get(url_b("/recovery"), headers=HEADERS_B).json()["cases"]
    assert all(c["case_id"] != case_id for c in b_cases)

    # Tenant B audit does not leak Tenant A's case audit
    assert client.get(url_b(f"/recovery/{case_id}/audit"), headers=HEADERS_B).status_code == 404


def test_recovery_audit_trail_chronological_integrity():
    case = client.post(url_a("/recovery"), headers=HEADERS_A, json={
        "trip_id": "trip-audit-full", "disruption": "cancelled", "options": sample_options()
    }).json()
    case_id = case["case_id"]

    client.post(url_a(f"/recovery/{case_id}/select"), headers=HEADERS_A, json={
        "selected_option_id": "opt-flight-101", "notes": "client accepted flight"
    })
    client.post(url_a(f"/recovery/{case_id}/decision"), headers=HEADERS_A, json={
        "approved": True, "approver": "supervisor-dan", "notes": "approved budget"
    })
    client.post(url_a(f"/recovery/{case_id}/execute"), headers=HEADERS_A, json={
        "idempotency_key": "audit-trail-key-101"
    })

    audit_resp = client.get(url_a(f"/recovery/{case_id}/audit"), headers=HEADERS_A)
    assert audit_resp.status_code == 200
    events = audit_resp.json()["events"]

    expected_actions = [
        "recovery.created",
        "recovery.option_selected",
        "recovery.approved",
        "recovery.execution_started",
        "recovery.resolved"
    ]
    actual_actions = [e["action"] for e in events]
    assert actual_actions == expected_actions

    for ev in events:
        assert ev["resource_type"] == "recovery_case"
        assert ev["resource_id"] == case_id
        assert ev["created_at"] != ""


def test_agent_runtime_recovery_integration():
    payload = {
        "trip_id": "trip-agent-rec",
        "disruption": "Severe snowstorm grounding flights",
        "options": sample_options(),
        "context": {"source": "automated_disruption_detector"}
    }
    resp = client.post(url_a("/agents/run"), headers=HEADERS_A, json={
        "task_type": "recovery",
        "payload": payload
    })
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["requires_human_confirmation"] is True
    assert result["status"] == "awaiting_selection"
    assert result["options_evaluated"] == 2
    case_id = result["case_id"]

    # Verify case was created and can be fetched via API
    case_fetched = client.get(url_a(f"/recovery/{case_id}"), headers=HEADERS_A).json()
    assert case_fetched["status"] == "awaiting_selection"
    assert case_fetched["context"]["source"] == "automated_disruption_detector"


def test_sqlite_schema_backward_compatibility_auto_migration(tmp_path):
    db_file = tmp_path / "legacy_recovery.sqlite"
    # Create database with older v0.1 schema (missing updated_at, approval_metadata, etc.)
    with Database(str(db_file)).connect() as db:
        db.execute("""
            CREATE TABLE recovery_cases (
              tenant TEXT NOT NULL,
              case_id TEXT NOT NULL,
              trip_id TEXT NOT NULL,
              disruption TEXT NOT NULL,
              options TEXT NOT NULL,
              selected_option TEXT,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY (tenant, case_id)
            )
        """)
        db.execute("""
            CREATE TABLE audit_events (
              tenant TEXT NOT NULL,
              event_id TEXT NOT NULL,
              actor TEXT NOT NULL,
              action TEXT NOT NULL,
              resource_type TEXT NOT NULL,
              resource_id TEXT NOT NULL,
              metadata TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY (tenant, event_id)
            )
        """)
        # Insert a pre-existing legacy record
        db.execute("""
            INSERT INTO recovery_cases VALUES(
              'legacy-tenant', 'rec-legacy-001', 'trip-0', 'delay', '[]', NULL, 'awaiting_selection', '2026-01-01T00:00:00Z'
            )
        """)

    # Opening PlatformStore should auto-migrate the missing columns without error
    migrated_store = PlatformStore(db_file, initialize=True)
    migrated_case = migrated_store.get_recovery("legacy-tenant", "rec-legacy-001")
    assert migrated_case is not None
    assert migrated_case["case_id"] == "rec-legacy-001"
    assert migrated_case["approval_metadata"] is None
    assert migrated_case["execution_metadata"] is None
    assert migrated_case["resolution_outcome"] is None
    assert migrated_case["idempotency_key"] is None

    # Now verify new operations work on this migrated table
    new_case = migrated_store.create_recovery(
        "legacy-tenant", "trip-1", "strike", sample_options()
    )
    assert new_case["status"] == "awaiting_selection"


def test_concurrent_execution_same_idempotency_key():
    from concurrent.futures import ThreadPoolExecutor

    # Setup approved case
    case = client.post(url_a("/recovery"), headers=HEADERS_A, json={
        "trip_id": "trip-concur-same", "disruption": "cancelled", "options": sample_options()
    }).json()
    case_id = case["case_id"]
    client.post(url_a(f"/recovery/{case_id}/select"), headers=HEADERS_A, json={"selected_option_id": "opt-flight-101"})
    client.post(url_a(f"/recovery/{case_id}/decision"), headers=HEADERS_A, json={"approved": True, "approver": "lead"})

    key = "concurrent-same-key-888"

    def run_exec():
        return client.post(url_a(f"/recovery/{case_id}/execute"), headers=HEADERS_A, json={
            "idempotency_key": key
        })

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(run_exec) for _ in range(8)]
        results = [f.result() for f in futures]

    # Every concurrent execution request with the same key must return 200
    assert all(r.status_code == 200 for r in results)

    # Every request must resolve to the identical confirmation ID
    conf_ids = {r.json()["resolution_outcome"]["confirmation_id"] for r in results}
    assert len(conf_ids) == 1

    # Exactly one execution_started and one resolved audit event must exist
    audit_events = client.get(url_a(f"/recovery/{case_id}/audit"), headers=HEADERS_A).json()["events"]
    actions = [e["action"] for e in audit_events]
    assert actions.count("recovery.execution_started") == 1
    assert actions.count("recovery.resolved") == 1


def test_concurrent_execution_different_idempotency_keys():
    from concurrent.futures import ThreadPoolExecutor

    case = client.post(url_a("/recovery"), headers=HEADERS_A, json={
        "trip_id": "trip-concur-diff", "disruption": "cancelled", "options": sample_options()
    }).json()
    case_id = case["case_id"]
    client.post(url_a(f"/recovery/{case_id}/select"), headers=HEADERS_A, json={"selected_option_id": "opt-flight-101"})
    client.post(url_a(f"/recovery/{case_id}/decision"), headers=HEADERS_A, json={"approved": True, "approver": "lead"})

    def run_exec(idx: int):
        return client.post(url_a(f"/recovery/{case_id}/execute"), headers=HEADERS_A, json={
            "idempotency_key": f"diff-key-{idx:04d}-999"
        })

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(run_exec, i) for i in range(8)]
        results = [f.result() for f in futures]

    status_codes = [r.status_code for r in results]
    # Exactly one thread succeeds with 200; the other 7 must receive 409 Conflict
    assert status_codes.count(200) == 1
    assert status_codes.count(409) == 7


def test_concurrent_idempotency_key_collision_across_different_cases():
    from concurrent.futures import ThreadPoolExecutor

    # Setup two different approved cases
    c1 = client.post(url_a("/recovery"), headers=HEADERS_A, json={"trip_id": "t1", "disruption": "d1", "options": sample_options()}).json()
    c2 = client.post(url_a("/recovery"), headers=HEADERS_A, json={"trip_id": "t2", "disruption": "d2", "options": sample_options()}).json()

    for c in (c1, c2):
        client.post(url_a(f"/recovery/{c['case_id']}/select"), headers=HEADERS_A, json={"selected_option_id": "opt-flight-101"})
        client.post(url_a(f"/recovery/{c['case_id']}/decision"), headers=HEADERS_A, json={"approved": True, "approver": "lead"})

    shared_key = "shared-cross-case-key-1234"

    def exec_case(cid: str):
        return client.post(url_a(f"/recovery/{cid}/execute"), headers=HEADERS_A, json={
            "idempotency_key": shared_key
        })

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(exec_case, c1["case_id"])
        f2 = pool.submit(exec_case, c2["case_id"])
        r1, r2 = f1.result(), f2.result()

    codes = {r1.status_code, r2.status_code}
    assert codes == {200, 409}


def test_concurrent_approval_and_cancellation():
    from concurrent.futures import ThreadPoolExecutor

    case = client.post(url_a("/recovery"), headers=HEADERS_A, json={
        "trip_id": "trip-appr-canc", "disruption": "cancelled", "options": sample_options()
    }).json()
    case_id = case["case_id"]
    client.post(url_a(f"/recovery/{case_id}/select"), headers=HEADERS_A, json={"selected_option_id": "opt-flight-101"})

    def do_approve():
        return client.post(url_a(f"/recovery/{case_id}/decision"), headers=HEADERS_A, json={
            "approved": True, "approver": "manager-1"
        })

    def do_cancel():
        return client.post(url_a(f"/recovery/{case_id}/cancel"), headers=HEADERS_A, json={
            "reason": "cancelling", "cancelled_by": "support-1"
        })

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_appr = pool.submit(do_approve)
        f_canc = pool.submit(do_cancel)
        r_appr, r_canc = f_appr.result(), f_canc.result()

    # The final state must be either 'approved' or 'cancelled'
    final_case = client.get(url_a(f"/recovery/{case_id}"), headers=HEADERS_A).json()
    assert final_case["status"] in {"approved", "cancelled"}


def test_sensitive_credentials_scrubbed_from_execution_audit():
    case = client.post(url_a("/recovery"), headers=HEADERS_A, json={
        "trip_id": "trip-scrub", "disruption": "cancelled", "options": sample_options()
    }).json()
    case_id = case["case_id"]
    client.post(url_a(f"/recovery/{case_id}/select"), headers=HEADERS_A, json={"selected_option_id": "opt-flight-101"})
    client.post(url_a(f"/recovery/{case_id}/decision"), headers=HEADERS_A, json={"approved": True, "approver": "lead"})

    res = client.post(url_a(f"/recovery/{case_id}/execute"), headers=HEADERS_A, json={
        "idempotency_key": "scrub-test-key-101",
        "details": {
            "credit_card": "4111-2222-3333-4444",
            "cvv": "999",
            "passport_number": "P12345678",
            "seat_selection": "14A"
        }
    })
    assert res.status_code == 200
    saved = res.json()
    # Sensitive details should be scrubbed from execution metadata
    assert "credit_card" not in saved["execution_metadata"]["details"]
    assert "cvv" not in saved["execution_metadata"]["details"]
    assert "passport_number" not in saved["execution_metadata"]["details"]
    assert saved["execution_metadata"]["details"]["seat_selection"] == "14A"

    # Audit events must never contain the sensitive details
    audit_events = client.get(url_a(f"/recovery/{case_id}/audit"), headers=HEADERS_A).json()["events"]
    for ev in audit_events:
        meta_str = str(ev["metadata"])
        assert "4111-2222" not in meta_str
        assert "P12345678" not in meta_str


def test_unexpected_programming_keyerror_not_masked_as_404(monkeypatch):
    # Ensure unexpected KeyError inside PlatformStore becomes 500, not 404
    def broken_get_recovery(*args, **kwargs):
        raise KeyError("simulated_internal_dictionary_bug")

    monkeypatch.setattr(api_module.platform_store, "get_recovery", broken_get_recovery)
    with TestClient(api_module.app, raise_server_exceptions=False) as c:
        resp = c.get(url_a("/recovery/rec-some-id"), headers=HEADERS_A)
    # Must NOT be 404 Not Found
    assert resp.status_code == 500

