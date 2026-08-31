"""Network investigation domain exports, loaded only when requested."""

from typing import Any

__all__ = [
    "build_network_rca_orchestrator",
    "build_network_rca_service",
    "load_ground_truth",
    "load_seed_cases",
]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(name)
    from domains.network_rca import factory

    return getattr(factory, name)
