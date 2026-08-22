"""Cross-cutting safety predicates: things no layer may decide on its own."""

from core.safety.tailscale import any_tailscale_target, is_tailscale_target, refusal

__all__ = ["any_tailscale_target", "is_tailscale_target", "refusal"]
