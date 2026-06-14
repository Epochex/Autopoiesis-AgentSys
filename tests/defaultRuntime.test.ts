import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { buildCodingInvestigationTask, buildOfficeWorkflowTask, createDefaultOrchiterKernel } from "../src/index.js";

test("default kernel composes built-in skills and runs office workflow", async () => {
  const orchestriterKernel = createDefaultOrchiterKernel();
  const task = buildOfficeWorkflowTask({
    task_id: "default_office",
    title: "Default office task",
    brief: "Prepare a default kernel handoff.",
    sections: ["Summary", "Next Actions"],
  });

  const state = await orchestriterKernel.kernel.run(task);

  assert.equal(state.status, "completed");
  assert.ok(orchestriterKernel.skills.get("document.compose"));
  assert.ok(orchestriterKernel.skills.get("memory.search"));
});

test("default kernel optionally registers workspace search", async () => {
  const repo = await mkdtemp(join(tmpdir(), "selfevo-default-kernel-"));
  await writeFile(join(repo, "README.md"), "Agentic kernel workspace search.\n", "utf8");
  const orchestriterKernel = createDefaultOrchiterKernel({ workspaceRoot: repo });
  const task = buildCodingInvestigationTask({
    task_id: "default_coding",
    title: "Search workspace",
    issue: "Find kernel workspace search",
    repository_path: repo,
  });

  const state = await orchestriterKernel.kernel.run(task);

  assert.equal(state.status, "completed");
  assert.ok(orchestriterKernel.skills.get("workspace.search"));
});
