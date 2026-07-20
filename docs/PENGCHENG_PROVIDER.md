# Pengcheng GPU Provider Notes

Autopoiesis-AgentSys can call the Pengcheng GPU path inherited from the NetOps research environment. All provider identity and Autopoiesis-facing configuration use Pengcheng (SSH host `pengcheng-gpu`, key `netops_pengcheng_gpu`). The boundary is intentionally provider-shaped: general agent planning should use an OpenAI-compatible chat backend, while incident-specific evidence gateways stay behind domain adapters.

## Direct OpenAI-Compatible Backend

Use this for general model calls:

```bash
AUTOPOIESIS_LLM_BASE_URL=http://127.0.0.1:28000/v1
AUTOPOIESIS_LLM_MODEL=glm-fast
AUTOPOIESIS_LLM_API_KEY=sk-local          # any non-empty token for a local server
```

These are the variables the kernel's `OpenAICompatibleClient` (`core/llm/provider.py`) reads.

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

The gateway on `http://127.0.0.1:18080/infer` is NetOps-specific. It accepts evidence-bundle requests and returns bounded incident-analysis output. Autopoiesis-AgentSys should call it only through a `netops` adapter so the core memory, compression, and policy iteration layers remain domain-neutral.

Health checks:

```bash
curl http://127.0.0.1:28000/health
curl http://127.0.0.1:18080/healthz
```

Useful scripts from the NetOps repository:

```bash
/data/Netops-causality-remediation/ops/pengcheng_gpu/start_fast_model_service.sh
/data/Netops-causality-remediation/ops/pengcheng_gpu/start_gateway.sh
/data/Netops-causality-remediation/ops/pengcheng_gpu/open_core_tunnel.sh
/data/Netops-causality-remediation/ops/pengcheng_gpu/select_a6000_gpu.py
/data/Netops-causality-remediation/ops/pengcheng_gpu/watch_connection_until_verified.sh
```

## Smoke Command

Once the OpenAI-compatible backend is reachable, point the kernel's GPU provider at it and
let the console probe it live:

```bash
export AUTOPOIESIS_GPU_BASE_URL=http://127.0.0.1:28000/v1
export AUTOPOIESIS_GPU_MODEL=glm-fast
export AUTOPOIESIS_GPU_API_KEY=sk-local          # any non-empty token for a local server
systemctl restart netops-ops-console-backend
```

`GET /api/rca/providers` then runs a live TCP reachability check and reports the
`gpu-tunnel` provider as reachable; an RCA snapshot can be produced with
`GET /api/rca/snapshot?provider=gpu-tunnel` instead of the default rule reasoner.
