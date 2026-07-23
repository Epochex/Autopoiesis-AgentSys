from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from domains.active_recon.adapters.live_readonly_target import LiveAsset, LiveReadonlyTargetAdapter


def scan_assets(assets: dict[str, LiveAsset], *, timeout_sec: float = 0.35) -> dict:
    adapter = LiveReadonlyTargetAdapter(assets, timeout_sec=timeout_sec)
    results = []
    for case_id, asset in assets.items():
        evidence = adapter.query(case_id, "port_scan")
        results.append(
            {
                "case_id": case_id,
                "asset_id": asset.asset_id,
                "host": asset.host,
                "ports_checked": list(asset.ports),
                "open_services": [
                    {
                        "port": item["port"],
                        "service": item["service"],
                        "evidence_id": item["evidence_id"],
                    }
                    for item in evidence
                ],
            }
        )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "allowlisted_readonly_tcp_connect",
        "guardrails": {
            "target_discovery": False,
            "credential_probe": False,
            "exploit_probe": False,
            "configuration_write": False,
        },
        "assets": results,
    }


def _asset(value: str) -> tuple[str, LiveAsset]:
    try:
        case_id, remainder = value.split("=", 1)
        asset_id, host, ports_raw = remainder.split("@", 2)
        ports = tuple(int(item) for item in ports_raw.split(",") if item)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "asset must be CASE=ASSET_ID@IP@PORT,PORT"
        ) from exc
    return case_id, LiveAsset(asset_id=asset_id, host=host, ports=ports)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan explicitly allowlisted owned assets with TCP connect only.")
    parser.add_argument("--asset", action="append", type=_asset, required=True)
    parser.add_argument("--timeout-sec", type=float, default=0.35)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = scan_assets(dict(args.asset), timeout_sec=args.timeout_sec)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
