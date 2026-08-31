-- One-time production cutover for the event-keyed fact archive.
-- Stop autopoiesis-facts-ingest before the RENAME and restart it after the view exists.

CREATE TABLE IF NOT EXISTS autopoiesis.facts_v2
(
    event_ts DateTime64(3, 'UTC'),
    event_id String,
    device_key String,
    srcip String,
    dstip String,
    dstport UInt16,
    proto LowCardinality(String),
    action LowCardinality(String),
    service LowCardinality(String),
    app LowCardinality(String),
    type LowCardinality(String),
    subtype LowCardinality(String),
    srcintf LowCardinality(String),
    dstintf LowCardinality(String),
    dstcountry LowCardinality(String),
    srcname String,
    sentbyte UInt64,
    rcvdbyte UInt64,
    ingested_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY toYYYYMM(event_ts)
ORDER BY event_id
TTL toDateTime(event_ts) + toIntervalDay(100);

RENAME TABLE autopoiesis.facts TO autopoiesis.facts_legacy;

CREATE VIEW autopoiesis.facts AS
SELECT
    event_ts, device_key, srcip, dstip, dstport, proto, action, service, app,
    type, subtype, srcintf, dstintf, dstcountry, srcname, sentbyte, rcvdbyte
FROM autopoiesis.facts_legacy
UNION ALL
SELECT
    event_ts, device_key, srcip, dstip, dstport, proto, action, service, app,
    type, subtype, srcintf, dstintf, dstcountry, srcname, sentbyte, rcvdbyte
FROM autopoiesis.facts_v2;

