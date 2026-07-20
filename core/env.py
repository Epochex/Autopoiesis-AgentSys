"""Environment-variable compatibility for the Autopoiesis rename.

New deployments should use ``AUTOPOIESIS_*``.  The former ``SELFEVO_*``
namespace remains a read-only fallback so existing services do not break while
their environment files are being migrated.
"""

from __future__ import annotations

import os


def autopoiesis_env(suffix: str, default: str | None = None) -> str | None:
    """Return the new variable, then its legacy equivalent, then ``default``."""

    primary = f"AUTOPOIESIS_{suffix}"
    legacy = f"SELFEVO_{suffix}"
    if primary in os.environ:
        return os.environ[primary]
    if legacy in os.environ:
        return os.environ[legacy]
    return default
