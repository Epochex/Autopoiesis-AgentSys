"""Bound rollout readiness by elapsed time and the gateway's JSON status."""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time


def positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout must be finite positive seconds") from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("timeout must be finite positive seconds")
    return seconds


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--timeout", type=positive_seconds, default=90.0)
    args = parser.parse_args()
    deadline = time.monotonic() + args.timeout
    last_error = "no completed probe"
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            # curl's total request timeout also covers slow response bodies.
            # The subprocess deadline bounds the whole attempt independently.
            result = subprocess.run(
                ["curl", "--fail", "--silent", "--show-error", "--max-time",
                 str(min(3.0, remaining)), args.url],
                capture_output=True, text=True, timeout=remaining, check=False,
            )
        except subprocess.TimeoutExpired:
            last_error = "HTTP probe timed out"
        except OSError as error:
            print(f"health probe cannot start: {type(error).__name__}", file=sys.stderr)
            return 2
        else:
            if result.returncode:
                last_error = result.stderr.strip() or f"curl exited {result.returncode}"
            else:
                try:
                    payload = json.loads(result.stdout)
                except ValueError:
                    last_error = "invalid JSON in health response"
                else:
                    if not isinstance(payload, dict):
                        last_error = "health response must be a JSON object"
                    elif payload.get("status") != "ok":
                        last_error = f"gateway status={payload.get('status', 'missing')}"
                    elif time.monotonic() <= deadline:
                        print(json.dumps(payload))
                        return 0
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(1.0, remaining))
    print(f"health deadline exceeded ({args.timeout:g}s): {last_error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
