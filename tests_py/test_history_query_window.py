from frontend.gateway.app import history


def test_exact_event_window_is_applied_and_never_widened(monkeypatch):
    queries: list[str] = []

    def fake_query(sql: str) -> list[dict]:
        queries.append(sql)
        return [{"flows": 0}]

    monkeypatch.setattr(history, "_q", fake_query)

    result = history.device_history(
        "192.168.1.23",
        fresh=True,
        window_start="2026-09-02T11:30:00+00:00",
        window_end="2026-09-02T12:00:00+00:00",
    )

    assert result is not None
    assert result["empty"] is True
    assert result["windowStart"] == "2026-09-02T11:30:00.000+00:00"
    assert result["windowEnd"] == "2026-09-02T12:00:00.000+00:00"
    assert len(queries) == 1
    assert "INTERVAL 7 DAY" not in queries[0]
    assert "2026-09-02T11:30:00.000+00:00" in queries[0]
    assert "2026-09-02T12:00:00.000+00:00" in queries[0]
    assert "(sentbyte + rcvdbyte) > 0 OR action != 'deny'" in queries[0]


def test_destination_history_has_no_display_limit_and_orders_by_latest(monkeypatch):
    queries: list[str] = []

    def fake_query(sql: str) -> list[dict]:
        queries.append(sql)
        if "min(event_ts) AS first" in sql:
            return [{"flows": 1, "bytes": 3, "up": 1, "down": 2, "denied": 0,
                     "peers": 1, "first": "2026-09-08 00:00:00", "last": "2026-09-08 01:00:00"}]
        if "GROUP BY dstip" in sql:
            return [{"dstip": "8.8.8.8", "country": "United States", "services": ["DNS"],
                     "flows": 1, "bytes": 3, "up": 1, "down": 2, "denied": 0,
                     "last_seen": "2026-09-08 01:00:00"}]
        return []

    monkeypatch.setattr(history, "_q", fake_query)
    result = history.device_history("192.168.1.36", fresh=True)

    destination_query = next(sql for sql in queries if "GROUP BY dstip" in sql)
    assert "ORDER BY last_seen DESC" in destination_query
    assert "LIMIT" not in destination_query
    assert result["destinations"][0]["lastSeenAt"] == "2026-09-08 01:00:00"
