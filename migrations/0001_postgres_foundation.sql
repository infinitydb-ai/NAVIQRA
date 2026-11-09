-- NAVIQRA PostgreSQL schema v0.5.0.
-- Apply once with a migration role before starting the API with
-- NAVIQRA_DB_AUTO_CREATE=false. Runtime role should have DML, not DDL, privileges.

CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY,
  traveler_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  key TEXT NOT NULL,
  value_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_traveler ON memories(traveler_id);

CREATE TABLE IF NOT EXISTS bookings (
  id TEXT PRIMARY KEY,
  trip_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  item_type TEXT NOT NULL,
  item_id TEXT NOT NULL,
  amount DOUBLE PRECISION NOT NULL,
  currency TEXT NOT NULL,
  status TEXT NOT NULL,
  idempotency_key TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL,
  approved_by TEXT
);

CREATE TABLE IF NOT EXISTS t_memory (
  tenant TEXT NOT NULL,
  traveler TEXT NOT NULL,
  tier TEXT NOT NULL CHECK (tier IN ('profile','episodic','semantic','procedural','active')),
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (tenant, traveler, tier, key)
);
CREATE INDEX IF NOT EXISTS idx_t_memory_lookup ON t_memory(tenant, traveler, updated_at DESC);

CREATE TABLE IF NOT EXISTS graph_nodes (
  tenant TEXT NOT NULL,
  id TEXT NOT NULL,
  kind TEXT NOT NULL,
  label TEXT NOT NULL,
  properties TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (tenant, id)
);
CREATE TABLE IF NOT EXISTS graph_edges (
  tenant TEXT NOT NULL,
  id TEXT NOT NULL,
  source TEXT NOT NULL,
  relation TEXT NOT NULL,
  target TEXT NOT NULL,
  properties TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (tenant, id),
  FOREIGN KEY (tenant, source) REFERENCES graph_nodes(tenant, id),
  FOREIGN KEY (tenant, target) REFERENCES graph_nodes(tenant, id)
);
CREATE INDEX IF NOT EXISTS idx_edges_source ON graph_edges(tenant, source);

CREATE TABLE IF NOT EXISTS group_votes (
  tenant TEXT NOT NULL,
  group_id TEXT NOT NULL,
  member_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  score DOUBLE PRECISION NOT NULL CHECK (score >= 0 AND score <= 1),
  weight DOUBLE PRECISION NOT NULL CHECK (weight > 0 AND weight <= 10),
  created_at TEXT NOT NULL,
  PRIMARY KEY (tenant, group_id, member_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS agent_runs (
  tenant TEXT NOT NULL,
  run_id TEXT NOT NULL,
  task_type TEXT NOT NULL,
  status TEXT NOT NULL,
  result TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (tenant, run_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
  tenant TEXT NOT NULL,
  event_id TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  metadata TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (tenant, event_id)
);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_time ON audit_events(tenant, created_at DESC);

CREATE TABLE IF NOT EXISTS recovery_cases (
  tenant TEXT NOT NULL,
  case_id TEXT NOT NULL,
  trip_id TEXT NOT NULL,
  disruption TEXT NOT NULL,
  options TEXT NOT NULL,
  selected_option TEXT,
  status TEXT NOT NULL CHECK (status IN ('awaiting_selection', 'approved', 'executing', 'resolved', 'rejected', 'failed', 'cancelled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT '',
  approval_metadata TEXT NOT NULL DEFAULT '{}',
  execution_metadata TEXT NOT NULL DEFAULT '{}',
  resolution_outcome TEXT,
  idempotency_key TEXT,
  context TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (tenant, case_id)
);
CREATE INDEX IF NOT EXISTS idx_recovery_cases_tenant_status ON recovery_cases(tenant, status, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_recovery_cases_tenant_idempotency ON recovery_cases(tenant, idempotency_key) WHERE idempotency_key IS NOT NULL;
