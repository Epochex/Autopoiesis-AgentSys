import { OpenAICompatibleJsonClient } from "../providers/openaiCompatible.js";
import { runJsonProviderSmoke } from "../providers/smoke.js";

async function main(): Promise<void> {
  const client = OpenAICompatibleJsonClient.fromEnv();
  const report = await runJsonProviderSmoke(client);
  console.log(JSON.stringify({ event: "provider_smoke", report }, null, 2));
  if (!report.ok) process.exitCode = 2;
}

main().catch((error: unknown) => {
  console.error(error);
  process.exitCode = 1;
});
