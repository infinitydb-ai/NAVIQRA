-- NAVIQRA PostgreSQL Migration 0003: Row-Level Security (RLS)
-- Database-enforced tenant isolation using transaction-scoped context.
-- Target: PostgreSQL 16+ (Render / Supabase / Cloud SQL)

-- 1. Enable and Force Row-Level Security on all tenant-owned tables
ALTER TABLE t_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE t_memory FORCE ROW LEVEL SECURITY;

ALTER TABLE graph_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE graph_nodes FORCE ROW LEVEL SECURITY;

ALTER TABLE graph_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE graph_edges FORCE ROW LEVEL SECURITY;

ALTER TABLE group_votes ENABLE ROW LEVEL SECURITY;
ALTER TABLE group_votes FORCE ROW LEVEL SECURITY;

ALTER TABLE agent_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_runs FORCE ROW LEVEL SECURITY;

ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events FORCE ROW LEVEL SECURITY;

ALTER TABLE recovery_cases ENABLE ROW LEVEL SECURITY;
ALTER TABLE recovery_cases FORCE ROW LEVEL SECURITY;

-- 2. Explicit tenant isolation policies
-- Enforces that rows can only be accessed or modified if their tenant column
-- matches the current transaction's 'app.current_tenant' setting.
-- If 'app.current_tenant' is unset, empty, or mismatched, access is denied.

DROP POLICY IF EXISTS tenant_isolation_policy ON t_memory;
CREATE POLICY tenant_isolation_policy ON t_memory
  FOR ALL
  USING (tenant = NULLIF(current_setting('app.current_tenant', true), ''))
  WITH CHECK (tenant = NULLIF(current_setting('app.current_tenant', true), ''));

DROP POLICY IF EXISTS tenant_isolation_policy ON graph_nodes;
CREATE POLICY tenant_isolation_policy ON graph_nodes
  FOR ALL
  USING (tenant = NULLIF(current_setting('app.current_tenant', true), ''))
  WITH CHECK (tenant = NULLIF(current_setting('app.current_tenant', true), ''));

DROP POLICY IF EXISTS tenant_isolation_policy ON graph_edges;
CREATE POLICY tenant_isolation_policy ON graph_edges
  FOR ALL
  USING (tenant = NULLIF(current_setting('app.current_tenant', true), ''))
  WITH CHECK (tenant = NULLIF(current_setting('app.current_tenant', true), ''));

DROP POLICY IF EXISTS tenant_isolation_policy ON group_votes;
CREATE POLICY tenant_isolation_policy ON group_votes
  FOR ALL
  USING (tenant = NULLIF(current_setting('app.current_tenant', true), ''))
  WITH CHECK (tenant = NULLIF(current_setting('app.current_tenant', true), ''));

DROP POLICY IF EXISTS tenant_isolation_policy ON agent_runs;
CREATE POLICY tenant_isolation_policy ON agent_runs
  FOR ALL
  USING (tenant = NULLIF(current_setting('app.current_tenant', true), ''))
  WITH CHECK (tenant = NULLIF(current_setting('app.current_tenant', true), ''));

DROP POLICY IF EXISTS tenant_isolation_policy ON audit_events;
CREATE POLICY tenant_isolation_policy ON audit_events
  FOR ALL
  USING (tenant = NULLIF(current_setting('app.current_tenant', true), ''))
  WITH CHECK (tenant = NULLIF(current_setting('app.current_tenant', true), ''));

DROP POLICY IF EXISTS tenant_isolation_policy ON recovery_cases;
CREATE POLICY tenant_isolation_policy ON recovery_cases
  FOR ALL
  USING (tenant = NULLIF(current_setting('app.current_tenant', true), ''))
  WITH CHECK (tenant = NULLIF(current_setting('app.current_tenant', true), ''));

-- 3. Dedicated unprivileged application runtime role definition
-- Security requirement: No default or hardcoded credentials in migration files.
-- Role is created with NOLOGIN; production infrastructure must provision credentials
-- out-of-band via ALTER ROLE naviqra_app WITH LOGIN PASSWORD '...';
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'naviqra_app') THEN
    CREATE ROLE naviqra_app WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  ELSE
    ALTER ROLE naviqra_app WITH NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  END IF;
END
$$;

-- Allow migration / admin user to switch into naviqra_app via SET ROLE for pooled execution
GRANT naviqra_app TO CURRENT_USER;

GRANT USAGE ON SCHEMA public TO naviqra_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO naviqra_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO naviqra_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO naviqra_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO naviqra_app;
