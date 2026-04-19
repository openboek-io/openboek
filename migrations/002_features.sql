-- OpenBoek — Feature migration: Scanner, GoCardless, Insights, Verification, Tax
-- Run after 001_initial.sql

-- ===========================================================================
-- Insights table (for Proactive AI Advisor)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS insights (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id       UUID NOT NULL REFERENCES entities(id),
    user_id         UUID REFERENCES users(id),
    category        VARCHAR(50) NOT NULL,
    title_nl        TEXT NOT NULL,
    title_en        TEXT NOT NULL,
    description_nl  TEXT NOT NULL,
    description_en  TEXT NOT NULL,
    impact_eur      NUMERIC(15,2),
    risk_level      VARCHAR(30) DEFAULT 'safe',
    legal_basis     TEXT,
    recommended_action_nl TEXT,
    recommended_action_en TEXT,
    status          VARCHAR(20) DEFAULT 'active',
    expires_at      TIMESTAMPTZ,
    dismissed_at    TIMESTAMPTZ,
    snoozed_until   DATE,
    resolved_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_insights_entity ON insights(entity_id);
CREATE INDEX IF NOT EXISTS idx_insights_status ON insights(status);

-- ===========================================================================
-- Verification sign-offs
-- ===========================================================================

CREATE TABLE IF NOT EXISTS verification_signoffs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id       UUID NOT NULL REFERENCES entities(id),
    period_type     VARCHAR(10) NOT NULL,
    period_year     INTEGER NOT NULL,
    period_q        INTEGER,
    automated_checks JSONB DEFAULT '{}',
    ai_review       JSONB DEFAULT '{}',
    signoff_user_id UUID REFERENCES users(id),
    signoff_at      TIMESTAMPTZ,
    status          VARCHAR(20) DEFAULT 'pending',
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_verification_entity ON verification_signoffs(entity_id);

-- ===========================================================================
-- Receipt files (for scanner)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS receipt_files (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id       UUID NOT NULL REFERENCES entities(id),
    uploaded_by     UUID REFERENCES users(id),
    original_filename TEXT,
    storage_path    TEXT NOT NULL,
    mime_type       VARCHAR(100),
    file_size       BIGINT,
    ocr_status      VARCHAR(20) DEFAULT 'pending',
    ocr_result      JSONB,
    journal_entry_id UUID REFERENCES journal_entries(id),
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_receipt_files_entity ON receipt_files(entity_id);

-- ===========================================================================
-- GoCardless bank connections
-- ===========================================================================

CREATE TABLE IF NOT EXISTS gocardless_connections (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id       UUID NOT NULL REFERENCES entities(id),
    requisition_id  TEXT NOT NULL,
    institution_id  TEXT,
    institution_name TEXT,
    account_ids     JSONB DEFAULT '[]',
    status          VARCHAR(30) DEFAULT 'pending',
    access_valid_until DATE,
    created_at      TIMESTAMPTZ DEFAULT now(),
    last_synced_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_gc_connections_entity ON gocardless_connections(entity_id);

-- ===========================================================================
-- Fiscal partner optimization results
-- ===========================================================================

CREATE TABLE IF NOT EXISTS fiscal_partner_optimizations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id       UUID NOT NULL REFERENCES entities(id),
    fiscal_year     INTEGER NOT NULL,
    partner_a_name  TEXT,
    partner_b_name  TEXT,
    scenario_a      JSONB DEFAULT '{}',
    scenario_b      JSONB DEFAULT '{}',
    optimal         JSONB DEFAULT '{}',
    saving_vs_equal NUMERIC(15,2),
    created_at      TIMESTAMPTZ DEFAULT now()
);
