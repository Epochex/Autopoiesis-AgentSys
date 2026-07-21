from __future__ import annotations

import threading

from core.memory.index_maintenance import IndexMaintenanceWorker


def test_run_once_skips_until_policy_is_due_then_records_success():
    due = False
    calls = []
    worker = IndexMaintenanceWorker(lambda: due, lambda: calls.append("compact") or 2)

    assert not worker.run_once()
    due = True
    assert worker.run_once()
    assert calls == ["compact"]
    stats = worker.stats()
    assert stats["checks"] == 2
    assert stats["attempts"] == stats["completed"] == 1
    assert stats["skipped"] == 1
    assert stats["failures"] == 0


def test_false_result_is_an_observable_concurrent_abort():
    worker = IndexMaintenanceWorker(lambda: True, lambda: False)
    assert not worker.run_once()
    assert worker.stats()["aborted"] == 1


def test_exception_is_recorded_without_escaping_into_serving_thread():
    def fail():
        raise RuntimeError("disk full")

    worker = IndexMaintenanceWorker(lambda: True, fail)
    assert not worker.run_once()
    stats = worker.stats()
    assert stats["failures"] == 1
    assert stats["last_error"] == "RuntimeError: disk full"


def test_background_trigger_runs_and_stops_cleanly():
    completed = threading.Event()
    worker = IndexMaintenanceWorker(
        lambda: True,
        lambda: completed.set() or 1,
        check_interval_seconds=60,
    )
    worker.start()
    worker.start()
    worker.trigger()
    assert completed.wait(1)
    assert worker.stop(timeout=1)
    assert worker.stats()["completed"] == 1
