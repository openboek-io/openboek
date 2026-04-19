-- OpenBoek — Initial schema migration
-- All tables in the public schema.

-- ===========================================================================
-- ENUM types
-- ===========================================================================

CREATE TYPE entity_type_enum AS ENUM ('zzp', 'bv', 'holding', 'personal');
CREATE TYPE relationship_type_enum AS ENUM ('holding_opco', 'fiscal_partner', 'shareholder');
CREATE TYPE access_role_enum AS ENUM ('owner', 'editor', 'viewer');
CREATE TYPE account_type_enum AS ENUM ('asset', 'liability', 'equity', 'revenue', 'expense');
CREATE TYPE journal_status_enum AS ENUM ('draft', 'posted', 'locked');
CREATE TYPE invoice_type_enum AS ENUM ('sales', 'purchase');
CREATE TYPE invoice_status_enum AS ENUM ('draft', 'sent', 'paid', 'cancelled');

-- ===========================================================================
-- Users
-- ===========================================================================

CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username        VARCHAR(150) UNIQUE NOT NULL,
    email           VARCHAR(254),
    password_hash   VARCHAR(256) NOT NULL,
    preferred_lang  VARCHAR(5) DEFAULT 'nl',
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- ===========================================================================
-- Entities
-- ===========================================================================

CREATE TABLE entities (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    entity_type     entity_type_enum NOT NULL,
    fiscal_number   VARCHAR(20),
    btw_number      VARCHAR(20),
    kvk_number      VARCHAR(20),
    address         VARCHAR(500),
    city            VARCHAR(100),
    country         VARCHAR(2) DEFAULT 'NL',
    currency        VARCHAR(3) DEFAULT 'EUR',
    owner_user_id   UUID REFERENCES users(id),
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_entities_owner ON entities(owner_user_id);

-- ===========================================================================
-- Entity relationships
-- ===========================================================================

CREATE TABLE entity_relationships (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    parent_entity_id    UUID NOT NULL REFERENCES entities(id),
    child_entity_id     UUID NOT NULL REFERENCES entities(id),
    relationship_type   relationship_type_enum NOT NULL,
    share_percentage    NUMERIC(5, 2),
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_entity_rel_parent ON entity_relationships(parent_entity_id);
CREATE INDEX idx_entity_rel_child ON entity_relationships(child_entity_id);

-- ===========================================================================
-- Entity access (user ↔ entity)
-- ===========================================================================

CREATE TABLE entity_access (
    user_id     UUID NOT NULL REFERENCES users(id),
    entity_id   UUID NOT NULL REFERENCES entities(id),
    role        access_role_enum NOT NULL,
    PRIMARY KEY (user_id, entity_id)
);

-- ===========================================================================
-- Chart of accounts
-- ===========================================================================

CREATE TABLE accounts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id       UUID NOT NULL REFERENCES entities(id),
    code            VARCHAR(20) NOT NULL,
    name_nl         VARCHAR(255) NOT NULL,
    name_en         VARCHAR(255) NOT NULL,
    account_type    account_type_enum NOT NULL,
    parent_id       UUID REFERENCES accounts(id),
    btw_code        VARCHAR(10),
    is_system       BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_accounts_entity ON accounts(entity_id);
CREATE INDEX idx_accounts_code ON accounts(entity_id, code);

-- ===========================================================================
-- Journal entries
-- ===========================================================================

CREATE TABLE journal_entries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id       UUID NOT NULL REFERENCES entities(id),
    date            DATE NOT NULL,
    reference       VARCHAR(100),
    description     TEXT,
    status          journal_status_enum DEFAULT 'draft',
    created_by      UUID REFERENCES users(id),
    posted_at       TIMESTAMPTZ,
    posted_by       UUID REFERENCES users(id),
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_journal_entries_entity_date ON journal_entries(entity_id, date);
CREATE INDEX idx_journal_entries_status ON journal_entries(status);

-- ===========================================================================
-- Journal lines
-- ===========================================================================

CREATE TABLE journal_lines (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entry_id        UUID NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE,
    account_id      UUID NOT NULL REFERENCES accounts(id),
    debit           NUMERIC(15, 2) DEFAULT 0.00,
    credit          NUMERIC(15, 2) DEFAULT 0.00,
    description     TEXT,
    currency        VARCHAR(3) DEFAULT 'EUR',
    exchange_rate   NUMERIC(18, 6) DEFAULT 1.000000,
    amount_original NUMERIC(15, 2)
);

CREATE INDEX idx_journal_lines_entry ON journal_lines(entry_id);
CREATE INDEX idx_journal_lines_account ON journal_lines(account_id);

-- ===========================================================================
-- Invoices
-- ===========================================================================

CREATE TABLE invoices (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id           UUID NOT NULL REFERENCES entities(id),
    invoice_type        invoice_type_enum NOT NULL,
    invoice_number      VARCHAR(50) NOT NULL,
    date                DATE NOT NULL,
    due_date            DATE,
    counterparty_name   VARCHAR(255) NOT NULL,
    counterparty_vat    VARCHAR(20),
    currency            VARCHAR(3) DEFAULT 'EUR',
    status              invoice_status_enum DEFAULT 'draft',
    total_excl          NUMERIC(15, 2) DEFAULT 0.00,
    total_btw           NUMERIC(15, 2) DEFAULT 0.00,
    total_incl          NUMERIC(15, 2) DEFAULT 0.00,
    pdf_path            VARCHAR(500),
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_invoices_entity ON invoices(entity_id);
CREATE INDEX idx_invoices_status ON invoices(entity_id, status);

-- ===========================================================================
-- Invoice lines
-- ===========================================================================

CREATE TABLE invoice_lines (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id      UUID NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    description     TEXT,
    quantity        NUMERIC(10, 3) DEFAULT 1.000,
    unit_price      NUMERIC(15, 2) DEFAULT 0.00,
    btw_rate        NUMERIC(5, 2) DEFAULT 21.00,
    btw_amount      NUMERIC(15, 2) DEFAULT 0.00,
    total           NUMERIC(15, 2) DEFAULT 0.00,
    account_id      UUID REFERENCES accounts(id)
);

CREATE INDEX idx_invoice_lines_invoice ON invoice_lines(invoice_id);

-- ===========================================================================
-- Bank accounts
-- ===========================================================================

CREATE TABLE bank_accounts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id       UUID NOT NULL REFERENCES entities(id),
    name            VARCHAR(255) NOT NULL,
    iban            VARCHAR(34) NOT NULL,
    currency        VARCHAR(3) DEFAULT 'EUR',
    opening_balance NUMERIC(15, 2) DEFAULT 0.00,
    current_balance NUMERIC(15, 2) DEFAULT 0.00
);

CREATE INDEX idx_bank_accounts_entity ON bank_accounts(entity_id);

-- ===========================================================================
-- Bank transactions
-- ===========================================================================

CREATE TABLE bank_transactions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bank_account_id     UUID NOT NULL REFERENCES bank_accounts(id),
    date                DATE NOT NULL,
    amount              NUMERIC(15, 2) NOT NULL,
    currency            VARCHAR(3) DEFAULT 'EUR',
    counterparty_name   VARCHAR(255),
    counterparty_iban   VARCHAR(34),
    description         TEXT,
    reference           VARCHAR(255),
    matched_entry_id    UUID REFERENCES journal_entries(id),
    import_hash         VARCHAR(64) UNIQUE NOT NULL,
    imported_at         TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_bank_tx_account ON bank_transactions(bank_account_id);
CREATE INDEX idx_bank_tx_date ON bank_transactions(bank_account_id, date);
CREATE INDEX idx_bank_tx_hash ON bank_transactions(import_hash);

-- ===========================================================================
-- Audit log (append-only)
-- ===========================================================================

CREATE TABLE audit_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID,
    entity_id       UUID,
    action          VARCHAR(100) NOT NULL,
    table_name      VARCHAR(100),
    record_id       VARCHAR(100),
    before_data     JSONB,
    after_data      JSONB,
    ip_address      VARCHAR(45),
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_audit_log_entity ON audit_log(entity_id);
CREATE INDEX idx_audit_log_created ON audit_log(created_at);

-- Prevent UPDATE and DELETE on audit_log
CREATE OR REPLACE FUNCTION audit_log_immutable()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is immutable — UPDATE and DELETE are not allowed';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_audit_log_no_update
    BEFORE UPDATE ON audit_log FOR EACH ROW
    EXECUTE FUNCTION audit_log_immutable();

CREATE TRIGGER trg_audit_log_no_delete
    BEFORE DELETE ON audit_log FOR EACH ROW
    EXECUTE FUNCTION audit_log_immutable();
