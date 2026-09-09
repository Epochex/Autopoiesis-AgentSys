import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "frontend" / "script" / "validate-flow-collector.py"
SPEC = importlib.util.spec_from_file_location("validate_flow_collector", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_valid_collector_parameters():
    MODULE.validate("192.168.1.27", "2055", "6343", "127.0.0.1:9091")


@pytest.mark.parametrize("address,nf,sf,metrics", [
    ("host.local", "2055", "6343", "127.0.0.1:9091"),
    ("192.168.1.27", "0", "6343", "127.0.0.1:9091"),
    ("192.168.1.27", "2055", "70000", "127.0.0.1:9091"),
    ("192.168.1.27", "2055", "6343", "0.0.0.0:9091"),
])
def test_invalid_collector_parameters(address, nf, sf, metrics):
    with pytest.raises((ValueError, TypeError)):
        MODULE.validate(address, nf, sf, metrics)
