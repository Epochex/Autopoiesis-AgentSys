from __future__ import annotations

import pytest

from domains.enterprise_ops.anomaly_eval import generate_pricing_batch, run_anomaly_eval


def test_contract_gating_strictly_reduces_committed_anomaly_rate():
    result = run_anomaly_eval(n=600, seed=7)

    without = result["without_contracts"]
    with_contracts = result["with_contracts"]
    # the fixture genuinely produces would-be anomalies ...
    assert without["anomalies"] > 0
    assert without["committed"] == 600 and without["blocked"] == 0
    # ... contract gating blocks bad steps before they land ...
    assert with_contracts["blocked"] > 0
    assert with_contracts["committed"] > 0
    # ... and the committed anomaly rate drops strictly, by a measured factor
    assert with_contracts["rate"] < without["rate"]
    assert result["reduction_factor"] > 1.0


def test_anomaly_eval_is_deterministic_for_a_fixed_seed():
    assert run_anomaly_eval(n=400, seed=11) == run_anomaly_eval(n=400, seed=11)


def test_different_seeds_change_the_batch_but_not_the_contract_guarantee():
    batch_a = generate_pricing_batch(50, seed=1)
    batch_b = generate_pricing_batch(50, seed=2)
    assert batch_a != batch_b
    assert [order["id"] for order in batch_a] == [order["id"] for order in batch_b]


def test_generate_pricing_batch_rejects_non_positive_n():
    with pytest.raises(ValueError):
        generate_pricing_batch(0, seed=7)
