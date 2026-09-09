-- Domain-level operational records and their immutable change stream.
CREATE TABLE IF NOT EXISTS operational_memory_records (
    kind        TEXT NOT NULL CHECK (kind IN ('incident_dossier','risk_pattern','network_feature')),
    record_id   TEXT NOT NULL CHECK (length(btrim(record_id)) > 0),
    version     BIGINT NOT NULL CHECK (version >= 1),
    payload     JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_sha256 TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (kind, record_id)
);

-- Upgrade installations created before digest-based idempotency. Existing rows
-- are intentionally left NULL and are rewritten once by the next bounded
-- commit. This avoids decoding or comparing a potentially large JSONB value on
-- the web gateway's request-serving process.
ALTER TABLE operational_memory_records
    ADD COLUMN IF NOT EXISTS payload_sha256 TEXT;

CREATE TABLE IF NOT EXISTS operational_memory_events (
    event_offset BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind         TEXT NOT NULL,
    record_id    TEXT NOT NULL,
    version      BIGINT NOT NULL CHECK (version >= 1),
    payload      JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (kind, record_id, version),
    FOREIGN KEY (kind, record_id)
      REFERENCES operational_memory_records(kind, record_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS operational_memory_records_kind_updated_idx
    ON operational_memory_records(kind, updated_at DESC);
CREATE INDEX IF NOT EXISTS operational_memory_events_record_idx
    ON operational_memory_events(kind, record_id, version);

CREATE OR REPLACE FUNCTION reject_operational_memory_event_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'operational_memory_events is append-only';
END;
$$;

DROP TRIGGER IF EXISTS operational_memory_events_append_only
    ON operational_memory_events;
CREATE TRIGGER operational_memory_events_append_only
BEFORE UPDATE OR DELETE OR TRUNCATE ON operational_memory_events
FOR EACH STATEMENT EXECUTE FUNCTION reject_operational_memory_event_mutation();
