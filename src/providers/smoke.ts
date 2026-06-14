import type { ChatJsonResult, JsonModelClient, ProviderHealth, ProviderSmokeAssertion, ProviderSmokeResult } from "./types.js";

export interface ProviderSmokeClient extends JsonModelClient {
  healthCheck?(): Promise<ProviderHealth>;
}

export interface ProviderSmokeOptions {
  sentinel?: string;
  maxTokens?: number;
}

export async function runJsonProviderSmoke(client: ProviderSmokeClient, options: ProviderSmokeOptions = {}): Promise<ProviderSmokeResult> {
  const sentinel = options.sentinel ?? "selfevo-provider-smoke";
  const health = client.healthCheck ? await client.healthCheck() : undefined;
  const chat = await client.chatJson(
    [
      { role: "system", content: "Return JSON only." },
      { role: "user", content: `Return {"ok":true,"component":"${sentinel}"}.` },
    ],
    { temperature: 0, maxTokens: options.maxTokens ?? 128 },
  );
  const assertions = buildAssertions(chat, sentinel, health);
  return {
    ok: assertions.every((assertion) => assertion.ok),
    ...(health ? { health } : {}),
    chat,
    assertions,
  };
}

function buildAssertions(chat: ChatJsonResult, sentinel: string, health?: ProviderHealth): ProviderSmokeAssertion[] {
  return [
    ...(health ? [{ name: "health_ok", ok: health.ok, message: health.error ?? `status=${health.status}` }] : []),
    {
      name: "chat_status_2xx",
      ok: chat.metadata.status >= 200 && chat.metadata.status < 300,
      message: `status=${chat.metadata.status}`,
    },
    {
      name: "chat_no_metadata_error",
      ok: !chat.metadata.error,
      message: chat.metadata.error,
    },
    {
      name: "chat_parseable_json",
      ok: Object.keys(chat.parsed).length > 0,
      message: `keys=${Object.keys(chat.parsed).join(",")}`,
    },
    {
      name: "chat_sentinel",
      ok: chat.parsed.component === sentinel || chat.parsed.ok === true,
      message: `component=${String(chat.parsed.component ?? "")}`,
    },
  ];
}
