-- Durable memory state, append-only change stream, and index-consumer progress.
-- The JSONB checks deliberately mirror every field in MemoryRecord.  Keeping the
-- complete record in each event makes an index rebuild independent of old code.
--
-- Event metadata is append-only.  Stored content has a narrower lifecycle:
-- redact_memory replaces every snapshot of one memory with the same tombstone,
-- while purge_memory removes the record and its events after writing an
-- independent, immutable purge log.  The authorization table lets the trigger
-- admit only the exact replacement or deletion prepared by those functions.

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
            'first_observed_at', 'last_observed_at', 'valid_from', 'valid_to',
            'event_type', 'relations', 'config_version', 'metric_window',
            'baseline_delta', 'redaction_reason', 'redacted_at'
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
    memory_id          TEXT NOT NULL REFERENCES memory_records(memory_id) ON DELETE CASCADE,
    version            BIGINT NOT NULL CHECK (version >= 1),
    event_type         TEXT NOT NULL CHECK (event_type IN ('UPSERT', 'QUARANTINE', 'REDACT')),
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
            'first_observed_at', 'last_observed_at', 'valid_from', 'valid_to',
            'event_type', 'relations', 'config_version', 'metric_window',
            'baseline_delta', 'redaction_reason', 'redacted_at'
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
        CHECK (
            event_type NOT IN ('QUARANTINE', 'REDACT')
            OR (record ->> 'quarantined')::BOOLEAN
        )
);

-- Upgrade installations created before REDACT and cascading purge existed.
ALTER TABLE memory_events
    DROP CONSTRAINT IF EXISTS memory_events_event_type_check;
ALTER TABLE memory_events
    ADD CONSTRAINT memory_events_event_type_check
        CHECK (event_type IN ('UPSERT', 'QUARANTINE', 'REDACT'));

ALTER TABLE memory_events
    DROP CONSTRAINT IF EXISTS memory_events_quarantine_type_ck;
ALTER TABLE memory_events
    ADD CONSTRAINT memory_events_quarantine_type_ck
        CHECK (
            event_type NOT IN ('QUARANTINE', 'REDACT')
            OR (record ->> 'quarantined')::BOOLEAN
        );

ALTER TABLE memory_events
    DROP CONSTRAINT IF EXISTS memory_events_memory_id_fkey;
ALTER TABLE memory_events
    ADD CONSTRAINT memory_events_memory_id_fkey
        FOREIGN KEY (memory_id) REFERENCES memory_records(memory_id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS memory_records_active_tier_idx
    ON memory_records ((record ->> 'tier'))
    WHERE NOT (record ->> 'quarantined')::BOOLEAN;

CREATE INDEX IF NOT EXISTS memory_events_memory_version_idx
    ON memory_events (memory_id, version);

CREATE TABLE IF NOT EXISTS memory_event_mutation_authorizations (
    backend_pid        INTEGER NOT NULL,
    transaction_id     BIGINT NOT NULL,
    memory_id          TEXT NOT NULL,
    mutation_type      TEXT NOT NULL CHECK (mutation_type IN ('REDACT', 'PURGE')),
    replacement_record JSONB,
    PRIMARY KEY (backend_pid, transaction_id, memory_id, mutation_type),
    CONSTRAINT memory_event_mutation_replacement_ck
        CHECK (
            (mutation_type = 'REDACT' AND jsonb_typeof(replacement_record) = 'object')
            OR (mutation_type = 'PURGE' AND replacement_record IS NULL)
        )
);

-- This table deliberately omits memory_id: purge removes the identity as well
-- as the content.  The log retains the accountable actor, time, reason, and the
-- number of event rows removed, which are the facts needed to audit the action.
CREATE TABLE IF NOT EXISTS memory_purge_log (
    purge_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor              TEXT NOT NULL CHECK (length(btrim(actor)) > 0),
    reason             TEXT NOT NULL CHECK (length(btrim(reason)) > 0),
    event_count        BIGINT NOT NULL CHECK (event_count >= 0),
    purged_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE OR REPLACE FUNCTION reject_memory_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path FROM CURRENT
AS $$
DECLARE
    authorized_record JSONB;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        SELECT replacement_record
        INTO authorized_record
        FROM memory_event_mutation_authorizations
        WHERE backend_pid = pg_backend_pid()
          AND transaction_id = txid_current()
          AND memory_id = OLD.memory_id
          AND mutation_type = 'REDACT';

        IF NOT FOUND
           OR NEW.event_offset IS DISTINCT FROM OLD.event_offset
           OR NEW.memory_id IS DISTINCT FROM OLD.memory_id
           OR NEW.version IS DISTINCT FROM OLD.version
           OR NEW.event_type IS DISTINCT FROM OLD.event_type
           OR NEW.occurred_at IS DISTINCT FROM OLD.occurred_at
           OR NEW.record IS DISTINCT FROM authorized_record THEN
            RAISE EXCEPTION 'memory_events is append-only';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_OP = 'DELETE' AND EXISTS (
        SELECT 1
        FROM memory_event_mutation_authorizations
        WHERE backend_pid = pg_backend_pid()
          AND transaction_id = txid_current()
          AND memory_id = OLD.memory_id
          AND mutation_type = 'PURGE'
    ) THEN
        RETURN OLD;
    END IF;

    RAISE EXCEPTION 'memory_events is append-only';
END;
$$;

DROP TRIGGER IF EXISTS memory_events_append_only ON memory_events;
CREATE TRIGGER memory_events_append_only
BEFORE UPDATE OR DELETE ON memory_events
FOR EACH ROW EXECUTE FUNCTION reject_memory_event_mutation();

-- The statement guard preserves the original fail-closed behaviour, including
-- UPDATE or DELETE statements whose predicate happens to match no rows. The row
-- guard above then verifies every authorized redaction or purge mutation.
CREATE OR REPLACE FUNCTION reject_memory_event_mutation_statement()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path FROM CURRENT
AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND EXISTS (
        SELECT 1
        FROM memory_event_mutation_authorizations
        WHERE backend_pid = pg_backend_pid()
          AND transaction_id = txid_current()
          AND mutation_type = 'REDACT'
    ) THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'DELETE' AND EXISTS (
        SELECT 1
        FROM memory_event_mutation_authorizations
        WHERE backend_pid = pg_backend_pid()
          AND transaction_id = txid_current()
          AND mutation_type = 'PURGE'
    ) THEN
        RETURN NULL;
    END IF;
    RAISE EXCEPTION 'memory_events is append-only';
END;
$$;

DROP TRIGGER IF EXISTS memory_events_append_only_statement ON memory_events;
CREATE TRIGGER memory_events_append_only_statement
BEFORE UPDATE OR DELETE ON memory_events
FOR EACH STATEMENT EXECUTE FUNCTION reject_memory_event_mutation_statement();

-- BEFORE UPDATE OR DELETE OR TRUNCATE are all guarded. TRUNCATE needs its own
-- statement trigger because PostgreSQL has no row-level TRUNCATE trigger.
CREATE OR REPLACE FUNCTION reject_memory_event_truncate()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'memory_events is append-only';
END;
$$;

DROP TRIGGER IF EXISTS memory_events_append_only_truncate ON memory_events;
CREATE TRIGGER memory_events_append_only_truncate
BEFORE TRUNCATE ON memory_events
FOR EACH STATEMENT EXECUTE FUNCTION reject_memory_event_truncate();

CREATE OR REPLACE FUNCTION reject_memory_purge_log_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'memory_purge_log is append-only';
END;
$$;

DROP TRIGGER IF EXISTS memory_purge_log_append_only ON memory_purge_log;
CREATE TRIGGER memory_purge_log_append_only
BEFORE UPDATE OR DELETE OR TRUNCATE ON memory_purge_log
FOR EACH STATEMENT EXECUTE FUNCTION reject_memory_purge_log_mutation();

CREATE OR REPLACE FUNCTION redact_memory(
    p_memory_id TEXT,
    p_expected_version BIGINT,
    p_reason TEXT
)
RETURNS TABLE (new_version BIGINT, new_event_offset BIGINT, redacted_at TIMESTAMPTZ)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path FROM CURRENT
AS $$
DECLARE
    current_version BIGINT;
    current_record JSONB;
    tombstone JSONB;
    redaction_time TIMESTAMPTZ := clock_timestamp();
    inserted_offset BIGINT;
BEGIN
    IF p_reason IS NULL OR length(btrim(p_reason)) = 0 THEN
        RAISE EXCEPTION 'redaction reason must not be empty';
    END IF;

    PERFORM pg_advisory_xact_lock(746617310630458476);
    SELECT version, record
    INTO current_version, current_record
    FROM memory_records
    WHERE memory_id = p_memory_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN;
    END IF;
    IF p_expected_version IS NOT NULL AND p_expected_version <> current_version THEN
        RAISE EXCEPTION 'memory % expected version %, current version is %',
            p_memory_id, p_expected_version, current_version;
    END IF;

    -- Historical payloads must lose the original text too.  One tombstone is
    -- used for every version so the event chain keeps identity, tier, event
    -- metadata, and observation timestamps without retaining searchable or
    -- provenance-linked content.
    tombstone := current_record || jsonb_build_object(
        'text', '[REDACTED]',
        'tags', '[]'::jsonb,
        'asset_ids', '[]'::jsonb,
        'evidence_ids', '[]'::jsonb,
        'source_trace_ids', '[]'::jsonb,
        'evidence_snapshot', '[]'::jsonb,
        'links', '[]'::jsonb,
        'relations', '[]'::jsonb,
        'metric_window', '{}'::jsonb,
        'baseline_delta', '{}'::jsonb,
        'confidence', 0,
        'importance', 0,
        'strength', 0,
        'access_count', 0,
        'superseded_by', NULL,
        'event_type', 'REDACT',
        'config_version', NULL,
        'quarantined', TRUE,
        'redaction_reason', btrim(p_reason),
        'redacted_at', redaction_time
    );

    INSERT INTO memory_event_mutation_authorizations (
        backend_pid, transaction_id, memory_id, mutation_type, replacement_record
    ) VALUES (
        pg_backend_pid(), txid_current(), p_memory_id, 'REDACT', tombstone
    );

    UPDATE memory_events
    SET record = tombstone
    WHERE memory_id = p_memory_id;

    current_version := current_version + 1;
    UPDATE memory_records
    SET version = current_version,
        record = tombstone,
        updated_at = redaction_time
    WHERE memory_id = p_memory_id;

    INSERT INTO memory_events (memory_id, version, event_type, record, occurred_at)
    VALUES (p_memory_id, current_version, 'REDACT', tombstone, redaction_time)
    RETURNING event_offset INTO inserted_offset;

    DELETE FROM memory_event_mutation_authorizations
    WHERE backend_pid = pg_backend_pid()
      AND transaction_id = txid_current()
      AND memory_id = p_memory_id
      AND mutation_type = 'REDACT';

    RETURN QUERY SELECT current_version, inserted_offset, redaction_time;
END;
$$;

CREATE OR REPLACE FUNCTION purge_memory(
    p_memory_id TEXT,
    p_actor TEXT,
    p_reason TEXT
)
RETURNS TABLE (new_purge_id BIGINT, deleted_event_count BIGINT, purge_time TIMESTAMPTZ)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path FROM CURRENT
AS $$
DECLARE
    removed_events BIGINT;
    inserted_purge_id BIGINT;
    inserted_purge_time TIMESTAMPTZ;
BEGIN
    IF p_actor IS NULL OR length(btrim(p_actor)) = 0 THEN
        RAISE EXCEPTION 'purge actor must not be empty';
    END IF;
    IF p_reason IS NULL OR length(btrim(p_reason)) = 0 THEN
        RAISE EXCEPTION 'purge reason must not be empty';
    END IF;

    PERFORM pg_advisory_xact_lock(746617310630458476);
    PERFORM 1
    FROM memory_records
    WHERE memory_id = p_memory_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;
    END IF;

    SELECT count(*) INTO removed_events
    FROM memory_events
    WHERE memory_id = p_memory_id;

    INSERT INTO memory_event_mutation_authorizations (
        backend_pid, transaction_id, memory_id, mutation_type, replacement_record
    ) VALUES (
        pg_backend_pid(), txid_current(), p_memory_id, 'PURGE', NULL
    );

    INSERT INTO memory_purge_log (actor, reason, event_count)
    VALUES (btrim(p_actor), btrim(p_reason), removed_events)
    RETURNING purge_id, purged_at INTO inserted_purge_id, inserted_purge_time;

    -- ON DELETE CASCADE reaches memory_events, whose trigger admits these rows
    -- only while this exact transaction holds the PURGE authorization.
    DELETE FROM memory_records WHERE memory_id = p_memory_id;

    DELETE FROM memory_event_mutation_authorizations
    WHERE backend_pid = pg_backend_pid()
      AND transaction_id = txid_current()
      AND memory_id = p_memory_id
      AND mutation_type = 'PURGE';

    RETURN QUERY SELECT inserted_purge_id, removed_events, inserted_purge_time;
END;
$$;

REVOKE ALL ON TABLE memory_event_mutation_authorizations FROM PUBLIC;
REVOKE ALL ON FUNCTION redact_memory(TEXT, BIGINT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION purge_memory(TEXT, TEXT, TEXT) FROM PUBLIC;

CREATE TABLE IF NOT EXISTS index_checkpoints (
    index_name          TEXT PRIMARY KEY CHECK (length(btrim(index_name)) > 0),
    event_offset       BIGINT NOT NULL CHECK (event_offset >= 0),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
