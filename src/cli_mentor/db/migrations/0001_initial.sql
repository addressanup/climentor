-- 0001: initial schema.
-- Times are Unix seconds (REAL). JSON columns hold validated JSON text.

CREATE TABLE sessions (
    id            TEXT PRIMARY KEY,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    ended_at      REAL,
    tmux_session  TEXT,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'ended')),
    provider      TEXT,
    model         TEXT
);

CREATE TABLE session_state (
    session_id      TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    goal            TEXT,
    mode            TEXT NOT NULL DEFAULT 'guided',
    active_exercise TEXT,
    exercise_stage  TEXT,
    hint_level      INTEGER NOT NULL DEFAULT 0,
    summary         TEXT,
    open_questions  TEXT NOT NULL DEFAULT '[]',
    updated_at      REAL NOT NULL
);

-- A. Learner profile: explicit goals, preferences, constraints.
CREATE TABLE profile_facts (
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,
    value       TEXT NOT NULL,
    provenance  TEXT NOT NULL CHECK (provenance IN
                  ('user_stated', 'observed', 'model_inferred', 'default_proposed')),
    status      TEXT NOT NULL CHECK (status IN ('proposed', 'confirmed', 'rejected')),
    source      TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

-- E. Free-form memory with provenance; model inferences start as 'proposed'.
CREATE TABLE memory_items (
    id            INTEGER PRIMARY KEY,
    category      TEXT NOT NULL CHECK (category IN ('profile', 'skill', 'session', 'project', 'note')),
    text          TEXT NOT NULL,
    provenance    TEXT NOT NULL CHECK (provenance IN ('user_stated', 'observed', 'model_inferred')),
    status        TEXT NOT NULL CHECK (status IN ('proposed', 'confirmed', 'rejected', 'superseded')),
    source        TEXT,
    session_id    TEXT,
    superseded_by INTEGER REFERENCES memory_items(id) ON DELETE SET NULL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE VIRTUAL TABLE memory_fts USING fts5(text, content='memory_items', content_rowid='id');

CREATE TRIGGER memory_items_ai AFTER INSERT ON memory_items BEGIN
    INSERT INTO memory_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER memory_items_ad AFTER DELETE ON memory_items BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER memory_items_au AFTER UPDATE OF text ON memory_items BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO memory_fts(rowid, text) VALUES (new.id, new.text);
END;

-- B. Skill evidence.
CREATE TABLE attempts (
    id                 TEXT PRIMARY KEY,
    session_id         TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    exercise_id        TEXT NOT NULL,
    exercise_version   INTEGER NOT NULL,
    mode               TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('in_progress', 'passed', 'failed', 'abandoned')),
    max_assistance     INTEGER NOT NULL DEFAULT 0,
    hints_used         INTEGER NOT NULL DEFAULT 0,
    solution_shown     INTEGER NOT NULL DEFAULT 0,
    started_at         REAL NOT NULL,
    finished_at        REAL
);

CREATE TABLE skill_evidence (
    id                INTEGER PRIMARY KEY,
    skill             TEXT NOT NULL,
    attempt_id        TEXT REFERENCES attempts(id) ON DELETE CASCADE,
    exercise_id       TEXT,
    kind              TEXT NOT NULL CHECK (kind IN ('verification', 'transfer', 'explanation', 'review')),
    result            TEXT NOT NULL CHECK (result IN ('pass', 'fail', 'partial', 'unverified')),
    assistance_level  INTEGER NOT NULL,
    technique         TEXT NOT NULL DEFAULT 'not_applicable'
                        CHECK (technique IN ('verified', 'unverified', 'not_applicable')),
    provenance        TEXT NOT NULL CHECK (provenance IN ('observed', 'user_stated', 'model_inferred')),
    details           TEXT NOT NULL DEFAULT '{}',
    created_at        REAL NOT NULL
);
CREATE INDEX skill_evidence_skill ON skill_evidence(skill, created_at);

CREATE TABLE skill_state (
    skill               TEXT PRIMARY KEY,
    state               TEXT NOT NULL CHECK (state IN
                          ('introduced', 'practiced_with_assistance',
                           'demonstrated_independently', 'demonstrated_new_context')),
    independent_passes  INTEGER NOT NULL DEFAULT 0,
    assisted_passes     INTEGER NOT NULL DEFAULT 0,
    failures            INTEGER NOT NULL DEFAULT 0,
    contexts            TEXT NOT NULL DEFAULT '[]',
    review_interval_days REAL,
    next_review_at      REAL,
    updated_at          REAL NOT NULL
);

-- D. Project memory.
CREATE TABLE project_memory (
    id          INTEGER PRIMARY KEY,
    root        TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    provenance  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    UNIQUE (root, key)
);

-- Raw observations (retention-limited).
CREATE TABLE events (
    event_id     TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    source_kind  TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    ingest_index INTEGER NOT NULL,
    type         TEXT NOT NULL,
    captured_at  REAL NOT NULL,
    received_at  REAL NOT NULL,
    payload      TEXT NOT NULL
);
CREATE INDEX events_session ON events(session_id, ingest_index);
CREATE INDEX events_received ON events(received_at);

-- Conversation transcript (retention-limited).
CREATE TABLE messages (
    id                INTEGER PRIMARY KEY,
    session_id        TEXT NOT NULL,
    role              TEXT NOT NULL CHECK (role IN ('learner', 'tutor', 'note')),
    content           TEXT NOT NULL,
    trigger           TEXT,
    status            TEXT NOT NULL DEFAULT 'complete',
    context_revision  INTEGER,
    provider          TEXT,
    model             TEXT,
    created_at        REAL NOT NULL
);
CREATE INDEX messages_session ON messages(session_id, id);

CREATE TABLE permissions (
    id          INTEGER PRIMARY KEY,
    capability  TEXT NOT NULL,
    scope       TEXT NOT NULL CHECK (scope IN ('once', 'session', 'persistent')),
    resource    TEXT,
    session_id  TEXT,
    granted_at  REAL NOT NULL,
    revoked_at  REAL,
    used_at     REAL
);

CREATE TABLE usage (
    id             INTEGER PRIMARY KEY,
    session_id     TEXT,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    input_tokens   INTEGER NOT NULL,
    output_tokens  INTEGER NOT NULL,
    estimated      INTEGER NOT NULL,
    created_at     REAL NOT NULL
);

-- Files created by exercise setup, so cleanup removes only what we made.
CREATE TABLE exercise_files (
    id           INTEGER PRIMARY KEY,
    exercise_id  TEXT NOT NULL,
    path         TEXT NOT NULL,
    sha256       TEXT,
    created_at   REAL NOT NULL,
    UNIQUE (exercise_id, path)
);
