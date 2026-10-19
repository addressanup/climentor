-- 0003: each plan (subject) owns a folder and remembers its own session.
ALTER TABLE learning_plans ADD COLUMN folder TEXT;
ALTER TABLE learning_plans ADD COLUMN session_id TEXT;
ALTER TABLE learning_plans ADD COLUMN last_used REAL;
