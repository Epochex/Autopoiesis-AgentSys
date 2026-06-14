import assert from "node:assert/strict";
import test from "node:test";
import { SuiteValidationError, buildOfficeWorkItem, parseScenarioSuiteJson, scenarioFromWorkItem } from "../src/index.js";

test("suite validation accepts domain-neutral work item suites", () => {
  const workItem = buildOfficeWorkItem({
    task_id: "valid_suite_office",
    title: "Valid office item",
    brief: "Prepare a valid handoff.",
    sections: ["Summary"],
  });

  const suite = parseScenarioSuiteJson(
    JSON.stringify({
      suite_id: "valid_suite",
      scenarios: [scenarioFromWorkItem(workItem, ["office"])],
    }),
  );

  assert.equal(suite.suite_id, "valid_suite");
  assert.equal(suite.scenarios[0]?.work_item.available_tools[0], "document.compose");
});

test("suite validation reports precise input contract issues", () => {
  try {
    parseScenarioSuiteJson(
      JSON.stringify({
        suite_id: "",
        scenarios: [
          {
            scenario_id: "bad_scenario",
            tags: ["office"],
            work_item: {
              work_item_id: "bad_item",
              title: "Bad Item",
              objective: "This suite is intentionally invalid.",
              domain: "office",
              payload: {},
              input_artifacts: [{ artifact_id: "brief:bad_item" }],
              available_tools: ["document.compose", ""],
              context_refs: [],
              approval_policy: { mode: "unsafe" },
              constraints: [],
              success_criteria: [],
            },
          },
        ],
      }),
    );
    assert.fail("Expected suite validation to reject invalid input.");
  } catch (error) {
    assert.ok(error instanceof SuiteValidationError);
    assert.ok(error.issues.some((issue) => issue.path === "$.suite_id"));
    assert.ok(error.issues.some((issue) => issue.path === "$.scenarios[0].work_item.input_artifacts[0].kind"));
    assert.ok(error.issues.some((issue) => issue.path === "$.scenarios[0].work_item.available_tools"));
    assert.ok(error.issues.some((issue) => issue.path === "$.scenarios[0].work_item.approval_policy.mode"));
  }
});
