import assert from "node:assert/strict";
import test from "node:test";
import { OpenAICompatibleJsonClient, runJsonProviderSmoke } from "../src/index.js";

test(
  "live OpenAI-compatible provider smoke",
  { skip: (process.env.SELFEVO_RUN_PROVIDER_SMOKE ?? process.env.HELIX_RUN_PROVIDER_SMOKE) !== "1" },
  async () => {
  const report = await runJsonProviderSmoke(OpenAICompatibleJsonClient.fromEnv());

  assert.equal(report.ok, true, JSON.stringify(report, null, 2));
  },
);
