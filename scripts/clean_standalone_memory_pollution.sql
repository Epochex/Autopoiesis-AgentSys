\set ON_ERROR_STOP on

BEGIN;

-- Derived aggregate snapshots are rebuilt from the current ClickHouse window
-- after restart. This removes historical contributors that no longer have an
-- authoritative dossier or belong to the retired distributed data path.
CREATE TEMP TABLE polluted_operational_records ON COMMIT DROP AS
SELECT kind, record_id
FROM autopoiesis_production.operational_memory_records
WHERE (kind = 'incident_dossier' AND payload->>'fault_family' = 'failed_units')
   OR kind IN ('risk_pattern', 'network_feature');

ALTER TABLE autopoiesis_production.operational_memory_events
    DISABLE TRIGGER operational_memory_events_append_only;
DELETE FROM autopoiesis_production.operational_memory_events AS event
USING polluted_operational_records AS polluted
WHERE event.kind = polluted.kind
  AND event.record_id = polluted.record_id;
ALTER TABLE autopoiesis_production.operational_memory_events
    ENABLE TRIGGER operational_memory_events_append_only;

DELETE FROM autopoiesis_production.operational_memory_records AS record
USING polluted_operational_records AS polluted
WHERE record.kind = polluted.kind
  AND record.record_id = polluted.record_id;

CREATE TEMP TABLE polluted_memory_records ON COMMIT DROP AS
SELECT memory_id
FROM autopoiesis_production.memory_records
WHERE record::text LIKE '%fwupd-refresh.service%'
   OR record::text LIKE '%udisks2.service%'
   OR record::text LIKE '%bvaccept-%';

ALTER TABLE autopoiesis_production.memory_events
    DISABLE TRIGGER memory_events_append_only;
ALTER TABLE autopoiesis_production.memory_events
    DISABLE TRIGGER memory_events_append_only_statement;
DELETE FROM autopoiesis_production.memory_events AS event
USING polluted_memory_records AS polluted
WHERE event.memory_id = polluted.memory_id;

DELETE FROM autopoiesis_production.memory_records AS record
USING polluted_memory_records AS polluted
WHERE record.memory_id = polluted.memory_id;

ALTER TABLE autopoiesis_production.memory_events
    ENABLE TRIGGER memory_events_append_only;
ALTER TABLE autopoiesis_production.memory_events
    ENABLE TRIGGER memory_events_append_only_statement;

COMMIT;
