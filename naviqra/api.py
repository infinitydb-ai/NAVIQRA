from __future__ import annotations

import hmac
import hashlib
import logging
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from naviqra import __version__
from naviqra.agents.orchestrator import TravelOrchestrator
from naviqra.booking import BookingService
from naviqra.models import (
    ApprovalRequest,
    BookingIntent,
    MemoryRecord,
    TripRequest,
    RecoveryCreateRequest,
    RecoverySelectRequest,
    RecoveryDecisionRequest,
    RecoveryExecuteRequest,
    RecoveryCancelRequest,
)
from naviqra.providers import DemoProvider, ProviderRegistry, SandboxProvider
from naviqra.store import SQLiteStore
from naviqra.platform.core import (
    PlatformStore,
    AgentRuntime,
    RecoveryCaseNotFoundError,
    RecoveryConflictError,
    RecoveryValidationError,
)
import json
from contextlib import asynccontextmanager

logging.basicConfig(level=os.getenv("NAVIQRA_LOG_LEVEL", "INFO"))
logger = logging.getLogger("naviqra.api")
DB_PATH = os.getenv("NAVIQRA_DATABASE_URL", os.getenv("NAVIQRA_DB", "naviqra.db"))
API_KEY = os.getenv("NAVIQRA_API_KEY")
IS_PRODUCTION = os.getenv("NAVIQRA_ENV", "development").lower() == "production"
ENABLE_LEGACY_V1 = os.getenv("NAVIQRA_ENABLE_LEGACY_V1", "false" if IS_PRODUCTION else "true").lower() == "true"
if IS_PRODUCTION and not API_KEY:
    raise RuntimeError("NAVIQRA_API_KEY is required when NAVIQRA_ENV=production")
if IS_PRODUCTION and len(API_KEY or "") < 32:
    raise RuntimeError("NAVIQRA_API_KEY must contain at least 32 characters in production")
if IS_PRODUCTION and not DB_PATH.startswith(("postgres://", "postgresql://")):
    raise RuntimeError("Production requires NAVIQRA_DATABASE_URL pointing to PostgreSQL")
ALLOWED_ORIGINS = [x.strip() for x in os.getenv("NAVIQRA_CORS_ORIGINS", "").split(",") if x.strip()]
ALLOWED_HOSTS = [x.strip() for x in os.getenv("NAVIQRA_ALLOWED_HOSTS", "").split(",") if x.strip()]
if IS_PRODUCTION and not ALLOWED_ORIGINS:
    raise RuntimeError("NAVIQRA_CORS_ORIGINS must list trusted frontend origins in production")
if IS_PRODUCTION and not ALLOWED_HOSTS:
    raise RuntimeError("NAVIQRA_ALLOWED_HOSTS must list trusted hostnames in production")
if IS_PRODUCTION and ("*" in ALLOWED_ORIGINS or "*" in ALLOWED_HOSTS):
    raise RuntimeError("Wildcard CORS origins and host allowlists are forbidden in production")
if IS_PRODUCTION and any(not origin.startswith("https://") for origin in ALLOWED_ORIGINS):
    raise RuntimeError("Production CORS origins must use HTTPS")
TENANT_KEYS = json.loads(os.getenv("NAVIQRA_TENANT_KEYS", "{}"))
TENANT_KEY_HASHES = json.loads(os.getenv("NAVIQRA_TENANT_KEY_HASHES", "{}"))
if IS_PRODUCTION and not TENANT_KEY_HASHES:
    raise RuntimeError("NAVIQRA_TENANT_KEY_HASHES JSON mapping is required in production")
if IS_PRODUCTION:
    for tenant, hashes in TENANT_KEY_HASHES.items():
        values = [hashes] if isinstance(hashes, str) else hashes
        if not isinstance(tenant, str) or not tenant or not isinstance(values, list) or not values:
            raise RuntimeError("Each production tenant must map to one or more SHA-256 key hashes")
        if any(not isinstance(value, str) or len(value) != 64 or
               any(ch not in "0123456789abcdef" for ch in value.lower()) for value in values):
            raise RuntimeError("Tenant key hashes must be 64-character SHA-256 hex digests")
    api_key_hash = hashlib.sha256(API_KEY.encode("utf-8")).hexdigest()
    if any(hmac.compare_digest(api_key_hash, value.lower())
           for hashes in TENANT_KEY_HASHES.values()
           for value in ([hashes] if isinstance(hashes, str) else hashes)):
        raise RuntimeError("NAVIQRA_API_KEY must be distinct from all tenant keys")
AUTO_CREATE_SCHEMA = os.getenv("NAVIQRA_DB_AUTO_CREATE", "false" if IS_PRODUCTION else "true").lower() == "true"
if IS_PRODUCTION and AUTO_CREATE_SCHEMA:
    raise RuntimeError("Production schema auto-creation is disabled; apply migrations before startup")
store = SQLiteStore(DB_PATH, initialize=AUTO_CREATE_SCHEMA)
platform_store = PlatformStore(DB_PATH, initialize=AUTO_CREATE_SCHEMA)
agent_runtime = AgentRuntime(platform_store)
orchestrator = TravelOrchestrator(store)
booking_service = BookingService(store)
provider = DemoProvider()
provider_registry = ProviderRegistry()
provider_registry.register(SandboxProvider())

@asynccontextmanager
async def lifespan(_: FastAPI):
    # Fail closed: never report a healthy service if persistence is unavailable.
    store.check()
    platform_store.check()
    yield


app = FastAPI(
    lifespan=lifespan,
    title="NAVIQRA API",
    version=__version__,
    description="Provider-neutral infrastructure for autonomous, approval-gated travel agents.",
    docs_url="/docs" if os.getenv("NAVIQRA_DOCS", "false" if IS_PRODUCTION else "true").lower() == "true" else None,
    redoc_url=None,
)
if ALLOWED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
if ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", "X-API-Key", "X-Request-ID", "X-Tenant-ID", "X-Tenant-Key"],
    )

WEB_DIR = Path(__file__).resolve().parent / "web"
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.middleware("http")
async def security_and_request_context(request: Request, call_next: Any):
    request_id = "".join(ch for ch in request.headers.get("X-Request-ID", str(uuid4()))[:128]
                         if ch.isalnum() or ch in "-_.:") or str(uuid4())
    max_body = int(os.getenv("NAVIQRA_MAX_REQUEST_BYTES", "1048576"))
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_body:
                return JSONResponse(status_code=413, content={"detail": "Request body too large",
                                                               "request_id": request_id})
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length",
                                                           "request_id": request_id})
    if request.url.path.startswith("/v1/"):
        if IS_PRODUCTION and not ENABLE_LEGACY_V1:
            return JSONResponse(status_code=404, content={"detail": "Legacy API is disabled"})
        supplied = request.headers.get("X-API-Key", "")
        if API_KEY and not hmac.compare_digest(supplied, API_KEY):
            return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key", "request_id": request_id})
    if request.url.path.startswith("/v2/"):
        tenant_id = request.headers.get("X-Tenant-ID", "")
        tenant_key = request.headers.get("X-Tenant-Key", "")
        path_parts = request.url.path.split("/")
        path_tenant = path_parts[3] if len(path_parts) > 3 and path_parts[2] == "tenants" else None
        if IS_PRODUCTION:
            accepted = TENANT_KEY_HASHES.get(tenant_id, [])
            accepted = [accepted] if isinstance(accepted, str) else accepted
            supplied_hash = hashlib.sha256(tenant_key.encode("utf-8")).hexdigest()
            authenticated = any(hmac.compare_digest(supplied_hash, stored_hash.lower()) for stored_hash in accepted)
        else:
            expected = TENANT_KEYS.get(tenant_id)
            if not TENANT_KEYS:
                expected = os.getenv("NAVIQRA_DEV_TENANT_KEY", "local-dev-key")
            authenticated = bool(expected) and hmac.compare_digest(tenant_key, expected)
        if not tenant_id or (path_tenant and path_tenant != tenant_id) or not authenticated:
            return JSONResponse(status_code=401, content={"detail": "Invalid tenant credentials", "request_id": request_id})
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "DENY"
    # API responses can contain traveler data, tenant metadata, and audit records.
    # Never allow shared or browser caches to retain authenticated API responses.
    if request.url.path.startswith(("/v1/", "/v2/")):
        response.headers["Cache-Control"] = "no-store, private, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/", include_in_schema=False)
def demo() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, str]:
    try:
        store.check()
        platform_store.check()
    except Exception:
        logger.exception("Database health check failed")
        raise HTTPException(status_code=503, detail="Database unavailable") from None
    return {"status": "ok", "version": __version__, "database": "ok",
            "authentication": "api_key_enabled" if API_KEY else "development_open"}


@app.get("/v1/platform/capabilities")
def capabilities() -> dict[str, Any]:
    return {
        "name": "NAVIQRA", "version": __version__, "license": "Apache-2.0",
        "capabilities": ["trip_planning", "traveler_memory", "provider_adapters", "approval_gated_booking"],
        "providers": [{"name": provider.name, "mode": "demo", "live_inventory": False, "booking_enabled": False}],
        "execution_policy": {"purchase_requires_approval": True, "demo_provider_is_non_bookable": True},
    }


@app.post("/v1/trips/plan")
def plan_trip(request: TripRequest):
    try:
        return orchestrator.plan_trip(request)
    except Exception:
        logger.exception("Trip planning failed")
        raise HTTPException(status_code=500, detail="Trip planning failed") from None


@app.post("/v1/memories", response_model=MemoryRecord)
def create_memory(payload: dict[str, Any]):
    required = {"traveler_id", "key", "value"}
    if not required.issubset(payload):
        raise HTTPException(status_code=422, detail=f"Required fields: {sorted(required)}")
    if not isinstance(payload["traveler_id"], str) or not 1 <= len(payload["traveler_id"]) <= 128:
        raise HTTPException(status_code=422, detail="traveler_id must be 1-128 characters")
    if not isinstance(payload["key"], str) or not 1 <= len(payload["key"]) <= 128:
        raise HTTPException(status_code=422, detail="key must be 1-128 characters")
    if len(payload) != 3:
        raise HTTPException(status_code=422, detail="Unexpected memory fields")
    return orchestrator.remember(payload["traveler_id"], payload["key"], payload["value"])


@app.get("/v1/travelers/{traveler_id}/memories", response_model=list[MemoryRecord])
def list_memories(traveler_id: str):
    return store.get_memories(traveler_id)


@app.get("/v1/providers/demo/search")
def search_demo(destination: str):
    if not destination.strip() or len(destination) > 160:
        raise HTTPException(status_code=422, detail="destination must be 1-160 characters")
    return {"results": provider.search({"destination": destination}),
            "notice": "Demo inventory only; results are not live or bookable."}


@app.post("/v1/bookings/prepare")
def prepare_booking(intent: BookingIntent):
    try:
        return booking_service.prepare(intent)
    except Exception as exc:
        if "UNIQUE constraint failed" in str(exc):
            existing = store.get_booking_by_key(intent.idempotency_key)
            if existing:
                return existing
        logger.exception("Booking intent preparation failed")
        raise HTTPException(status_code=409, detail="Could not prepare booking intent") from None


@app.post("/v1/bookings/{booking_id}/approval")
def decide_booking(booking_id: str, decision: ApprovalRequest):
    try:
        return booking_service.decide(booking_id, decision.approved, decision.approver)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# v2 enterprise intelligence APIs. Every route is tenant-key scoped by middleware.
@app.get("/v2/tenants/{tenant_id}/capabilities")
def tenant_capabilities(tenant_id: str):
    return {"platform":"NAVIQRA","api_version":"v2","tenant_id":tenant_id,
            "assets":["agent_runtime","five_tier_memory","travel_intelligence_graph",
                      "group_consensus","travel_recovery","provider_sdk",
                      "provider_registry","tenant_audit","traveler_data_deletion"],
            "booking_policy":"external_purchase_requires_explicit_user_approval",
            "inventory_mode":"demo_until_live_provider_configured"}

@app.post("/v2/tenants/{tenant_id}/memory")
def tenant_put_memory(tenant_id: str, payload: dict[str, Any]):
    required={"traveler_id","tier","key","value"}
    if set(payload) != required:
        raise HTTPException(status_code=422, detail=f"Expected exactly: {sorted(required)}")
    try:
        return platform_store.put_memory(tenant_id, payload["traveler_id"], payload["tier"],
                                         payload["key"], payload["value"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

@app.get("/v2/tenants/{tenant_id}/memory/{traveler_id}")
def tenant_get_memory(tenant_id: str, traveler_id: str):
    return platform_store.get_memories(tenant_id, traveler_id)

@app.post("/v2/tenants/{tenant_id}/graph/nodes")
def graph_add_node(tenant_id: str, payload: dict[str, Any]):
    if not isinstance(payload.get("kind"),str) or not isinstance(payload.get("label"),str):
        raise HTTPException(status_code=422, detail="kind and label are required")
    return platform_store.add_node(tenant_id,payload["kind"],payload["label"],payload.get("properties") or {})

@app.post("/v2/tenants/{tenant_id}/graph/edges")
def graph_add_edge(tenant_id: str, payload: dict[str, Any]):
    try:
        return platform_store.add_edge(tenant_id,payload["source"],payload["relation"],
                                       payload["target"],payload.get("properties") or {})
    except (KeyError,ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

@app.get("/v2/tenants/{tenant_id}/graph")
def graph_read(tenant_id: str, limit: int = 100):
    if not 1 <= limit <= 500:
        raise HTTPException(status_code=422, detail="limit must be 1..500")
    return platform_store.graph(tenant_id,limit)

@app.post("/v2/tenants/{tenant_id}/groups/votes")
def group_vote(tenant_id: str, payload: dict[str, Any]):
    try:
        return platform_store.vote(tenant_id,payload["group_id"],payload["member_id"],
                                   payload["candidate_id"],float(payload["score"]),
                                   float(payload.get("weight",1)))
    except (KeyError,ValueError,TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

@app.get("/v2/tenants/{tenant_id}/groups/{group_id}/consensus")
def group_consensus(tenant_id: str, group_id: str):
    return platform_store.consensus(tenant_id,group_id)

@app.post("/v2/tenants/{tenant_id}/agents/run")
def run_agent(tenant_id: str, payload: dict[str, Any]):
    try:
        task_type=payload["task_type"]
        agent_payload=payload.get("payload") or {}
        if not isinstance(agent_payload,dict):
            raise ValueError("payload must be an object")
        return agent_runtime.execute(tenant_id,task_type,agent_payload)
    except (KeyError,ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        logger.exception("Agent execution failed")
        raise HTTPException(status_code=500, detail="Agent execution failed") from None


# v0.4 provider interoperability, auditability and traveler data controls.
@app.get("/v2/tenants/{tenant_id}/providers")
def list_providers(tenant_id: str):
    return {"providers": provider_registry.descriptors(),
            "notice": "Sandbox inventory is synthetic and is never bookable."}

@app.post("/v2/tenants/{tenant_id}/providers/{provider_name}/search")
def provider_search(tenant_id: str, provider_name: str, payload: dict[str, Any]):
    try:
        adapter = provider_registry.get(provider_name)
        results = adapter.search(payload)
        platform_store.audit(tenant_id, "tenant_api", "provider.search", "provider",
                             provider_name, {"result_count": len(results)})
        return {"provider": provider_name, "results": results,
                "inventory_mode": "synthetic_non_bookable"}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

@app.get("/v2/tenants/{tenant_id}/audit")
def tenant_audit(tenant_id: str, limit: int = 100):
    if not 1 <= limit <= 500:
        raise HTTPException(status_code=422, detail="limit must be 1..500")
    return {"events": platform_store.audit_events(tenant_id, limit)}

@app.delete("/v2/tenants/{tenant_id}/travelers/{traveler_id}/memory")
def delete_traveler_memory(tenant_id: str, traveler_id: str):
    return platform_store.delete_traveler(tenant_id, traveler_id)


# v0.5 travel disruption recovery lifecycle
@app.post("/v2/tenants/{tenant_id}/recovery", status_code=201)
def create_recovery(tenant_id: str, payload: RecoveryCreateRequest):
    try:
        return platform_store.create_recovery(
            tenant=tenant_id,
            trip_id=payload.trip_id,
            disruption=payload.disruption,
            options=payload.options,
            context=payload.context,
        )
    except RecoveryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        logger.exception("Failed to create recovery case")
        raise HTTPException(status_code=500, detail="Failed to create recovery case") from None


@app.get("/v2/tenants/{tenant_id}/recovery")
def list_recoveries(tenant_id: str, status: str | None = None, limit: int = 100):
    if not 1 <= limit <= 500:
        raise HTTPException(status_code=422, detail="limit must be 1..500")
    return {"cases": platform_store.list_recoveries(tenant_id, status=status, limit=limit)}


@app.get("/v2/tenants/{tenant_id}/recovery/{case_id}")
def get_recovery(tenant_id: str, case_id: str):
    case = platform_store.get_recovery(tenant_id, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"Recovery case '{case_id}' not found")
    return case


@app.post("/v2/tenants/{tenant_id}/recovery/{case_id}/select")
def select_recovery_option(tenant_id: str, case_id: str, payload: RecoverySelectRequest):
    try:
        return platform_store.select_recovery_option(
            tenant_id, case_id, payload.selected_option_id, payload.notes
        )
    except RecoveryCaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RecoveryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RecoveryValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/v2/tenants/{tenant_id}/recovery/{case_id}/decision")
def decide_recovery(tenant_id: str, case_id: str, payload: RecoveryDecisionRequest):
    try:
        return platform_store.decide_recovery(
            tenant_id, case_id, payload.approved, payload.approver, payload.notes
        )
    except RecoveryCaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RecoveryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RecoveryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/v2/tenants/{tenant_id}/recovery/{case_id}/execute")
def execute_recovery(tenant_id: str, case_id: str, payload: RecoveryExecuteRequest):
    try:
        return platform_store.execute_recovery(
            tenant=tenant_id,
            case_id=case_id,
            idempotency_key=payload.idempotency_key,
            provider=payload.provider,
            simulate_failure=payload.simulate_failure,
            execution_details=payload.details,
        )
    except RecoveryCaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RecoveryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RecoveryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/v2/tenants/{tenant_id}/recovery/{case_id}/cancel")
def cancel_recovery(tenant_id: str, case_id: str, payload: RecoveryCancelRequest):
    try:
        return platform_store.cancel_recovery(
            tenant=tenant_id,
            case_id=case_id,
            reason=payload.reason,
            cancelled_by=payload.cancelled_by,
        )
    except RecoveryCaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RecoveryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RecoveryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/v2/tenants/{tenant_id}/recovery/{case_id}/audit")
def recovery_audit(tenant_id: str, case_id: str):
    try:
        return {"case_id": case_id, "events": platform_store.recovery_audit(tenant_id, case_id)}
    except RecoveryCaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

