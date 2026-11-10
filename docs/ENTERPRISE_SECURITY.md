# Enterprise security and production deployment

See [`PRODUCTION_DEPLOYMENT.md`](PRODUCTION_DEPLOYMENT.md) for the full runbook.

## Authentication model

The v2 API uses `X-Tenant-ID` and `X-Tenant-Key`. Production requires a 32+ character global secret and SHA-256 hashes of high-entropy tenant keys. The
legacy non-tenant-scoped `/v1` API is disabled by default in production. Static
tenant secrets are a baseline, not OIDC/SSO; enterprise identity integration is
still required for broader enterprise approval.

## Production startup gates

Production requires PostgreSQL, a pre-applied schema migration, `NAVIQRA_DB_AUTO_CREATE=false`,
explicit `NAVIQRA_ALLOWED_HOSTS`, explicit `NAVIQRA_CORS_ORIGINS`, distinct
secrets, and API docs disabled by default. Startup checks both persistence schemas.

## Operational requirements

- Managed PostgreSQL with TLS, encryption at rest, least-privilege runtime role,
  encrypted backups, and restore drills.
- Row-Level Security (RLS) enabled and forced on all seven tenant-owned tables
  (`t_memory`, `graph_nodes`, `graph_edges`, `group_votes`, `agent_runs`, `audit_events`, `recovery_cases`).
- Dedicated runtime role `naviqra_app` configured with `NOSUPERUSER NOBYPASSRLS NOLOGIN`.
  Credentials must be provisioned out-of-band via secure environment secret stores, never
  hardcoded in DDL migrations.
- Runtime role is strictly non-owner; table creation, schema modification, and policy
  alterations are restricted exclusively to migration roles.
- Superusers and roles with `BYPASSRLS` must NEVER be used by the application, as
  PostgreSQL engines bypass RLS for superusers regardless of table configuration.
- Transaction-scoped tenant setting (`app.current_tenant` with `is_local=true`)
  guaranteeing zero cross-tenant context leakage across connection pools.
- Application-level authentication (`X-Tenant-ID` / `X-Tenant-Key`) remains part of
  the Trusted Computing Base (TCB); caller-controlled GUCs do not replace gateway auth.
- TLS reverse proxy/WAF with shared rate limiting and request-size limits.
- Secret manager, key rotation, log redaction, metrics, traces, alerts, and on-call.
- Privacy/retention policy, data subject request handling, threat model, and
  independent penetration test.
- Supplier contract tests and explicit user confirmation for any real booking.

The current build does not provide OIDC/SSO, distributed rate limiting, supplier
production adapters, payment/ticketing, or independent security certification.
Do not collect passport or payment-card data. Synthetic provider results are
non-bookable.
