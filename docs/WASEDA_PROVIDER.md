# Waseda GPU Provider Notes

selfevo-orchiter can call the Waseda GPU path inherited from the NetOps research environment, but the boundary is intentionally provider-shaped. General agent planning should use an OpenAI-compatible chat backend; incident-specific evidence gateways should stay behind domain adapters.

## Direct OpenAI-Compatible Backend

Use this for general model calls:

```bash
SELFEVO_MODEL_BASE_URL=http://127.0.0.1:28000/v1
SELFEVO_MODEL_ID=glm-fast
SELFEVO_MODEL_PROVIDER=waseda-gpu
```

The legacy `HELIX_MODEL_*` variables are still accepted by older scripts, but new documentation should prefer `SELFEVO_MODEL_*`.

Request shape:

```json
{
  "model": "glm-fast",
  "messages": [{ "role": "user", "content": "Return JSON." }],
  "temperature": 0,
  "max_tokens": 1200,
  "response_format": { "type": "json_object" }
}
```

## NetOps Evidence Gateway

The gateway on `http://127.0.0.1:18080/infer` is NetOps-specific. It accepts evidence-bundle requests and returns bounded incident-analysis output. selfevo-orchiter should call it only through a `netops` adapter so the core memory, compression, and policy iteration layers remain domain-neutral.

Health checks:

```bash
curl http://127.0.0.1:28000/health
curl http://127.0.0.1:18080/healthz
```

Useful scripts from the NetOps repository:

```bash
/data/Netops-causality-remediation/ops/waseda_gpu/start_fast_model_service.sh
/data/Netops-causality-remediation/ops/waseda_gpu/start_gateway.sh
/data/Netops-causality-remediation/ops/waseda_gpu/open_core_tunnel.sh
/data/Netops-causality-remediation/ops/waseda_gpu/select_a6000_gpu.py
/data/Netops-causality-remediation/ops/waseda_gpu/watch_connection_until_verified.sh
```

## Smoke Command

Once the OpenAI-compatible backend is reachable:

```bash
SELFEVO_MODEL_BASE_URL=http://127.0.0.1:28000/v1 \
SELFEVO_MODEL_ID=glm-fast \
npm run provider:smoke
```

The command runs health and a tiny JSON sentinel chat. For CI-style opt-in testing:

```bash
SELFEVO_RUN_PROVIDER_SMOKE=1 npm run test:provider
```
