# Real Network RCA Fixtures

Put readonly R230 FortiGate syslog exports here only after removing secrets and confirming the files are safe to commit or store privately.

Expected local layout:

```text
manifest.json
syslog/fortigate-YYYYMMDD.log[.gz]
train_cases.json
heldout_cases.json
```

`heldout_cases.json` must use cases that were not used to write or tune the rule baseline. Every ground-truth block must set `"dataset_kind": "real"`.

Large real logs are ignored by default. If they are sensitive, do not commit them; keep a private artifact path and point `AUTOPOIESIS_REAL_DATASET_MANIFEST` at its manifest.

Validate the manifest:

```bash
python3 -m domains.network_rca.validate_real_dataset
```

Run held-out baselines only after validation is ready:

```bash
python3 -m domains.network_rca.eval_real_heldout domains/network_rca/fixtures/real/manifest.json
```

Optional R230 readonly ingestor service:

```bash
python3 -m pip install -e '.[ingestor]'
R230_FORTIGATE_LOG_PATHS=/data/fortigate-runtime/input/fortigate.log \
  uvicorn domains.network_rca.ingestor_app:app --host 0.0.0.0 --port 8000
```

The ingestor exposes only `GET /healthz` and `GET /logs`; it does not mutate FortiGate, R230, or local log files.
