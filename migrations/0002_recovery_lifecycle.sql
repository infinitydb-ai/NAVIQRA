-- NAVIQRA Migration 0002: Complete Travel Disruption Recovery Lifecycle
-- Upgrades recovery_cases with lifecycle state, approval metadata, execution metadata,
-- resolution outcome, idempotency key tracking, and disruption context.

ALTER TABLE recovery_cases ADD COLUMN IF NOT EXISTS updated_at TEXT NOT NULL DEFAULT '';
ALTER TABLE recovery_cases ADD COLUMN IF NOT EXISTS approval_metadata TEXT NOT NULL DEFAULT '{}';
ALTER TABLE recovery_cases ADD COLUMN IF NOT EXISTS execution_metadata TEXT NOT NULL DEFAULT '{}';
ALTER TABLE recovery_cases ADD COLUMN IF NOT EXISTS resolution_outcome TEXT;
ALTER TABLE recovery_cases ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
ALTER TABLE recovery_cases ADD COLUMN IF NOT EXISTS context TEXT NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_recovery_cases_tenant_status ON recovery_cases(tenant, status, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_recovery_cases_tenant_idempotency ON recovery_cases(tenant, idempotency_key) WHERE idempotency_key IS NOT NULL;

