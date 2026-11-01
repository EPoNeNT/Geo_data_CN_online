CREATE TABLE IF NOT EXISTS certitude_descriptions (
    cache_code TEXT PRIMARY KEY,
    record JSONB NOT NULL,
    last_attempt_at TIMESTAMPTZ,
    scanned_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS certitude_checkers (
    checker_id TEXT PRIMARY KEY,
    record JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS certitude_snapshots (
    checker_id TEXT NOT NULL REFERENCES certitude_checkers (checker_id),
    observed_at TIMESTAMPTZ NOT NULL,
    record JSONB NOT NULL,
    PRIMARY KEY (checker_id, observed_at)
);
