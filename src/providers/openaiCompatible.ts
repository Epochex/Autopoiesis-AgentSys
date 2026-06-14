import type { ChatJsonResult, ChatMessage, JsonModelClient, ProviderHealth } from "./types.js";

export interface OpenAICompatibleConfig {
  baseUrl: string;
  apiKey?: string | undefined;
  model: string;
  provider: string;
  timeoutMs: number;
  healthUrl?: string | undefined;
}

export class OpenAICompatibleJsonClient implements JsonModelClient {
  constructor(private readonly config: OpenAICompatibleConfig) {}

  static fromEnv(env: NodeJS.ProcessEnv = process.env): OpenAICompatibleJsonClient {
    return new OpenAICompatibleJsonClient({
      baseUrl: env.SELFEVO_MODEL_BASE_URL ?? env.HELIX_MODEL_BASE_URL ?? "http://127.0.0.1:28000/v1",
      apiKey: env.SELFEVO_MODEL_API_KEY ?? env.HELIX_MODEL_API_KEY,
      model: env.SELFEVO_MODEL_ID ?? env.HELIX_MODEL_ID ?? "glm-fast",
      provider: env.SELFEVO_MODEL_PROVIDER ?? env.HELIX_MODEL_PROVIDER ?? "waseda-gpu",
      timeoutMs: Number(env.SELFEVO_MODEL_TIMEOUT_MS ?? env.HELIX_MODEL_TIMEOUT_MS ?? 120_000),
      healthUrl: env.SELFEVO_MODEL_HEALTH_URL ?? env.HELIX_MODEL_HEALTH_URL,
    });
  }

  async chatJson(messages: ChatMessage[], options: { maxTokens?: number; temperature?: number } = {}): Promise<ChatJsonResult> {
    const started = performance.now();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.config.timeoutMs);
    let status = 0;
    let rawText = "";
    let usage = {};
    let error = "";
    try {
      const response = await fetch(`${this.config.baseUrl.replace(/\/$/, "")}/chat/completions`, {
        method: "POST",
        signal: controller.signal,
        headers: {
          "Content-Type": "application/json",
          ...(this.config.apiKey ? { Authorization: `Bearer ${this.config.apiKey}` } : {}),
        },
        body: JSON.stringify({
          model: this.config.model,
          messages,
          temperature: options.temperature ?? 0,
          max_tokens: options.maxTokens ?? 1200,
          response_format: { type: "json_object" },
        }),
      });
      status = response.status;
      const responseText = await response.text();
      const payload = parseMaybeObject(responseText);
      usage = isRecord(payload.usage) ? payload.usage : {};
      const choices = Array.isArray(payload.choices) ? payload.choices : [];
      const first = isRecord(choices[0]) ? choices[0] : {};
      const message = isRecord(first.message) ? first.message : {};
      rawText = String(message.content ?? message.reasoning_content ?? "");
      if (!response.ok) error = JSON.stringify(payload).slice(0, 500);
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    } finally {
      clearTimeout(timeout);
    }
    const parsed = parseJsonObject(rawText);
    if (!parsed && !error) error = "model did not return parseable JSON object";
    return {
      parsed: parsed ?? {},
      metadata: {
        provider: this.config.provider,
        model: this.config.model,
        status,
        latency_ms: Math.round((performance.now() - started) * 100) / 100,
        usage,
        ...(error ? { error } : {}),
        raw_text_chars: rawText.length,
      },
    };
  }

  async healthCheck(): Promise<ProviderHealth> {
    const endpoint = this.config.healthUrl ?? deriveHealthUrl(this.config.baseUrl);
    const started = performance.now();
    try {
      const response = await fetch(endpoint);
      return {
        ok: response.ok,
        status: response.status,
        latency_ms: Math.round((performance.now() - started) * 100) / 100,
        endpoint,
      };
    } catch (caught) {
      return {
        ok: false,
        status: 0,
        latency_ms: Math.round((performance.now() - started) * 100) / 100,
        endpoint,
        error: caught instanceof Error ? caught.message : String(caught),
      };
    }
  }
}

export function deriveHealthUrl(baseUrl: string): string {
  const trimmed = baseUrl.replace(/\/$/, "");
  if (trimmed.endsWith("/v1")) return `${trimmed.slice(0, -3)}/health`;
  return `${trimmed}/health`;
}

function parseMaybeObject(text: string): Record<string, unknown> {
  try {
    const parsed = JSON.parse(text) as unknown;
    return isRecord(parsed) ? parsed : {};
  } catch {
    return { error: text.slice(0, 500) };
  }
}

function parseJsonObject(text: string): Record<string, unknown> | undefined {
  const trimmed = text.trim();
  if (!trimmed) return undefined;
  const candidates = [trimmed];
  const match = trimmed.match(/\{[\s\S]*\}/);
  if (match && match[0] !== trimmed) candidates.push(match[0]);
  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(candidate) as unknown;
      return isRecord(parsed) ? parsed : undefined;
    } catch {
      // try next candidate
    }
  }
  return undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
