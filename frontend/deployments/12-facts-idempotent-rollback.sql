-- Stop autopoiesis-facts-ingest before rollback.
-- Rows written to facts_v2 after cutover remain intact for a later repair import.

DROP VIEW IF EXISTS autopoiesis.facts;
RENAME TABLE autopoiesis.facts_legacy TO autopoiesis.facts;

