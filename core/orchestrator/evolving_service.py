"""Production wrapper that closes the diagnosis, learning and maintenance loop."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from typing import TYPE_CHECKING, Any, Protocol, Sequence
from uuid import uuid4

from core.memory.index_maintenance import IndexMaintenanceWorker
from core.memory.index_projector import MemoryIndexProjector

if TYPE_CHECKING:
    from core.evolve.consolidate import ConsolidationReport


class MaintenanceWorker(Protocol):
    def start(self) -> None: ...

    def trigger(self, context: dict[str, Any] | None = None) -> None: ...

    def stop(self, timeout: float = 5.0) -> bool: ...

    def stats(self) -> dict[str, Any]: ...


@dataclass
class EvolutionRuntimeStats:
    diagnoses: int = 0
    verified_runs: int = 0
    consolidations: int = 0
    consolidation_failures: int = 0
    maintenance_triggers: int = 0
    last_error: str | None = None


class EvolvingRCAService:
    """Add verified consolidation and background index upkeep to an RCA runtime.

    Diagnosis remains the foreground transaction.  A verifier rejection never
    enters consolidation.  A post-diagnosis learning failure is recorded in
    :meth:`health` and, by default, does not discard the already completed
    diagnosis; callers that require strict learning durability can enable
    ``raise_on_evolution_error``.
    """

    def __init__(
        self,
        orchestrator: Any,
        *,
        projector: MemoryIndexProjector | None = None,
        maintenance_workers: Sequence[MaintenanceWorker] | None = None,
        maintenance_interval_seconds: float = 60.0,
        start_maintenance: bool = True,
        raise_on_evolution_error: bool = False,
        projection_max_batches: int = 10,
        consolidation_options: dict[str, Any] | None = None,
    ) -> None:
        if projection_max_batches <= 0:
            raise ValueError("projection_max_batches must be positive")
        self.orchestrator = orchestrator
        self._temporary_observer_dir: TemporaryDirectory[str] | None = None
        self._observer = getattr(orchestrator, "observer", None)
        self._observer_aware_runtime = self._observer is not None
        if self._observer is None:
            from core.observability import ExecutionObserver

            self._temporary_observer_dir = TemporaryDirectory(
                prefix="autopoiesis-observability-"
            )
            self._observer = ExecutionObserver(
                Path(self._temporary_observer_dir.name) / "nodes.jsonl"
            )
        self.projector = projector
        self.projection_max_batches = projection_max_batches
        self.raise_on_evolution_error = raise_on_evolution_error
        self.consolidation_options = dict(consolidation_options or {})
        self.last_consolidation: ConsolidationReport | None = None
        self._stats = EvolutionRuntimeStats()
        # SingleAgentRCAOrchestrator keeps the current trace/evidence on the
        # instance. Serialising the complete request also prevents projection or
        # consolidation from changing memory halfway through another diagnosis.
        self._request_lock = threading.RLock()
        if maintenance_workers is None:
            memory = orchestrator.memory
            maintenance_workers = (
                IndexMaintenanceWorker(
                    lambda: bool(memory.index_health().get("compaction_due")),
                    memory.compact_index,
                    check_interval_seconds=maintenance_interval_seconds,
                    name="memory-bm25-maintenance",
                    around_run=self._observe_index_maintenance,
                ),
                IndexMaintenanceWorker(
                    memory.vector_index_should_compact,
                    memory.compact_vector_index,
                    check_interval_seconds=maintenance_interval_seconds,
                    name="memory-vector-maintenance",
                    around_run=self._observe_index_maintenance,
                ),
            )
        self._maintenance_workers = list(maintenance_workers)
        self._closed = False
        self._close_complete = False
        if start_maintenance:
            for worker in self._maintenance_workers:
                worker.start()

    @property
    def memory(self) -> Any:
        return self.orchestrator.memory

    @property
    def skills(self) -> Any:
        return self.orchestrator.skills

    @property
    def last_run_id(self) -> str:
        return self.orchestrator.last_run_id

    @property
    def _run_events(self) -> list[Any]:
        return self.orchestrator._run_events

    @property
    def _last_evidence(self) -> list[dict]:
        return self.orchestrator._last_evidence

    @property
    def observer(self) -> Any:
        return self._observer

    def diagnose(self, case: Any, *, session_id: str | None = None) -> tuple[Any, Any]:
        with self._request_lock:
            return self._diagnose_locked(case, session_id=session_id)

    def _diagnose_locked(
        self, case: Any, *, session_id: str | None = None
    ) -> tuple[Any, Any]:
        if self._closed:
            raise RuntimeError("evolving RCA service is closed")
        run_id = str(uuid4())
        with self.observer.span(
            trace_id=run_id,
            session_id=session_id,
            case_id=case.id,
            node_name="rca.evolving_run",
            node_type="workflow",
            input={
                "query": getattr(case, "query", ""),
                "assets": getattr(case, "assets", []),
            },
        ) as root_span:
            diagnosis, verification = self._run_observed(
                case, run_id=run_id, session_id=session_id
            )
            committed = self.last_consolidation is not None
            if verification.passed and not committed:
                root_span.mark_partial(self._stats.last_error or "memory consolidation did not commit")
            root_span.set_result(
                output={
                    "root_cause_key": diagnosis.root_cause_key,
                    "verified": verification.passed,
                    "memory_committed": committed,
                },
                metrics={
                    "verification_passed": verification.passed,
                    "memory_committed": committed,
                    "memory_records": len(self.memory.records()),
                },
                status="ok" if verification.passed and committed else "partial",
            )
            return diagnosis, verification

    def _run_observed(
        self,
        case: Any,
        *,
        run_id: str,
        session_id: str | None,
    ) -> tuple[Any, Any]:
        # A replica catches up before reading memories. Projection failure is a
        # foreground safety failure: serving against a known-stale index would
        # be less honest than failing this request.
        if self.projector is not None:
            with self.observer.span(
                trace_id=run_id,
                session_id=session_id,
                case_id=case.id,
                node_name="memory.project.catchup",
                node_type="index",
                input={"max_batches": self.projection_max_batches},
            ) as projection_span:
                applied = self.projector.sync_pending(
                    max_batches=self.projection_max_batches
                )
                pending = self.projector.has_pending()
                projection_span.set_result(
                    output={"pending": pending},
                    metrics={"events_applied": applied, "pending": pending},
                    attributes={"projector": self.projector.stats()},
                    status="error" if pending else "ok",
                )
                if pending:
                    raise RuntimeError(
                        "memory index remains behind the durable event stream after "
                        f"{self.projection_max_batches} projection batches"
                    )

        if self._observer_aware_runtime:
            diagnosis, verification = self.orchestrator.diagnose(
                case,
                run_id=run_id,
                session_id=session_id,
                observe_root=False,
            )
        else:
            diagnosis, verification = self.orchestrator.diagnose(case)
        self._stats.diagnoses += 1
        self.last_consolidation = None
        if not verification.passed:
            return diagnosis, verification

        self._stats.verified_runs += 1
        memory_snapshot = [
            record.model_copy(deep=True) for record in self.orchestrator.memory.records()
        ]
        skill_snapshots = {
            skill.spec.name: skill.spec.model_copy(deep=True)
            for skill in self.orchestrator.skills.all()
        }
        try:
            # Kept lazy to avoid making the core.orchestrator package import the
            # evaluation stream, which itself imports the domain factory.
            from core.evolve.consolidate import consolidate_run

            with self.observer.span(
                trace_id=run_id,
                session_id=session_id,
                case_id=case.id,
                node_name="memory.consolidate",
                node_type="memory_write",
                input={
                    "trace_events": len(self.orchestrator._run_events),
                    "evidence_count": len(self.orchestrator._last_evidence),
                },
                attributes={"options": self.consolidation_options},
            ) as consolidation_span:
                self.last_consolidation = consolidate_run(
                    list(self.orchestrator._run_events),
                    case,
                    self.orchestrator.memory,
                    self.orchestrator.skills,
                    list(self.orchestrator._last_evidence),
                    **self.consolidation_options,
                )
                report = self.last_consolidation
                consolidation_span.set_result(
                    output={
                        "added": report.added,
                        "updated": report.updated,
                        "superseded": report.superseded,
                        "reinforced": report.reinforced,
                        "quarantined": report.quarantined,
                        "linked": report.linked,
                        "insights": report.insights,
                    },
                    metrics={
                        "added": len(report.added),
                        "updated": len(report.updated),
                        "superseded": len(report.superseded),
                        "reinforced": len(report.reinforced),
                        "quarantined": len(report.quarantined),
                        "linked": len(report.linked),
                        "memory_records": len(self.memory.records()),
                    },
                    attributes={"index_health": self.memory.index_health()},
                )
            self._stats.consolidations += 1
            self._stats.last_error = None
            with self.observer.span(
                trace_id=run_id,
                session_id=session_id,
                case_id=case.id,
                node_name="index.maintenance.trigger",
                node_type="index",
                input={"workers": len(self._maintenance_workers)},
            ) as maintenance_span:
                for worker in self._maintenance_workers:
                    if isinstance(worker, IndexMaintenanceWorker):
                        worker.trigger(
                            {
                                "triggered_by_run_id": run_id,
                                "session_id": session_id,
                                "case_id": case.id,
                            }
                        )
                    else:
                        worker.trigger()
                    self._stats.maintenance_triggers += 1
                maintenance_span.set_result(
                    output={"worker_stats": [worker.stats() for worker in self._maintenance_workers]},
                    metrics={"workers_triggered": len(self._maintenance_workers)},
                )
        except Exception as exc:
            self._stats.consolidation_failures += 1
            rollback_error: Exception | None = None
            try:
                if self.orchestrator.memory.repository is not None:
                    self.orchestrator.memory.reload_from_repository()
                else:
                    self.orchestrator.memory.replace_records(memory_snapshot)
                for skill in self.orchestrator.skills.all():
                    snapshot = skill_snapshots.get(skill.spec.name)
                    if snapshot is not None:
                        skill.spec = snapshot
            except Exception as restore_exc:
                rollback_error = restore_exc
            self._stats.last_error = f"{type(exc).__name__}: {exc}"
            # The in-memory/database state was restored, so retaining the
            # pre-rollback report would falsely imply that this run committed.
            self.last_consolidation = None
            if rollback_error is not None:
                self._stats.last_error += (
                    f"; rollback failed: {type(rollback_error).__name__}: {rollback_error}"
                )
                raise RuntimeError(self._stats.last_error) from rollback_error
            if self.raise_on_evolution_error:
                raise
        return diagnosis, verification

    def health(self) -> dict[str, Any]:
        with self._request_lock:
            return {
                **asdict(self._stats),
                "closed": self._closed,
                "close_complete": self._close_complete,
                "projection": self.projector.stats() if self.projector is not None else None,
                "maintenance": [worker.stats() for worker in self._maintenance_workers],
                "memory_index": self.orchestrator.memory.index_health(),
                "observability": self.observer.health(),
            }

    def close(self, timeout: float = 5.0) -> bool:
        with self._request_lock:
            if self._close_complete:
                return True
            deadline = time.monotonic() + timeout
            stop_results = [
                worker.stop(max(0.0, deadline - time.monotonic()))
                for worker in self._maintenance_workers
            ]
            stopped = all(stop_results)
            close_observer = getattr(self.observer, "close", None)
            if callable(close_observer):
                stopped = bool(
                    close_observer(max(0.0, deadline - time.monotonic()))
                ) and stopped
            self._closed = True
            if stopped and self._temporary_observer_dir is not None:
                self._temporary_observer_dir.cleanup()
                self._temporary_observer_dir = None
            self._close_complete = stopped
            return stopped

    def _observe_index_maintenance(
        self,
        worker_name: str,
        context: dict[str, Any] | None,
        operation: Any,
    ) -> bool:
        context = context or {}
        trace_id = f"maintenance-{uuid4()}"
        case_id = str(context.get("case_id") or "index-maintenance")
        before = self.memory.index_health()
        worker = next(
            (
                item
                for item in self._maintenance_workers
                if isinstance(item, IndexMaintenanceWorker) and item.name == worker_name
            ),
            None,
        )
        worker_before = worker.stats() if worker is not None else {}
        with self.observer.span(
            trace_id=trace_id,
            session_id=context.get("session_id"),
            case_id=case_id,
            node_name=f"index.maintenance.{worker_name}",
            node_type="background",
            input={"triggered_by_run_id": context.get("triggered_by_run_id")},
        ) as maintenance_span:
            completed = bool(operation())
            after = self.memory.index_health()
            worker_after = worker.stats() if worker is not None else {}
            failure_delta = int(worker_after.get("failures", 0)) - int(
                worker_before.get("failures", 0)
            )
            abort_delta = int(worker_after.get("aborted", 0)) - int(
                worker_before.get("aborted", 0)
            )
            before_vector = before.get("vector") or {}
            after_vector = after.get("vector") or {}
            maintenance_span.set_result(
                output={
                    "completed": completed,
                    "index_health_before": before,
                    "index_health_after": after,
                    "worker_before": worker_before,
                    "worker_after": worker_after,
                },
                metrics={
                    "completed": completed,
                    "failure_delta": failure_delta,
                    "abort_delta": abort_delta,
                    "lexical_generation_delta": int(after.get("generation", 0))
                    - int(before.get("generation", 0)),
                    "vector_generation_delta": int(after_vector.get("generation", 0))
                    - int(before_vector.get("generation", 0)),
                    "obsolete_entries_after": int(after.get("obsolete_entries", 0)),
                    "vector_delta_after": int(after_vector.get("delta", 0)),
                },
                attributes={"triggered_by_run_id": context.get("triggered_by_run_id")},
                status="error" if failure_delta else "partial" if abort_delta else "ok",
            )
            if failure_delta:
                maintenance_span.error = str(
                    worker_after.get("last_error") or "index maintenance failed"
                )
            return completed

    def __enter__(self) -> "EvolvingRCAService":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
