import assert from "node:assert/strict";
import test from "node:test";
import {
  MatrixHarnessRunner,
  buildOfficeWorkItem,
  createStaticArchitectureProfile,
  scenarioFromWorkItem,
} from "../src/index.js";

test("matrix harness runs a reusable work item suite across architecture profiles", async () => {
  const workItem = buildOfficeWorkItem({
    task_id: "matrix_office_001",
    title: "Draft launch handoff",
    brief: "Prepare an internal launch handoff.",
    sections: ["Summary", "Next Actions"],
  });
  const suite = {
    suite_id: "portable_work_items",
    scenarios: [scenarioFromWorkItem(workItem, ["office", "digital_employee"])],
  };

  const report = await new MatrixHarnessRunner([createStaticArchitectureProfile()]).runSuite(suite);

  assert.equal(report.suite_id, "portable_work_items");
  assert.equal(report.aggregate.profiles, 1);
  assert.equal(report.aggregate.scenarios, 1);
  assert.equal(report.aggregate.runs, 1);
  assert.equal(report.aggregate.completed, 1);
  assert.equal(report.by_profile.static_default?.completed, 1);
  assert.equal(report.rows[0]?.profile_id, "static_default");
  assert.deepEqual(report.rows[0]?.tags.sort(), ["deterministic", "digital_employee", "office", "static"]);
});
