#!/usr/bin/env python3
"""Fold duplicate memory records into one, keeping every piece of provenance.

Until 11 Sep a verified procedure was keyed by the asset it was confirmed on,
so the same sentence — "verified service_failed; successful checks: …" — was
stored once per demo unit and once per conflicting address: 28 copies of one
procedure, each retrieved and shown separately. The key is fixed; this folds
what was written under the old one.

Records are grouped by tier and exact text. The oldest record in a group is
kept; the others' asset ids, evidence ids, source trace ids and tags are merged
into it, its confidence takes the group's maximum, and the duplicates are
purged with a reason naming the survivor. Quarantined records are left alone.

    sudo -E ./scripts/merge_duplicate_memories.py --dry-run
    sudo -E ./scripts/merge_duplicate_memories.py
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEMO_PREFIX = "autopoiesis-acceptance-"


def load_environment() -> None:
    path = Path("/etc/autopoiesis.env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report the merge without writing")
    parser.add_argument("--tiers", default="procedural,semantic", help="tiers to fold (comma separated)")
    args = parser.parse_args()
    load_environment()
    dsn = os.getenv("AUTOPOIESIS_MEMORY_DSN", "").strip()
    if not dsn:
        parser.error("AUTOPOIESIS_MEMORY_DSN is not set")
    from core.memory.postgres_repository import PostgresMemoryRepository

    repository = PostgresMemoryRepository(dsn, schema=os.getenv("AUTOPOIESIS_MEMORY_SCHEMA", "autopoiesis_production"))
    tiers = {item.strip() for item in args.tiers.split(",") if item.strip()}
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for record, version in repository.load_versioned_records(include_quarantined=False):
        if record.tier not in tiers or record.quarantined:
            continue
        # A procedure is one per root cause and family; its sentence may differ
        # in which checks happened to be listed. Anything else folds by text.
        root = next((tag[5:] for tag in record.tags if tag.startswith("root:")), "")
        family = next((tag for tag in record.tags if tag.startswith("fam-")), "")
        key = (record.tier, f"{root}\0{family}") if record.tier == "procedural" and root else (record.tier, " ".join(record.text.split()))
        groups[key].append((record, version))

    merged = purged = 0
    for (tier, _text), members in sorted(groups.items()):
        if len(members) < 2:
            continue
        members.sort(key=lambda item: (item[0].first_observed_at or item[0].last_observed_at or "", item[0].memory_id))
        keeper, keeper_version = members[0]
        rest = members[1:]
        for record, _ in rest:
            for value in record.asset_ids:
                if value not in keeper.asset_ids and not str(value).startswith(DEMO_PREFIX):
                    keeper.asset_ids.append(value)
            for value in record.evidence_ids:
                if value not in keeper.evidence_ids:
                    keeper.evidence_ids.append(value)
            for value in record.source_trace_ids:
                if value not in keeper.source_trace_ids:
                    keeper.source_trace_ids.append(value)
            for value in record.tags:
                if value not in keeper.tags:
                    keeper.tags.append(value)
            keeper.confidence = max(float(keeper.confidence), float(record.confidence))
            if (record.last_observed_at or "") > (keeper.last_observed_at or ""):
                keeper.last_observed_at = record.last_observed_at
        keeper.asset_ids = [value for value in keeper.asset_ids if not str(value).startswith(DEMO_PREFIX)]
        print(f"[{tier}] keep {keeper.memory_id} ← fold {len(rest)}: {keeper.text[:70]}")
        if not args.dry_run:
            repository.upsert(keeper, expected_version=keeper_version)
            for record, _ in rest:
                repository.purge(record.memory_id, f"merged_into:{keeper.memory_id}", actor="merge-duplicate-memories", allow_purge=True)
        merged += 1
        purged += len(rest)
    print(f"{'would fold' if args.dry_run else 'folded'} {purged} duplicates into {merged} records")
    return 0


if __name__ == "__main__":
    sys.exit(main())
