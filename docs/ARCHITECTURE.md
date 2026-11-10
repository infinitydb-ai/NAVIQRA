# NAVIQRA architecture

## Design goals

1. Embeddable infrastructure: the API and orchestration layer can be integrated into an existing travel product.
2. Provider neutrality: inventory and booking providers are adapters, not assumptions embedded in agents.
3. Explicit execution boundaries: discovery and planning may be autonomous; money movement and external commitments require approval.
4. Durable traveler context: preference records are scoped to a traveler identifier and persisted behind a store abstraction.
5. Measurability: stages, outputs, latency, and benchmark methodology should be inspectable.

## Request lifecycle

`POST /v1/trips/plan` validates a typed trip request, loads traveler memory, invokes the planner, and returns a trip ID, itinerary, stages, and warnings. The v0.1 planner is deterministic so tests and baseline performance measurements are reproducible. A future model adapter may enrich the plan, but must preserve schema validation and budget checks.

## Booking lifecycle

`prepare -> pending_approval -> approved | rejected`

The idempotency key prevents duplicate intent creation. Approval records the approver. In v0.1, confirmation is a local workflow state only; it does not contact an airline, accommodation provider, or payment processor. Production adapters must revalidate price and availability at execution time and implement provider idempotency and reconciliation.

## Trust boundaries

- Treat user input, provider content, and model output as untrusted data.
- Validate all model-generated structured output against Pydantic schemas.
- Require explicit approval before purchase, cancellation, or other consequential external action.
- Do not expose provider credentials to model prompts.
- Add authentication and tenant scoping before exposing the service publicly.

## Planned production topology

API gateway -> authenticated tenant context -> orchestration service -> agent workers -> policy/approval service -> provider adapters. PostgreSQL is the planned transactional store; Redis or a durable queue can support asynchronous work. OpenTelemetry will provide traces across stages and providers. SQLite remains useful for local development and deterministic tests.


## v0.3.0 enterprise intelligence plane

The v2 tenant API fronts `PlatformStore` and `AgentRuntime`. The five capabilities are implemented as tenant-scoped persistent primitives: registered agent handlers, five-tier memory, typed graph nodes/edges, weighted group consensus, and approval-gated recovery proposals. `NaviqraClient` is the server-to-server Python SDK. Current storage is SQLite for local evaluation; production deployment requires managed PostgreSQL, OIDC/API-gateway identity, migrations, rate limiting, observability and tested provider adapters.

## Database-enforced tenant isolation (PostgreSQL RLS)

Production multi-tenancy employs defense-in-depth database enforcement via PostgreSQL Row-Level Security (RLS):

1. **Transaction-Scoped Tenant Context**:
   Application queries set `app.current_tenant` using transaction-local configuration:
   `SELECT set_config('app.current_tenant', %s, true);`
   With `is_local = true`, the parameter is strictly bound to the active transaction. When the transaction commits or rolls back, PostgreSQL automatically resets the setting, preventing context leakage across pooled or reused connections.

2. **Fail-Closed Engine Policies**:
   Policies on all seven tenant-owned tables enforce:
   `USING (tenant = NULLIF(current_setting('app.current_tenant', true), ''))`
   `WITH CHECK (tenant = NULLIF(current_setting('app.current_tenant', true), ''))`
   If `app.current_tenant` is unset, empty, or mismatched, `NULLIF` evaluates to `NULL`, resulting in zero rows returned and write rejections.

3. **Forced Table Policies**:
   All tenant tables (`t_memory`, `graph_nodes`, `graph_edges`, `group_votes`, `agent_runs`, `audit_events`, `recovery_cases`) have both `ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY` applied, ensuring non-superuser table owners are bound by RLS policies.

4. **Least-Privilege Runtime Role & Credential Provisioning**:
   The runtime application operates under a dedicated unprivileged role (`naviqra_app`) defined as `NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOLOGIN`. Migration scripts contain no default or hardcoded credentials. During production deployment, infrastructure provisioning out-of-band assigns login credentials (`ALTER ROLE naviqra_app WITH LOGIN PASSWORD '...';`) or permits `SET ROLE naviqra_app;` from pooled connections.

5. **Dual-Layer Architecture**:
   SQLite remains fully supported for local development and deterministic offline testing using explicit application-level filtering (`WHERE tenant = ?`). In PostgreSQL production environments, database engine RLS operates beneath application queries, providing guaranteed multi-tenant isolation even in the event of application logic regressions.

## Tenant context trust boundary & threat model

To maintain rigorous security architecture, the boundaries of PostgreSQL RLS with transaction-scoped GUCs are explicitly defined:

- **What RLS Protects Against**:
  1. *Application Query-Level Logic Bugs*: If developer code or query builders accidentally omit `WHERE tenant = ?`, PostgreSQL automatically restricts results to `app.current_tenant`.
  2. *Predicative SQL Injection*: Injected SQL clauses altering query predicates (e.g. `WHERE traveler_id = '' OR 1=1`) cannot breach the tenant boundary; they can only expose records belonging to the authenticated tenant.
  3. *Un-scoped Internal Queries*: Direct `SELECT * FROM table` queries accidentally run in the application context return only current tenant data.
  4. *Connection Pool Context Leakage*: Scoping to `is_local = true` guarantees that committing or rolling back a transaction completely clears the tenant setting.

- **What RLS Does NOT Protect Against**:
  1. *Arbitrary SQL / Multi-Statement Execution*: A caller with permission to execute arbitrary raw SQL statements under `naviqra_app` can execute `SELECT set_config('app.current_tenant', 'other-tenant', true)` before running queries. The GUC `app.current_tenant` is an internal parameter, not a cryptographically signed or tamper-proof identity token.
  2. *Compromised Application Runtime*: If the application server process is breached, the attacker controls the tenant parameter passed into `PlatformStore.connect(tenant)`.
  3. *PostgreSQL Superuser & BYPASSRLS Exceptions*: Under PostgreSQL engine architecture, superusers and roles with `BYPASSRLS` always bypass RLS regardless of `FORCE ROW LEVEL SECURITY`. Therefore, the application and connection pool must NEVER connect as a superuser.

- **Trusted Computing Base (TCB)**:
  Application-level authentication (verifying `X-Tenant-ID` and `X-Tenant-Key` using constant-time HMAC-SHA256 comparison against securely stored tenant secrets) remains a mandatory and non-negotiable part of the Trusted Computing Base. RLS provides defense-in-depth, not a substitute for API gateway authentication.

- **Future Migration Standards**:
  PostgreSQL does not automatically enable RLS on newly created tables. Any future migration introducing a tenant-scoped table MUST explicitly execute `ENABLE ROW LEVEL SECURITY`, `FORCE ROW LEVEL SECURITY`, `CREATE POLICY ... FOR ALL USING (...) WITH CHECK (...)`, and `GRANT SELECT, INSERT, UPDATE, DELETE ... TO naviqra_app`.
