-- Durable memory state, append-only change stream, and index-consumer progress.
-- The JSONB checks deliberately mirror every field in MemoryRecord.  Keeping the
-- complete record in each event makes an index rebuild independent of old code.

CREATE TABLE IF NOT EXISTS memory_records (
    memory_id          TEXT PRIMARY KEY,
    version            BIGINT NOT NULL CHECK (version >= 1),
    record             JSONB NOT NULL,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT memory_records_object_ck
        CHECK (jsonb_typeof(record) = 'object'),
    CONSTRAINT memory_records_identity_ck
        CHECK (record ->> 'memory_id' = memory_id),
    CONSTRAINT memory_records_fields_ck
        CHECK (record ?& ARRAY[
            'memory_id', 'tier', 'text', 'tags', 'asset_ids', 'evidence_ids',
            'confidence', 'quarantined', 'source_trace_ids', 'evidence_snapshot',
            'links', 'importance', 'strength', 'access_count', 'superseded_by',
            'first_observed_at', 'last_observed_at', 'event_type', 'relations',
            'config_version', 'metric_window', 'baseline_delta'
        ]),
    CONSTRAINT memory_records_tier_ck
        CHECK (record ->> 'tier' IN ('episodic', 'semantic', 'procedural', 'asset_profile')),
    CONSTRAINT memory_records_quarantined_ck
        CHECK (jsonb_typeof(record -> 'quarantined') = 'boolean'),
    CONSTRAINT memory_records_arrays_ck
        CHECK (
            jsonb_typeof(record -> 'tags') = 'array'
            AND jsonb_typeof(record -> 'asset_ids') = 'array'
            AND jsonb_typeof(record -> 'evidence_ids') = 'array'
            AND jsonb_typeof(record -> 'source_trace_ids') = 'array'
            AND jsonb_typeof(record -> 'evidence_snapshot') = 'array'
            AND jsonb_typeof(record -> 'links') = 'array'
            AND jsonb_typeof(record -> 'relations') = 'array'
        )
);

CREATE TABLE IF NOT EXISTS memory_events (
    event_offset       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    memory_id          TEXT NOT NULL REFERENCES memory_records(memory_id),
    version            BIGINT NOT NULL CHECK (version >= 1),
    event_type         TEXT NOT NULL CHECK (event_type IN ('UPSERT', 'QUARANTINE')),
    record             JSONB NOT NULL,
    occurred_at        TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT memory_events_version_uk UNIQUE (memory_id, version),
    CONSTRAINT memory_events_object_ck
        CHECK (jsonb_typeof(record) = 'object'),
    CONSTRAINT memory_events_identity_ck
        CHECK (record ->> 'memory_id' = memory_id),
    CONSTRAINT memory_events_fields_ck
        CHECK (record ?& ARRAY[
            'memory_id', 'tier', 'text', 'tags', 'asset_ids', 'evidence_ids',
            'confidence', 'quarantined', 'source_trace_ids', 'evidence_snapshot',
            'links', 'importance', 'strength', 'access_count', 'superseded_by',
            'first_observed_at', 'last_observed_at', 'event_type', 'relations',
            'config_version', 'metric_window', 'baseline_delta'
        ]),
    CONSTRAINT memory_events_tier_ck
        CHECK (record ->> 'tier' IN ('episodic', 'semantic', 'procedural', 'asset_profile')),
    CONSTRAINT memory_events_quarantined_ck
        CHECK (jsonb_typeof(record -> 'quarantined') = 'boolean'),
    CONSTRAINT memory_events_arrays_ck
        CHECK (
            jsonb_typeof(record -> 'tags') = 'array'
            AND jsonb_typeof(record -> 'asset_ids') = 'array'
            AND jsonb_typeof(record -> 'evidence_ids') = 'array'
            AND jsonb_typeof(record -> 'source_trace_ids') = 'array'
            AND jsonb_typeof(record -> 'evidence_snapshot') = 'array'
            AND jsonb_typeof(record -> 'links') = 'array'
            AND jsonb_typeof(record -> 'relations') = 'array'
        ),
    CONSTRAINT memory_events_quarantine_type_ck
        CHECK (event_type <> 'QUARANTINE' OR (record ->> 'quarantined')::BOOLEAN)
);

CREATE INDEX IF NOT EXISTS memory_records_active_tier_idx
    ON memory_records ((record ->> 'tier'))
    WHERE NOT (record ->> 'quarantined')::BOOLEAN;

CREATE INDEX IF NOT EXISTS memory_events_memory_version_idx
    ON memory_events (memory_id, version);

CREATE OR REPLACE FUNCTION reject_memory_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'memory_events is append-only';
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgname = 'memory_events_append_only'
          AND tgrelid = 'memory_events'::regclass
    ) THEN
        CREATE TRIGGER memory_events_append_only
        BEFORE UPDATE OR DELETE OR TRUNCATE ON memory_events
        FOR EACH STATEMENT EXECUTE FUNCTION reject_memory_event_mutation();
    END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS index_checkpoints (
    index_name          TEXT PRIMARY KEY CHECK (length(btrim(index_name)) > 0),
    event_offset       BIGINT NOT NULL CHECK (event_offset >= 0),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
