import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { FileArtifactStore } from "../src/index.js";

test("file artifact store writes content with sha256 metadata", async () => {
  const rootDir = await mkdtemp(join(tmpdir(), "selfevo-artifacts-"));
  const store = new FileArtifactStore({ rootDir });

  const record = await store.write({
    run_id: "run_artifact",
    step_id: "step_1",
    name: "summary.md",
    media_type: "text/markdown",
    content: "# Summary\n",
    metadata: { kind: "handoff" },
  });
  const content = await store.read(record.artifact_id);
  const loaded = await store.get(record.artifact_id);

  assert.equal(record.size_bytes, 10);
  assert.equal(Buffer.from(content ?? []).toString("utf8"), "# Summary\n");
  assert.equal(loaded?.metadata.kind, "handoff");
  assert.match(record.sha256, /^[a-f0-9]{64}$/);
});
