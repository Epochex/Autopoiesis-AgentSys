import type { JsonObject } from "../core/types.js";

export interface ChatMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

export interface ChatJsonResult {
  parsed: JsonObject;
  metadata: {
    provider: string;
    model: string;
    status: number;
    latency_ms: number;
    usage?: JsonObject;
    error?: string;
    raw_text_chars?: number;
  };
}

export interface JsonModelClient {
  chatJson(messages: ChatMessage[], options?: { maxTokens?: number; temperature?: number }): Promise<ChatJsonResult>;
}

export interface ProviderHealth {
  ok: boolean;
  status: number;
  latency_ms: number;
  endpoint: string;
  error?: string;
}

export interface ProviderSmokeAssertion {
  name: string;
  ok: boolean;
  message?: string | undefined;
}

export interface ProviderSmokeResult {
  ok: boolean;
  health?: ProviderHealth;
  chat: ChatJsonResult;
  assertions: ProviderSmokeAssertion[];
}
