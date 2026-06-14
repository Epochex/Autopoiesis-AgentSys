import type { JsonObject } from "../core/types.js";
import type { SandboxRunner } from "../sandbox/types.js";
import type { Skill } from "./types.js";

export interface CliSkillInput extends JsonObject {
  command: string;
  args?: string[];
  cwd?: string;
  timeoutMs?: number;
}

export function createCliSkill(runner: SandboxRunner): Skill<CliSkillInput, JsonObject> {
  return {
    name: "cli.run",
    version: "0.1.0",
    description: "Run an allowlisted local CLI command through the selfevo-orchiter sandbox runner.",
    input_schema: {
      type: "object",
      required: ["command"],
      properties: {
        command: { type: "string" },
        args: { type: "array" },
        cwd: { type: "string" },
        timeoutMs: { type: "number" },
      },
    },
    output_schema: {
      type: "object",
      required: ["status", "exitCode", "stdout", "stderr", "durationMs"],
      properties: {
        status: { type: "string" },
        exitCode: { type: "number" },
        stdout: { type: "string" },
        stderr: { type: "string" },
        durationMs: { type: "number" },
      },
    },
    permissions: [
      {
        permission: "local.process.spawn",
        risk: "side_effect",
        description: "Spawns an allowlisted local subprocess under sandbox policy.",
        approval_required: true,
      },
    ],
    async invoke(invocation) {
      const result = await runner.run({
        command: String(invocation.input.command),
        args: Array.isArray(invocation.input.args) ? invocation.input.args.map(String) : [],
        ...(typeof invocation.input.cwd === "string" ? { cwd: invocation.input.cwd } : {}),
        ...(typeof invocation.input.timeoutMs === "number" ? { timeoutMs: invocation.input.timeoutMs } : {}),
      });
      return {
        status: result.status === "ok" ? "ok" : "error",
        output: result as unknown as JsonObject,
        observations: [
          {
            observation_id: `${invocation.invocation_id}:obs:cli`,
            skill_name: "cli.run",
            summary: `Command ${result.command} finished with ${result.status}.`,
            cited_resource_refs: [],
            data: result as unknown as JsonObject,
          },
        ],
        ...(result.status === "ok" ? {} : { error: result.error ?? result.stderr }),
      };
    },
  };
}
