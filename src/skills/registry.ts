import type { JsonObject } from "../core/types.js";
import { DefaultSkillPolicy, type SkillPolicy } from "./policy.js";
import type { Skill, SkillInvocation, SkillResult } from "./types.js";

export interface SkillRegistryOptions {
  policy?: SkillPolicy;
}

export class SkillRegistry {
  private readonly skills = new Map<string, Skill>();

  constructor(private readonly options: SkillRegistryOptions = {}) {}

  register(skill: Skill): void {
    const key = skillKey(skill.name, skill.version);
    if (this.skills.has(key)) throw new Error(`Skill already registered: ${key}`);
    this.skills.set(key, skill);
  }

  get(name: string, version?: string): Skill | undefined {
    if (version) return this.skills.get(skillKey(name, version));
    return [...this.skills.values()].find((skill) => skill.name === name);
  }

  list(): Skill[] {
    return [...this.skills.values()];
  }

  async invoke<Input extends JsonObject = JsonObject, Output extends JsonObject = JsonObject>(
    invocation: SkillInvocation<Input>,
    version?: string,
  ): Promise<SkillResult<Output>> {
    const skill = this.get(invocation.skill_name, version);
    if (!skill) throw new Error(`Unknown skill: ${invocation.skill_name}${version ? `@${version}` : ""}`);
    const decision = (this.options.policy ?? new DefaultSkillPolicy()).evaluate(skill, invocation);
    if (decision.status === "deny") {
      return {
        status: "error",
        output: {} as Output,
        observations: [],
        error: decision.reason,
      };
    }
    if (decision.status === "approval_required") {
      return {
        status: "approval_required",
        output: ({
          approval_required: true,
          permission: decision.permission?.permission,
          reason: decision.reason,
        } as unknown) as Output,
        observations: [],
        error: decision.reason,
      };
    }
    return skill.invoke(invocation) as Promise<SkillResult<Output>>;
  }
}

function skillKey(name: string, version: string): string {
  return `${name}@${version}`;
}
