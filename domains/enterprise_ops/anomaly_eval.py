from __future__ import annotations

import json
import random
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from core.orchestrator.intent_router import CascadingIntentRouter
from core.skills.controller import SkillAttentionController
from core.skills.registry import SkillRegistry
from core.trace.events import TraceEvent
from core.verifier.contracts import ContractVerifier
from domains.enterprise_ops.adapters.mock_system import MockEnterpriseSystem
from domains.enterprise_ops.schema import EnterpriseOpsCase
from domains.enterprise_ops.skills.ops_skills import register_enterprise_ops_skills


# Ground truth the EVALUATOR uses to call a committed record anomalous. The
# contract layer never sees this band — it only sees each order's (possibly
# corrupted) policy, which is exactly what makes the residual rate honest.
INTENDED_BAND = (60.0, 250.0)

# Anomaly-inducing tails of the request mix. These are fixture DESIGN inputs
# (how often ops feeds the pipeline bad configs), not measured outputs — the
# reported rates fall out of seeded draws plus what the contracts actually catch.
_P_CORRUPT_MULTIPLIER = 0.060  # misconfigured policy, sane bounds -> contracts CAN see it
_P_SKIP_PRICING = 0.025        # approval submitted on an unpriced order (illegal transition)
_P_CORRUPT_BOUNDS = 0.004      # policy bounds corrupted too -> contracts CANNOT see it

_FLOW_QUERIES = {
    "quote_then_approve": "按新策略报价，然后提交审批",
    "approve_unpriced": "直接提交审批",
}


def generate_pricing_batch(n: int, seed: int) -> list[dict[str, Any]]:
    """Deterministic order mix: mostly sane pricing flows plus designed bad tails.

    All randomness comes from one `random.Random(seed)` instance created here —
    no module-level RNG state, so same (n, seed) always yields the same batch.
    Raises ValueError for a non-positive batch size.
    """
    if n < 1:
        raise ValueError(f"batch size must be >= 1, got {n}")
    rng = random.Random(seed)
    lo, hi = INTENDED_BAND
    orders: list[dict[str, Any]] = []
    for index in range(n):
        draw = rng.random()
        flow = "quote_then_approve"
        if draw < _P_CORRUPT_MULTIPLIER:
            # corrupted multiplier, sane bounds: quote usually escapes the band,
            # and the pricing postcondition can see it.
            base = rng.uniform(120.0, 300.0)
            policy = _policy("corrupt_multiplier", rng.uniform(1.8, 3.0), rng.uniform(0.0, 8.0), lo, hi)
        elif draw < _P_CORRUPT_MULTIPLIER + _P_SKIP_PRICING:
            # approval requested on an unpriced order: illegal status transition.
            flow = "approve_unpriced"
            base = rng.uniform(90.0, 220.0)
            policy = _policy("standard", rng.uniform(0.9, 1.05), rng.uniform(0.0, 8.0), lo, hi)
        elif draw < _P_CORRUPT_MULTIPLIER + _P_SKIP_PRICING + _P_CORRUPT_BOUNDS:
            # policy bounds corrupted along with the multiplier: the contract's
            # own reference is wrong, so verification is structurally blind here.
            base = rng.uniform(150.0, 380.0)
            policy = _policy("corrupt_bounds", rng.uniform(2.0, 3.0), rng.uniform(0.0, 8.0), 1.0, 100000.0)
        else:
            # sane draw: quote provably lands inside the intended band.
            base = rng.uniform(90.0, 220.0)
            policy = _policy("standard", rng.uniform(0.9, 1.05), rng.uniform(0.0, 8.0), lo, hi)
        order_id = f"order_{index:04d}"
        orders.append(
            {
                "id": order_id,
                "flow": flow,
                "state": {
                    "asset": order_id,
                    "base_price": round(base, 2),
                    "policy": policy,
                    "pricing_status": "unpriced",
                    "quote_price": None,
                    "approval_submitted": False,
                    "status": "draft",
                    "reminder_sent": False,
                },
            }
        )
    return orders


def run_anomaly_eval(n: int = 2000, seed: int = 7) -> dict[str, Any]:
    """Measure the committed-record anomaly rate without vs with contract gating.

    The identical routed batch runs twice through the enterprise_ops pipeline:
    (A) every step commits unverified; (B) `ContractVerifier.check_step` gates
    every step and a failing step is rolled back before it lands. Rates are
    measured against `INTENDED_BAND` ground truth — never hard-coded.
    """
    orders = generate_pricing_batch(n, seed)
    without = _run_batch(orders, verify=False)
    with_contracts = _run_batch(orders, verify=True)
    factor = without["rate"] / with_contracts["rate"] if with_contracts["rate"] > 0 else float("inf")
    return {
        "n": n,
        "seed": seed,
        "without_contracts": without,
        "with_contracts": with_contracts,
        "reduction_factor": factor,
    }


def _run_batch(orders: list[dict[str, Any]], *, verify: bool) -> dict[str, Any]:
    """Route + execute the batch; with `verify`, gate every step and roll back failures."""
    with TemporaryDirectory() as tmp_dir:
        fixture = Path(tmp_dir) / "anomaly_fixture.json"
        fixture.write_text(
            json.dumps({order["id"]: order["state"] for order in orders}, sort_keys=True),
            encoding="utf-8",
        )
        registry = SkillRegistry()
        adapter = MockEnterpriseSystem.from_path(fixture)
        register_enterprise_ops_skills(registry, adapter)
        router = CascadingIntentRouter(
            registry,
            SkillAttentionController(enabled=True, top_k=4),
            _InMemoryLedger(),
            enable_induction=False,
        )
        verifier = ContractVerifier()

        committed = 0
        anomalies = 0
        blocked_orders = 0
        for order in orders:
            case = EnterpriseOpsCase(
                id=order["id"],
                query=_FLOW_QUERIES[order["flow"]],
                query_terms=[],
                assets=[order["state"]["asset"]],
                relevant_skills=[],
            )
            outcome = router.route(case)
            if not (outcome.resolved and outcome.chain):
                raise RuntimeError(f"anomaly batch request failed to route: {order['id']} -> {outcome.tier}")
            landed_any = False
            for name in outcome.chain:
                before = adapter.snapshot(case.id)
                result = registry.execute(name, case=case)
                after = adapter.snapshot(case.id)
                if verify:
                    verdict = verifier.check_step(registry.get(name), before, {"case": case}, after, result)
                    if not verdict.passed:
                        adapter.restore(case.id, before)
                        break
                landed_any = True
            if not landed_any:
                blocked_orders += 1
                continue
            committed += 1
            if _is_anomalous(adapter.snapshot(case.id)):
                anomalies += 1

        rate = anomalies / committed if committed else 0.0
        return {
            "mode": "with_contracts" if verify else "without_contracts",
            "requests": len(orders),
            "committed": committed,
            "blocked": blocked_orders,
            "anomalies": anomalies,
            "rate": rate,
        }


def _is_anomalous(state: dict[str, Any]) -> bool:
    """Ground-truth check on a committed record: bad price or illegal state."""
    lo, hi = INTENDED_BAND
    quote = state.get("quote_price")
    if quote is not None and not (lo <= float(quote) <= hi):
        return True
    approval_landed = state.get("approval_submitted") is True or state.get("status") == "pending_approval"
    return approval_landed and state.get("pricing_status") != "quoted"


def _policy(name: str, multiplier: float, discount: float, min_price: float, max_price: float) -> dict[str, Any]:
    return {
        "name": name,
        "multiplier": round(multiplier, 4),
        "discount": round(discount, 2),
        "min_price": min_price,
        "max_price": max_price,
    }


class _InMemoryLedger:
    """Trace sink for high-volume eval batches (no per-event fsync)."""

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def append(self, event: TraceEvent) -> None:
        self.events.append(event)


def _print_report(result: dict[str, Any]) -> None:
    print(f"pricing anomaly eval: n={result['n']} seed={result['seed']}")
    print(f"{'mode':<20} {'committed':>9} {'blocked':>7} {'anomalies':>9} {'rate':>8}")
    for key in ("without_contracts", "with_contracts"):
        row = result[key]
        print(
            f"{row['mode']:<20} {row['committed']:>9} {row['blocked']:>7} "
            f"{row['anomalies']:>9} {row['rate']:>8.2%}"
        )
    print(f"reduction factor: {result['reduction_factor']:.1f}x")


if __name__ == "__main__":
    _print_report(run_anomaly_eval())
