# Pengcheng GPU Provider

Pengcheng is an optional OpenAI-compatible model endpoint for Autopoiesis investigations. The production service depends only on the HTTP contract and the `AUTOPOIESIS_GPU_*` settings. Tunnel creation and accelerator scheduling belong to the provider side. The Autopoiesis repository contains the complete client-side integration.

## Provider contract

```bash
AUTOPOIESIS_GPU_BASE_URL=http://127.0.0.1:28000/v1
AUTOPOIESIS_GPU_MODEL=glm-fast
AUTOPOIESIS_GPU_API_KEY=sk-local
```

The configured endpoint must accept OpenAI-compatible chat requests:

```json
{
  "model": "glm-fast",
  "messages": [{ "role": "user", "content": "Return JSON." }],
  "temperature": 0,
  "max_tokens": 1200,
  "response_format": { "type": "json_object" }
}
```

The provider supplies model output. Autopoiesis owns incident context compilation, tool selection, evidence state, memory updates and action control.

## Health check

After the provider-side tunnel is ready:

```bash
curl http://127.0.0.1:28000/health
systemctl restart autopoiesis-gateway
curl http://127.0.0.1:8026/api/rca/providers
```

`GET /api/rca/providers` performs the live reachability check. A snapshot can select the provider through `GET /api/rca/snapshot?provider=gpu-tunnel`. The rule-based path remains available when this optional endpoint is offline.
