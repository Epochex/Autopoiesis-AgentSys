import type { JsonObject } from "../core/types.js";
import type { DecisionSimulator } from "../simulators/decision.js";
import type { Skill } from "./types.js";

export function createDecisionSimulateSkill(simulator: DecisionSimulator): Skill<JsonObject, JsonObject> {
  return {
    name: "decision.simulate",
    version: "0.1.0",
    description: "Evaluate a candidate business/action decision against a simulator scenario.",
    input_schema: {
      type: "object",
      required: ["scenario_id", "action_id"],
      properties: {
        scenario_id: { type: "string" },
        action_id: { type: "string" },
      },
    },
    output_schema: {
      type: "object",
      required: ["accepted", "reward", "cost", "risk"],
      properties: {
        accepted: { type: "boolean" },
        reward: { type: "number" },
        cost: { type: "number" },
        risk: { type: "number" },
      },
    },
    permissions: [
      {
        permission: "simulator.decision.read",
        risk: "read_only",
        description: "Runs a deterministic local decision simulator.",
        approval_required: false,
      },
    ],
    async invoke(invocation) {
      const result = simulator.simulate(String(invocation.input.scenario_id ?? ""), String(invocation.input.action_id ?? ""));
      return {
        status: "ok",
        output: result as unknown as JsonObject,
        observations: [
          {
            observation_id: `${invocation.invocation_id}:obs:decision`,
            skill_name: "decision.simulate",
            summary: `Decision ${result.action_id} ${result.accepted ? "satisfies" : "violates"} scenario constraints.`,
            cited_resource_refs: [`scenario:${result.scenario_id}`, `action:${result.action_id}`],
            data: result as unknown as JsonObject,
          },
        ],
      };
    },
  };
}
