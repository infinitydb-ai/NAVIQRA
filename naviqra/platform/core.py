from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from naviqra.database import Database


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class RecoveryError(Exception):
    """Base domain error for travel disruption recovery operations."""
    pass


class RecoveryCaseNotFoundError(RecoveryError, KeyError):
    """Raised when a recovery case is not found for a tenant."""
    pass


class RecoveryConflictError(RecoveryError, ValueError):
    """Raised when an operation conflicts with lifecycle state or idempotency rules."""
    pass


class RecoveryValidationError(RecoveryError, ValueError):
    """Raised when input parameters fail domain validation rules."""
    pass


class PlatformStore:
    """Tenant-scoped persistence for graph, memory, groups, runs and recovery cases."""

    def __init__(self, path: str | Path, *, initialize: bool = True):
        self.path = str(path)
        self.database = Database(self.path)
        if initialize:
            self._initialize()

    def _initialize(self):
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS t_memory(
              tenant TEXT NOT NULL, traveler TEXT NOT NULL, tier TEXT NOT NULL,
              key TEXT NOT NULL, value TEXT NOT NULL, updated_at TEXT NOT NULL,
              PRIMARY KEY(tenant, traveler, tier, key));
            CREATE TABLE IF NOT EXISTS graph_nodes(
              tenant TEXT NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL,
              label TEXT NOT NULL, properties TEXT NOT NULL, created_at TEXT NOT NULL,
              PRIMARY KEY(tenant,id));
            CREATE TABLE IF NOT EXISTS graph_edges(
              tenant TEXT NOT NULL, id TEXT NOT NULL, source TEXT NOT NULL,
              relation TEXT NOT NULL, target TEXT NOT NULL, properties TEXT NOT NULL,
              created_at TEXT NOT NULL, PRIMARY KEY(tenant,id));
            CREATE INDEX IF NOT EXISTS idx_edges_source ON graph_edges(tenant,source);
            CREATE TABLE IF NOT EXISTS group_votes(
              tenant TEXT NOT NULL, group_id TEXT NOT NULL, member_id TEXT NOT NULL,
              candidate_id TEXT NOT NULL, score REAL NOT NULL, weight REAL NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY(tenant,group_id,member_id,candidate_id));
            CREATE TABLE IF NOT EXISTS agent_runs(
              tenant TEXT NOT NULL, run_id TEXT NOT NULL, task_type TEXT NOT NULL,
              status TEXT NOT NULL, result TEXT, created_at TEXT NOT NULL,
              PRIMARY KEY(tenant,run_id));
            CREATE TABLE IF NOT EXISTS audit_events(
              tenant TEXT NOT NULL, event_id TEXT NOT NULL, actor TEXT NOT NULL,
              action TEXT NOT NULL, resource_type TEXT NOT NULL, resource_id TEXT NOT NULL,
              metadata TEXT NOT NULL, created_at TEXT NOT NULL,
              PRIMARY KEY(tenant,event_id));
            CREATE INDEX IF NOT EXISTS idx_audit_tenant_time ON audit_events(tenant,created_at);
            CREATE TABLE IF NOT EXISTS recovery_cases(
              tenant TEXT NOT NULL, case_id TEXT NOT NULL, trip_id TEXT NOT NULL,
              disruption TEXT NOT NULL, options TEXT NOT NULL, selected_option TEXT,
              status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              approval_metadata TEXT NOT NULL DEFAULT '{}',
              execution_metadata TEXT NOT NULL DEFAULT '{}',
              resolution_outcome TEXT,
              idempotency_key TEXT,
              context TEXT NOT NULL DEFAULT '{}',
              PRIMARY KEY(tenant,case_id));
            CREATE INDEX IF NOT EXISTS idx_recovery_lookup ON recovery_cases(tenant, status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_recovery_trip ON recovery_cases(tenant, trip_id);
            """)
            if db.dialect == "sqlite":
                cols = {row["name"] for row in db.execute("PRAGMA table_info(recovery_cases)").fetchall()}
                if "updated_at" not in cols:
                    db.execute("ALTER TABLE recovery_cases ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
                if "approval_metadata" not in cols:
                    db.execute("ALTER TABLE recovery_cases ADD COLUMN approval_metadata TEXT NOT NULL DEFAULT '{}'")
                if "execution_metadata" not in cols:
                    db.execute("ALTER TABLE recovery_cases ADD COLUMN execution_metadata TEXT NOT NULL DEFAULT '{}'")
                if "resolution_outcome" not in cols:
                    db.execute("ALTER TABLE recovery_cases ADD COLUMN resolution_outcome TEXT")
                if "idempotency_key" not in cols:
                    db.execute("ALTER TABLE recovery_cases ADD COLUMN idempotency_key TEXT")
                if "context" not in cols:
                    db.execute("ALTER TABLE recovery_cases ADD COLUMN context TEXT NOT NULL DEFAULT '{}'")
                db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_recovery_cases_tenant_idempotency ON recovery_cases(tenant, idempotency_key) WHERE idempotency_key IS NOT NULL")
            else:
                db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_recovery_cases_tenant_idempotency ON recovery_cases(tenant, idempotency_key) WHERE idempotency_key IS NOT NULL")

    def connect(self):
        return self.database.connect()

    def check(self):
        self.database.check()
        with self.connect() as db:
            db.execute("SELECT 1 FROM t_memory LIMIT 1").fetchone()

    def put_memory(self, tenant: str, traveler: str, tier: str, key: str, value: Any):
        allowed = {"profile", "episodic", "semantic", "procedural", "active"}
        if tier not in allowed:
            raise ValueError("tier must be one of " + ", ".join(sorted(allowed)))
        with self.connect() as db:
            db.execute("""INSERT INTO t_memory VALUES(?,?,?,?,?,?)
              ON CONFLICT(tenant,traveler,tier,key) DO UPDATE SET
              value=excluded.value,updated_at=excluded.updated_at""",
              (tenant, traveler, tier, key, json.dumps(value), utcnow()))
        return {"tenant_id": tenant, "traveler_id": traveler, "tier": tier,
                "key": key, "value": value}

    def get_memories(self, tenant: str, traveler: str):
        with self.connect() as db:
            rows = db.execute("""SELECT tier,key,value,updated_at FROM t_memory
              WHERE tenant=? AND traveler=? ORDER BY updated_at DESC""",
              (tenant, traveler)).fetchall()
        return [{"tier": r["tier"], "key": r["key"], "value": json.loads(r["value"]),
                 "updated_at": r["updated_at"]} for r in rows]

    def add_node(self, tenant: str, kind: str, label: str, properties: dict[str, Any]):
        node_id = str(uuid4())
        with self.connect() as db:
            db.execute("INSERT INTO graph_nodes VALUES(?,?,?,?,?,?)",
                       (tenant,node_id,kind,label,json.dumps(properties),utcnow()))
        return {"id": node_id, "kind": kind, "label": label, "properties": properties}

    def add_edge(self, tenant: str, source: str, relation: str, target: str,
                 properties: dict[str, Any] | None = None):
        edge_id = str(uuid4())
        with self.connect() as db:
            exists = db.execute("""SELECT COUNT(*) n FROM graph_nodes
                WHERE tenant=? AND id IN (?,?)""", (tenant,source,target)).fetchone()["n"]
            if exists != 2:
                raise ValueError("both graph endpoints must exist in this tenant")
            db.execute("INSERT INTO graph_edges VALUES(?,?,?,?,?,?,?)",
                       (tenant,edge_id,source,relation,target,json.dumps(properties or {}),utcnow()))
        return {"id": edge_id, "source": source, "relation": relation, "target": target,
                "properties": properties or {}}

    def graph(self, tenant: str, limit: int = 100):
        with self.connect() as db:
            nodes = db.execute("SELECT * FROM graph_nodes WHERE tenant=? ORDER BY created_at DESC LIMIT ?",
                               (tenant,limit)).fetchall()
            edges = db.execute("SELECT * FROM graph_edges WHERE tenant=? ORDER BY created_at DESC LIMIT ?",
                               (tenant,limit)).fetchall()
        return {
            "nodes": [{"id":r["id"],"kind":r["kind"],"label":r["label"],
                       "properties":json.loads(r["properties"])} for r in nodes],
            "edges": [{"id":r["id"],"source":r["source"],"relation":r["relation"],
                       "target":r["target"],"properties":json.loads(r["properties"])} for r in edges]
        }

    def vote(self, tenant: str, group_id: str, member_id: str, candidate_id: str,
             score: float, weight: float = 1.0):
        if not 0 <= score <= 1 or not 0 < weight <= 10:
            raise ValueError("score must be 0..1 and weight must be >0..10")
        with self.connect() as db:
            db.execute("""INSERT INTO group_votes VALUES(?,?,?,?,?,?,?)
              ON CONFLICT(tenant,group_id,member_id,candidate_id) DO UPDATE SET
              score=excluded.score,weight=excluded.weight,created_at=excluded.created_at""",
              (tenant,group_id,member_id,candidate_id,score,weight,utcnow()))
        return self.consensus(tenant, group_id)

    def consensus(self, tenant: str, group_id: str):
        with self.connect() as db:
            rows = db.execute("""SELECT candidate_id, SUM(score*weight)/SUM(weight) AS score,
              COUNT(DISTINCT member_id) AS voters FROM group_votes
              WHERE tenant=? AND group_id=? GROUP BY candidate_id ORDER BY score DESC,candidate_id""",
              (tenant,group_id)).fetchall()
        return {"group_id":group_id,"ranked_candidates":[
            {"candidate_id":r["candidate_id"],"score":round(r["score"],4),"voters":r["voters"]}
            for r in rows]}

    def save_run(self, tenant: str, run_id: str, task_type: str, status: str, result: Any):
        with self.connect() as db:
            db.execute("INSERT INTO agent_runs VALUES(?,?,?,?,?,?)",
                       (tenant,run_id,task_type,status,json.dumps(result),utcnow()))

    @staticmethod
    def _recovery_from_row(r: Any) -> dict[str, Any]:
        options = json.loads(r["options"]) if isinstance(r["options"], str) else r["options"]
        selected = json.loads(r["selected_option"]) if r["selected_option"] else None
        keys = r.keys() if hasattr(r, "keys") else r

        updated_at = r["updated_at"] if "updated_at" in keys and r["updated_at"] else r["created_at"]
        approval = json.loads(r["approval_metadata"]) if "approval_metadata" in keys and r["approval_metadata"] and r["approval_metadata"] != "{}" else None
        execution = json.loads(r["execution_metadata"]) if "execution_metadata" in keys and r["execution_metadata"] and r["execution_metadata"] != "{}" else None
        resolution = json.loads(r["resolution_outcome"]) if "resolution_outcome" in keys and r["resolution_outcome"] else None
        idempotency_key = r["idempotency_key"] if "idempotency_key" in keys else None
        context = json.loads(r["context"]) if "context" in keys and r["context"] else {}

        return {
            "case_id": r["case_id"],
            "tenant_id": r["tenant"],
            "tenant": r["tenant"],
            "trip_id": r["trip_id"],
            "disruption": r["disruption"],
            "options": options,
            "selected_option": selected,
            "status": r["status"],
            "created_at": r["created_at"],
            "updated_at": updated_at,
            "approval_metadata": approval,
            "execution_metadata": execution,
            "resolution_outcome": resolution,
            "idempotency_key": idempotency_key,
            "context": context,
            "requires_human_confirmation": r["status"] == "awaiting_selection",
        }

    def create_recovery(self, tenant: str, trip_id: str, disruption: str,
                        options: list[dict[str, Any]], context: dict[str, Any] | None = None):
        if not trip_id or not str(trip_id).strip():
            raise RecoveryValidationError("trip_id must be non-empty")
        if not disruption or not str(disruption).strip():
            raise RecoveryValidationError("disruption must be non-empty")
        if not isinstance(options, list) or not options:
            raise RecoveryValidationError("non-empty options list is required")
        for opt in options:
            if not isinstance(opt, dict) or not opt.get("id"):
                raise RecoveryValidationError("each option must be a dictionary with an 'id' field")

        case_id = str(uuid4())
        now = utcnow()
        with self.connect() as db:
            db.execute(
                """INSERT INTO recovery_cases(tenant, case_id, trip_id, disruption, options,
                   selected_option, status, created_at, updated_at, approval_metadata,
                   execution_metadata, resolution_outcome, idempotency_key, context)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (tenant, case_id, trip_id, disruption, json.dumps(options), None,
                 "awaiting_selection", now, now, "{}", "{}",
                 None, None, json.dumps(context or {}))
            )
        self.audit(tenant, "agent_runtime", "recovery.created", "recovery_case", case_id,
                   {"trip_id": trip_id, "disruption": disruption, "options_count": len(options)})
        return self.get_recovery(tenant, case_id)

    def get_recovery(self, tenant: str, case_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM recovery_cases WHERE tenant=? AND case_id=?",
                             (tenant, case_id)).fetchone()
        return self._recovery_from_row(row) if row else None

    def list_recoveries(self, tenant: str, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            if status:
                rows = db.execute(
                    "SELECT * FROM recovery_cases WHERE tenant=? AND status=? ORDER BY created_at DESC LIMIT ?",
                    (tenant, status, limit)
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM recovery_cases WHERE tenant=? ORDER BY created_at DESC LIMIT ?",
                    (tenant, limit)
                ).fetchall()
        return [self._recovery_from_row(r) for r in rows]

    def select_recovery_option(self, tenant: str, case_id: str, selected_option_id: str, notes: str = "") -> dict[str, Any]:
        case = self.get_recovery(tenant, case_id)
        if case is None:
            raise RecoveryCaseNotFoundError(f"recovery case '{case_id}' not found")
        if case["status"] != "awaiting_selection":
            raise RecoveryConflictError(f"Cannot select alternative for recovery case in state '{case['status']}'. Must be in 'awaiting_selection'.")

        matching = next((opt for opt in case["options"] if str(opt.get("id")) == str(selected_option_id)), None)
        if matching is None:
            raise RecoveryValidationError(f"selected_option_id '{selected_option_id}' does not match any available option in this recovery case")

        now = utcnow()
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE recovery_cases SET selected_option=?, updated_at=? WHERE tenant=? AND case_id=? AND status='awaiting_selection'",
                (json.dumps(matching), now, tenant, case_id)
            )
            if cursor.rowcount == 0:
                raise RecoveryConflictError("Concurrent update detected: case is no longer awaiting selection.")
        self.audit(tenant, "operator", "recovery.option_selected", "recovery_case", case_id,
                   {"selected_option_id": selected_option_id, "notes": notes})
        return self.get_recovery(tenant, case_id)

    def decide_recovery(self, tenant: str, case_id: str, approved: bool, approver: str, notes: str = "") -> dict[str, Any]:
        if not approver or not str(approver).strip():
            raise RecoveryValidationError("approver must be specified")
        case = self.get_recovery(tenant, case_id)
        if case is None:
            raise RecoveryCaseNotFoundError(f"recovery case '{case_id}' not found")
        if case["status"] != "awaiting_selection":
            raise RecoveryConflictError(f"Invalid state transition: cannot decide recovery case in state '{case['status']}'. Expected 'awaiting_selection'.")

        now = utcnow()
        if approved:
            if not case.get("selected_option"):
                raise RecoveryConflictError("Cannot approve recovery case without selecting an alternative first.")
            target_status = "approved"
            action = "recovery.approved"
            approval_meta = {"approver": approver.strip(), "approved_at": now, "notes": notes, "decision": "approved"}
        else:
            target_status = "rejected"
            action = "recovery.rejected"
            approval_meta = {"approver": approver.strip(), "rejected_at": now, "notes": notes, "decision": "rejected"}

        with self.connect() as db:
            cursor = db.execute(
                "UPDATE recovery_cases SET status=?, updated_at=?, approval_metadata=? WHERE tenant=? AND case_id=? AND status='awaiting_selection'",
                (target_status, now, json.dumps(approval_meta), tenant, case_id)
            )
            if cursor.rowcount == 0:
                raise RecoveryConflictError("Concurrent update detected: case is no longer awaiting selection.")
        self.audit(tenant, approver.strip(), action, "recovery_case", case_id, approval_meta)
        return self.get_recovery(tenant, case_id)

    def execute_recovery(self, tenant: str, case_id: str, idempotency_key: str, provider: str = "sandbox",
                         simulate_failure: bool = False, execution_details: dict[str, Any] | None = None) -> dict[str, Any]:
        if not idempotency_key or len(idempotency_key) < 8 or len(idempotency_key) > 128:
            raise RecoveryValidationError("idempotency_key must be 8-128 characters")
        case = self.get_recovery(tenant, case_id)
        if case is None:
            raise RecoveryCaseNotFoundError(f"recovery case '{case_id}' not found")

        # Idempotency replay check for this case
        if case.get("idempotency_key") == idempotency_key:
            if case["status"] in {"executing", "resolved", "failed"}:
                return case

        # Verify idempotency key is not stolen by another case
        with self.connect() as db:
            existing = db.execute(
                "SELECT case_id FROM recovery_cases WHERE tenant=? AND idempotency_key=? AND case_id != ?",
                (tenant, idempotency_key, case_id)
            ).fetchone()
            if existing:
                raise RecoveryConflictError("idempotency_key has already been used for another recovery case")

        if case["status"] != "approved":
            raise RecoveryConflictError(f"Cannot execute recovery case in state '{case['status']}'. Explicit human approval is required before execution.")
        if not case.get("selected_option"):
            raise RecoveryConflictError("Cannot execute recovery case without an approved alternative.")

        # Sanitize execution details to eliminate sensitive credential fields
        sanitized_details: dict[str, Any] = {}
        if execution_details and isinstance(execution_details, dict):
            sensitive_patterns = ("card", "cvv", "pan", "passport", "ssn", "secret", "token", "password", "credential")
            for k, v in execution_details.items():
                if any(p in str(k).lower() for p in sensitive_patterns):
                    continue
                sanitized_details[k] = v

        now = utcnow()
        exec_meta = {
            "idempotency_key": idempotency_key,
            "execution_started_at": now,
            "provider": provider,
            "mode": "simulation",
            "details": sanitized_details
        }
        try:
            with self.connect() as db:
                cursor = db.execute(
                    "UPDATE recovery_cases SET status='executing', updated_at=?, idempotency_key=?, execution_metadata=? WHERE tenant=? AND case_id=? AND status='approved'",
                    (now, idempotency_key, json.dumps(exec_meta), tenant, case_id)
                )
        except Exception as exc:
            if "UNIQUE" in str(exc).upper() or "idx_recovery_cases_tenant_idempotency" in str(exc):
                raise RecoveryConflictError("idempotency_key has already been used for another recovery case") from exc
            raise

        if cursor.rowcount == 0:
            import time
            current = None
            for _ in range(40):
                current = self.get_recovery(tenant, case_id)
                if current and current.get("idempotency_key") == idempotency_key:
                    if current["status"] in {"resolved", "failed"}:
                        return current
                time.sleep(0.05)
            if current and current.get("idempotency_key") == idempotency_key:
                return current
            raise RecoveryConflictError("Concurrent execution detected: case is not in approved state.")

        # Log operational audit event without sensitive details
        self.audit(tenant, "recovery_executor", "recovery.execution_started", "recovery_case", case_id,
                   {"idempotency_key": idempotency_key, "provider": provider, "mode": "simulation"})

        if simulate_failure:
            err_now = utcnow()
            fail_meta = {**exec_meta, "failed_at": err_now, "error": "Simulated supplier inventory rejection or payment failure"}
            fail_outcome = {"outcome": "failed", "error": "Simulated supplier error", "provider": provider, "failed_at": err_now}
            with self.connect() as db:
                db.execute(
                    "UPDATE recovery_cases SET status='failed', updated_at=?, execution_metadata=?, resolution_outcome=? WHERE tenant=? AND case_id=? AND status='executing'",
                    (err_now, json.dumps(fail_meta), json.dumps(fail_outcome), tenant, case_id)
                )
            self.audit(tenant, "recovery_executor", "recovery.execution_failed", "recovery_case", case_id, fail_outcome)
            return self.get_recovery(tenant, case_id)
        else:
            res_now = utcnow()
            conf_id = f"SIM-REC-{uuid4().hex[:12].upper()}"
            resolution = {
                "outcome": "simulated_rebooking_confirmed",
                "confirmation_id": conf_id,
                "provider": provider,
                "live_inventory": False,
                "booking_mode": "simulated_non_bookable",
                "resolved_at": res_now,
                "selected_option": case["selected_option"],
                "notice": "Simulated sandbox execution; no live external booking was performed."
            }
            success_meta = {
                **exec_meta,
                "resolved_at": res_now,
                "confirmation_id": conf_id,
                "mode": "simulation",
                "booking_mode": "simulated_non_bookable"
            }
            with self.connect() as db:
                db.execute(
                    "UPDATE recovery_cases SET status='resolved', updated_at=?, execution_metadata=?, resolution_outcome=? WHERE tenant=? AND case_id=? AND status='executing'",
                    (res_now, json.dumps(success_meta), json.dumps(resolution), tenant, case_id)
                )
            self.audit(tenant, "recovery_executor", "recovery.resolved", "recovery_case", case_id, {"confirmation_id": conf_id, "provider": provider})
            return self.get_recovery(tenant, case_id)

    def cancel_recovery(self, tenant: str, case_id: str, reason: str, cancelled_by: str) -> dict[str, Any]:
        if not reason or not str(reason).strip():
            raise RecoveryValidationError("reason must be specified")
        if not cancelled_by or not str(cancelled_by).strip():
            raise RecoveryValidationError("cancelled_by must be specified")

        case = self.get_recovery(tenant, case_id)
        if case is None:
            raise RecoveryCaseNotFoundError(f"recovery case '{case_id}' not found")
        if case["status"] not in {"awaiting_selection", "approved"}:
            raise RecoveryConflictError(f"Cannot cancel recovery case in state '{case['status']}'. Only 'awaiting_selection' or 'approved' cases can be cancelled.")

        now = utcnow()
        cancel_meta = {
            "cancelled_by": cancelled_by.strip(),
            "cancelled_at": now,
            "reason": reason.strip(),
            "previous_status": case["status"]
        }
        cancel_outcome = {
            "outcome": "cancelled",
            "reason": reason.strip(),
            "cancelled_by": cancelled_by.strip(),
            "cancelled_at": now
        }
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE recovery_cases SET status='cancelled', updated_at=?, execution_metadata=?, resolution_outcome=? WHERE tenant=? AND case_id=? AND status IN ('awaiting_selection', 'approved')",
                (now, json.dumps(cancel_meta), json.dumps(cancel_outcome), tenant, case_id)
            )
            if cursor.rowcount == 0:
                raise RecoveryConflictError("Concurrent update detected: case could not be cancelled.")
        self.audit(tenant, cancelled_by.strip(), "recovery.cancelled", "recovery_case", case_id, cancel_meta)
        return self.get_recovery(tenant, case_id)

    def recovery_audit(self, tenant: str, case_id: str) -> list[dict[str, Any]]:
        case = self.get_recovery(tenant, case_id)
        if case is None:
            raise RecoveryCaseNotFoundError(f"recovery case '{case_id}' not found")
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM audit_events WHERE tenant=? AND resource_type='recovery_case' AND resource_id=? ORDER BY created_at ASC, event_id ASC",
                (tenant, case_id)
            ).fetchall()
        return [
            {
                "event_id": r["event_id"],
                "actor": r["actor"],
                "action": r["action"],
                "resource_type": r["resource_type"],
                "resource_id": r["resource_id"],
                "metadata": json.loads(r["metadata"]) if isinstance(r["metadata"], str) else r["metadata"],
                "created_at": r["created_at"]
            }
            for r in rows
        ]
        return [
            {
                "event_id": r["event_id"],
                "actor": r["actor"],
                "action": r["action"],
                "resource_type": r["resource_type"],
                "resource_id": r["resource_id"],
                "metadata": json.loads(r["metadata"]) if isinstance(r["metadata"], str) else r["metadata"],
                "created_at": r["created_at"]
            }
            for r in rows
        ]

    def audit(self, tenant: str, actor: str, action: str, resource_type: str,
              resource_id: str, metadata: dict[str, Any] | None = None):
        event_id = str(uuid4())
        with self.connect() as db:
            db.execute("INSERT INTO audit_events VALUES(?,?,?,?,?,?,?,?)",
                       (tenant,event_id,actor,action,resource_type,resource_id,
                        json.dumps(metadata or {}, sort_keys=True),utcnow()))
        return {"event_id":event_id,"actor":actor,"action":action,
                "resource_type":resource_type,"resource_id":resource_id}

    def audit_events(self, tenant: str, limit: int = 100):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM audit_events WHERE tenant=? ORDER BY created_at DESC LIMIT ?",
                              (tenant,limit)).fetchall()
        return [{"event_id":r["event_id"],"actor":r["actor"],"action":r["action"],
                 "resource_type":r["resource_type"],"resource_id":r["resource_id"],
                 "metadata":json.loads(r["metadata"]),"created_at":r["created_at"]} for r in rows]

    def delete_traveler(self, tenant: str, traveler: str):
        with self.connect() as db:
            cursor = db.execute("DELETE FROM t_memory WHERE tenant=? AND traveler=?", (tenant,traveler))
            deleted = cursor.rowcount
        self.audit(tenant,"tenant_api","privacy.delete_traveler_memory","traveler",traveler,
                   {"memory_records_deleted":deleted})
        return {"tenant_id":tenant,"traveler_id":traveler,"memory_records_deleted":deleted}


@dataclass
class Agent:
    name: str
    handler: Callable[[dict[str, Any]], dict[str, Any]]


class AgentRuntime:
    """Deterministic, auditable workflow runtime; LLM/provider execution is an explicit adapter."""
    def __init__(self, store: PlatformStore):
        self.store = store
        self.agents: dict[str, Agent] = {
            "travel_intelligence": Agent("travel_intelligence", self._intelligence),
            "group_consensus": Agent("group_consensus", self._consensus),
            "recovery": Agent("recovery", self._recovery),
        }

    def execute(self, tenant: str, task_type: str, payload: dict[str, Any]):
        if task_type not in self.agents:
            raise ValueError("unsupported task_type")
        run_id = str(uuid4())
        try:
            result = self.agents[task_type].handler({"tenant_id":tenant, **payload})
            self.store.save_run(tenant,run_id,task_type,"succeeded",result)
            return {"run_id":run_id,"agent":task_type,"status":"succeeded","result":result}
        except (ValueError, KeyError, TypeError) as exc:
            self.store.save_run(tenant,run_id,task_type,"failed",{"error":str(exc)})
            raise

    def _intelligence(self, p):
        traveler = p.get("traveler_id")
        if not traveler:
            raise ValueError("traveler_id is required")
        return {"traveler_id":traveler,"memory":self.store.get_memories(p["tenant_id"],traveler),
                "graph":self.store.graph(p["tenant_id"],limit=50)}

    def _consensus(self, p):
        if not p.get("group_id"):
            raise ValueError("group_id is required")
        return self.store.consensus(p["tenant_id"],p["group_id"])

    def _recovery(self, p):
        trip_id, disruption = p.get("trip_id"), p.get("disruption")
        options = p.get("options")
        context = p.get("context")
        if not trip_id or not disruption or not isinstance(options,list) or not options:
            raise ValueError("trip_id, disruption and non-empty options are required")
        case = self.store.create_recovery(p["tenant_id"],trip_id,disruption,options,context=context)
        return {
            **case,
            "options_evaluated": len(options),
        }
