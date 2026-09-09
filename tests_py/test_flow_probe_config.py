import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "frontend" / "script" / "render-flow-probe.py"
SPEC = importlib.util.spec_from_file_location("render_flow_probe", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_render_is_ipfix_and_bounded():
    config = MODULE.render("eth2", "192.168.1.20", 2055, "192.168.0.0/20")
    assert "pcap_interface: eth2" in config
    assert "nfprobe_receiver[autopoiesis]: 192.168.1.20:2055" in config
    assert "nfprobe_version[autopoiesis]: 10" in config
    assert "maxlife=60:expint=5" in config
    assert (
        "pcap_filter: (net 192.168.0.0/20) and "
        "not (dst host 192.168.1.20 and udp dst port 2055)"
    ) in config


def test_render_without_cidr_still_excludes_its_own_export_stream():
    config = MODULE.render("eth2", "192.168.1.27", 2055)
    assert (
        "pcap_filter: not (dst host 192.168.1.27 and udp dst port 2055)"
    ) in config


@pytest.mark.parametrize("interface,collector,port", [("eth 2", "192.168.1.20", 2055), ("eth2", "host.local", 2055), ("eth2", "192.168.1.20", 70000)])
def test_render_rejects_unbounded_parameters(interface, collector, port):
    with pytest.raises(ValueError):
        MODULE.render(interface, collector, port)
