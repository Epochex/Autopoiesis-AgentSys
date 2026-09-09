#!/usr/bin/env bash
set -euo pipefail

repo=/data/Autopoiesis-AgentSys
archive=/data/archives/netops-paper/20260831T124059Z
branch=archive/distributed-redpanda-20260907

echo "distributed branch"
git -C "$repo" show-ref --verify --hash "refs/heads/$branch"

echo "distributed implementation"
git -C "$repo" ls-tree -r --name-only "$branch" | grep -E \
  'frontend/gateway/ingest/event_pipeline.py|frontend/deployments/10-event-pipeline.yaml|frontend/deployments/11-data-plane-services.yaml|frontend/deploy/event-pipeline/Dockerfile|tests_py/test_event_pipeline.py'

echo "archived k3s evidence"
sed -n '1,20p' "$archive/k3s/nodes.txt"

echo "archived redpanda topics"
sed -n '1,30p' "$archive/post-cutover/redpanda-topics.txt"

echo "archived detector consumer group"
sed -n '1,30p' "$archive/post-cutover/redpanda-event-group.txt"

echo "archived workload kinds"
python3 - \
  "$archive/k3s/edge-resources.yaml" \
  "$archive/k3s/netops-core-resources.yaml" \
  "$archive/post-cutover/k3s-autopoiesis-resources.yaml" <<'PY'
from collections import Counter
import sys
import yaml

counts = Counter()
workloads = []
for filename in sys.argv[1:]:
    with open(filename, encoding="utf-8") as handle:
        for document in yaml.safe_load_all(handle):
            if not isinstance(document, dict):
                continue
            rows = document.get("items") or [document]
            for row in rows:
                if not isinstance(row, dict):
                    continue
                kind = str(row.get("kind") or "Unknown")
                counts[kind] += 1
                if kind in {"Pod", "Deployment", "StatefulSet", "DaemonSet", "Service"}:
                    metadata = dict(row.get("metadata") or {})
                    workloads.append((
                        kind,
                        str(metadata.get("namespace") or "default"),
                        str(metadata.get("name") or ""),
                    ))
for kind, count in sorted(counts.items()):
    print(f"{kind}: {count}")
print("archived workloads")
for kind, namespace, name in sorted(set(workloads)):
    print(f"{kind} {namespace}/{name}")
PY

echo "redpanda snapshot checksums"
(
  cd "$archive/data/redpanda-pv-snapshot"
  sha256sum -c SHA256SUMS
)

echo "restored redpanda volume"
du -sh /var/lib/rancher/k3s/storage/pvc-b2772354-0751-4c7c-8023-2f8dc54359f7_netops-core_datadir-netops-redpanda-0
