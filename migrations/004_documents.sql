-- Unified document/transaction queue with AI auto-categorization
-- Handles both scanned receipts and bank statement imports

CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id UUID NOT NULL REFERENCES entities(id),
    user_id UUID NOT NULL REFERENCES users(id),
    source TEXT NOT NULL DEFAULT 'scan',  -- 'scan' or 'bank'
    batch_id UUID,  -- groups uploads from same session

    -- File info (for scans)
    original_filename TEXT,
    storage_path TEXT,
    mime_type TEXT,
    file_size_bytes BIGINT,

    -- OCR info (for scans)
    ocr_status TEXT DEFAULT 'pending',  -- pending, processing, completed, failed, not_needed
    ocr_result JSONB,

    -- Bank transaction link (for bank imports)
    bank_transaction_id UUID REFERENCES bank_transactions(id),

    -- Unified transaction data (from OCR or bank parse)
    vendor_name TEXT,
    transaction_date DATE,
    amount NUMERIC(15,2),
    amount_excl NUMERIC(15,2),
    btw_amount NUMERIC(15,2),
    btw_rate NUMERIC(5,2),
    description TEXT,
    counterparty_iban TEXT,

    -- AI categorization
    category TEXT,  -- business_expense, sales_income, purchase_invoice, salary, tax_payment, loan, personal, other
    account_id UUID REFERENCES accounts(id),
    ai_category TEXT,
    ai_account_suggestion TEXT,  -- account code suggested by AI
    ai_confidence NUMERIC(3,2) DEFAULT 0,
    rule_id UUID,  -- if matched by a categorization rule

    -- Status
    journal_entry_id UUID REFERENCES journal_entries(id),
    review_status TEXT NOT NULL DEFAULT 'pending',
        -- pending: waiting for OCR/parse
        -- auto_processed: AI categorized with high confidence, journal entry created
        -- needs_review: AI unsure, needs human
        -- reviewed: human confirmed
        -- skipped: human skipped
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    reviewed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_documents_entity_review ON documents(entity_id, review_status);
CREATE INDEX IF NOT EXISTS idx_documents_entity_batch ON documents(entity_id, batch_id);
CREATE INDEX IF NOT EXISTS idx_documents_bank_tx ON documents(bank_transaction_id) WHERE bank_transaction_id IS NOT NULL;

-- Categorization rules: learned from user corrections
CREATE TABLE IF NOT EXISTS categorization_rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id UUID NOT NULL REFERENCES entities(id),
    match_type TEXT NOT NULL,  -- counterparty_name, counterparty_iban, description_contains, vendor_name
    match_value TEXT NOT NULL,
    category TEXT NOT NULL,
    account_id UUID REFERENCES accounts(id),
    confidence NUMERIC(3,2) DEFAULT 1.00,
    times_used INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(entity_id, match_type, match_value)
);

CREATE INDEX IF NOT EXISTS idx_cat_rules_entity ON categorization_rules(entity_id);
CREATE INDEX IF NOT EXISTS idx_cat_rules_match ON categorization_rules(entity_id, match_type, match_value);

-- Track pattern confirmations before auto-creating rules
CREATE TABLE IF NOT EXISTS categorization_confirmations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id UUID NOT NULL REFERENCES entities(id),
    match_type TEXT NOT NULL,
    match_value TEXT NOT NULL,
    category TEXT NOT NULL,
    account_id UUID REFERENCES accounts(id),
    confirmed_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cat_confirm_pattern
    ON categorization_confirmations(entity_id, match_type, match_value);
