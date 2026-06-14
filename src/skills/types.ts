import type { JsonObject, JsonValue } from "../core/types.js";

export type SkillRisk = "read_only" | "local_write" | "network" | "side_effect" | "privileged";

export interface SkillPermission {
  permission: string;
  risk: SkillRisk;
  description: string;
  approval_required: boolean;
}

export interface SkillContext {
  run_id: string;
  task_id: string;
  allowed_resource_refs: string[];
  memory_refs: string[];
  metadata: JsonObject;
}

export interface SkillInvocation<Input extends JsonObject = JsonObject> {
  invocation_id: string;
  skill_name: string;
  input: Input;
  context: SkillContext;
}

export interface SkillObservation {
  observation_id: string;
  skill_name: string;
  summary: string;
  cited_resource_refs: string[];
  data: JsonObject;
}

export interface SkillResult<Output extends JsonObject = JsonObject> {
  status: "ok" | "error" | "approval_required";
  output: Output;
  observations: SkillObservation[];
  error?: string;
}

export interface SkillSchema {
  type: "object";
  required?: string[];
  properties: Record<string, JsonValue>;
}

export interface Skill<Input extends JsonObject = JsonObject, Output extends JsonObject = JsonObject> {
  name: string;
  version: string;
  description: string;
  input_schema: SkillSchema;
  output_schema: SkillSchema;
  permissions: SkillPermission[];
  invoke(invocation: SkillInvocation<Input>): Promise<SkillResult<Output>>;
}
