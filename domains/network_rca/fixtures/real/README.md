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

Large real logs are ignored by default. If they are sensitive, do not commit them; keep a private artifact path and point `SELFEVO_REAL_DATASET_MANIFEST` at its manifest.
