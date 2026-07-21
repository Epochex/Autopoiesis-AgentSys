"""Production wrapper that closes the diagnosis, learning and maintenance loop."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import threading
from typing import TYPE_CHECKING, Any, Protocol, Sequence

from core.memory.index_maintenance import IndexMaintenanceWorker
from core.memory.index_projector import MemoryIndexProjector

if TYPE_CHECKING:
    from core.evolve.consolidate import ConsolidationReport


class MaintenanceWorker(Protocol):
    def start(self) -> None: ...

    def trigger(self) -> None: ...

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
                ),
                IndexMaintenanceWorker(
                    memory.vector_index_should_compact,
                    memory.compact_vector_index,
                    check_interval_seconds=maintenance_interval_seconds,
                    name="memory-vector-maintenance",
                ),
            )
        self._maintenance_workers = list(maintenance_workers)
        self._closed = False
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

    def diagnose(self, case: Any) -> tuple[Any, Any]:
        with self._request_lock:
            return self._diagnose_locked(case)

    def _diagnose_locked(self, case: Any) -> tuple[Any, Any]:
        if self._closed:
            raise RuntimeError("evolving RCA service is closed")
        # A replica catches up before reading memories. Projection failure is a
        # foreground safety failure: serving against a known-stale index would
        # be less honest than failing this request.
        if self.projector is not None:
            self.projector.sync_pending(max_batches=self.projection_max_batches)
            if self.projector.has_pending():
                raise RuntimeError(
                    "memory index remains behind the durable event stream after "
                    f"{self.projection_max_batches} projection batches"
                )

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

            self.last_consolidation = consolidate_run(
                list(self.orchestrator._run_events),
                case,
                self.orchestrator.memory,
                self.orchestrator.skills,
                list(self.orchestrator._last_evidence),
                **self.consolidation_options,
            )
            self._stats.consolidations += 1
            self._stats.last_error = None
            for worker in self._maintenance_workers:
                worker.trigger()
                self._stats.maintenance_triggers += 1
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
                "projection": self.projector.stats() if self.projector is not None else None,
                "maintenance": [worker.stats() for worker in self._maintenance_workers],
                "memory_index": self.orchestrator.memory.index_health(),
            }

    def close(self, timeout: float = 5.0) -> bool:
        with self._request_lock:
            if self._closed:
                return True
            stop_results = [worker.stop(timeout) for worker in self._maintenance_workers]
            stopped = all(stop_results)
            self._closed = True
            return stopped

    def __enter__(self) -> "EvolvingRCAService":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
