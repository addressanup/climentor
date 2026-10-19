-- 0002: AI-designed learning plans.

CREATE TABLE learning_plans (
    id            TEXT PRIMARY KEY,
    goal          TEXT NOT NULL,
    title         TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('proposed', 'active', 'archived', 'completed')),
    outline_json  TEXT NOT NULL,
    current_step  TEXT,
    provider      TEXT,
    model         TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE plan_steps (
    plan_id       TEXT NOT NULL REFERENCES learning_plans(id) ON DELETE CASCADE,
    step_id       TEXT NOT NULL,
    position      INTEGER NOT NULL,
    module        TEXT NOT NULL,
    title         TEXT NOT NULL,
    objective     TEXT NOT NULL,
    concepts      TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'in_progress', 'done', 'skipped')),
    lesson_json   TEXT,
    completed_at  REAL,
    PRIMARY KEY (plan_id, step_id)
);
CREATE INDEX plan_steps_order ON plan_steps(plan_id, position);

ALTER TABLE session_state ADD COLUMN plan_id TEXT;
ALTER TABLE session_state ADD COLUMN phase TEXT NOT NULL DEFAULT 'ask_goal';
