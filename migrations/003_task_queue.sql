-- Task queue for background processing (PostgreSQL-based, no Redis)
CREATE TABLE IF NOT EXISTS tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_type TEXT NOT NULL,
    payload JSONB DEFAULT '{}',
    status TEXT DEFAULT 'pending',  -- pending, running, completed, failed, cancelled
    priority INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    scheduled_for TIMESTAMPTZ DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error TEXT,
    retry_count INT DEFAULT 0,
    max_retries INT DEFAULT 3
);

CREATE INDEX IF NOT EXISTS idx_tasks_pending ON tasks(priority DESC, scheduled_for ASC) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_type ON tasks(task_type);
