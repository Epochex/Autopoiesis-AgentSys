# Autopoiesis event pipeline deployment

Build and import the image on the K3s node that runs the event pipeline:

```bash
cd /data/Autopoiesis-AgentSys
docker build -t autopoiesis-event-pipeline:v9 \
  -f frontend/deploy/event-pipeline/Dockerfile .
docker save autopoiesis-event-pipeline:v9 | sudo k3s ctr images import -
```

Create the ClickHouse credential from the current production secret source. Keep the values out of shell history and source control:

```bash
kubectl -n autopoiesis create secret generic autopoiesis-clickhouse \
  --from-literal=username='<autopoiesis user>' \
  --from-literal=password='<secret>'
```

Apply the namespace, data-plane adapters and detector:

```bash
kubectl apply -f frontend/deployments/00-namespace.yaml
kubectl apply -f frontend/deployments/11-data-plane-services.yaml
kubectl apply -f frontend/deployments/10-event-pipeline.yaml
kubectl -n autopoiesis rollout status deployment/autopoiesis-event-pipeline
```

Acceptance requires all three checks:

```bash
kubectl -n autopoiesis get deployment autopoiesis-event-pipeline
kubectl -n netops-core exec netops-redpanda-0 -c redpanda -- \
  rpk group describe autopoiesis-event-detector-v1
python3 -m json.tool /data/autopoiesis-production/status/event-pipeline.json
```

The consumer group must be stable and converge to zero lag. The status file must advance `last_event_at`, keep `last_error` empty and report persisted alerts. Production alert files live under `/data/autopoiesis-production/stream/alerts` and use stable `alert_id` names.
