import type { AgentPlan, AgentRole, AgentStep, AgentTask, JsonObject } from "./types.js";

export interface PlanValidationOptions {
  allowedRoles?: AgentRole[];
  allowedSkills?: string[];
  maxSteps?: number;
}

export interface NormalizedPlanCandidate {
  plan?: AgentPlan;
  warnings: string[];
}

export function validatePlan(plan: AgentPlan, task: AgentTask, options: PlanValidationOptions = {}): void {
  if (plan.task_id !== task.task_id) throw new Error(`Plan task mismatch: ${plan.task_id} !== ${task.task_id}`);
  if (options.maxSteps && plan.steps.length > options.maxSteps) throw new Error(`Plan has ${plan.steps.length} steps; max is ${options.maxSteps}`);
  const allowedRoles = options.allowedRoles ? new Set(options.allowedRoles) : undefined;
  const allowedSkills = options.allowedSkills ? new Set(options.allowedSkills) : undefined;
  const ids = new Set<string>();
  for (const step of plan.steps) {
    if (ids.has(step.step_id)) throw new Error(`Duplicate step id: ${step.step_id}`);
    ids.add(step.step_id);
    if (allowedRoles && !allowedRoles.has(step.agent_role)) throw new Error(`Step ${step.step_id} uses unavailable role ${step.agent_role}`);
    if (allowedSkills) {
      for (const skill of step.skill_refs) {
        if (!allowedSkills.has(skill)) throw new Error(`Step ${step.step_id} uses unavailable skill ${skill}`);
      }
    }
  }
  for (const step of plan.steps) {
    for (const dependency of step.depends_on) {
      if (!ids.has(dependency)) throw new Error(`Step ${step.step_id} depends on unknown step ${dependency}`);
    }
  }
  orderedSteps(plan);
}

export function orderedSteps(plan: AgentPlan): AgentStep[] {
  const remaining = new Map(plan.steps.map((step) => [step.step_id, step]));
  const ordered: AgentStep[] = [];
  while (remaining.size > 0) {
    const ready = [...remaining.values()].find((step) => step.depends_on.every((dependency) => ordered.some((done) => done.step_id === dependency)));
    if (!ready) throw new Error("Plan contains a dependency cycle");
    ordered.push(ready);
    remaining.delete(ready.step_id);
  }
  return ordered;
}

export function normalizePlanCandidate(task: AgentTask, value: JsonObject, options: PlanValidationOptions = {}): NormalizedPlanCandidate {
  const warnings: string[] = [];
  const strategy = typeof value.strategy === "string" && value.strategy.trim() ? value.strategy.trim() : undefined;
  const rawSteps = Array.isArray(value.steps) ? value.steps : undefined;
  if (!strategy) warnings.push("missing strategy");
  if (!rawSteps || rawSteps.length === 0) warnings.push("missing steps");
  if (!strategy || !rawSteps || rawSteps.length === 0) return { warnings };
  const maxSteps = options.maxSteps ?? rawSteps.length;
  const allowedRoles = options.allowedRoles ? new Set(options.allowedRoles) : undefined;
  const allowedSkills = options.allowedSkills ? new Set(options.allowedSkills) : undefined;
  const steps: AgentStep[] = [];
  for (const [index, raw] of rawSteps.slice(0, maxSteps).entries()) {
    if (!isRecord(raw)) {
      warnings.push(`step ${index + 1} is not an object`);
      continue;
    }
    const stepId = typeof raw.step_id === "string" && raw.step_id.trim() ? raw.step_id.trim() : `step_${index + 1}`;
    const title = typeof raw.title === "string" && raw.title.trim() ? raw.title.trim() : `Step ${index + 1}`;
    const agentRole = typeof raw.agent_role === "string" && raw.agent_role.trim() ? raw.agent_role.trim() : "executor";
    if (allowedRoles && !allowedRoles.has(agentRole)) {
      warnings.push(`step ${stepId} rejected unavailable role ${agentRole}`);
      continue;
    }
    const skillRefs = stringArray(raw.skill_refs).filter((skill) => {
      if (!allowedSkills || allowedSkills.has(skill)) return true;
      warnings.push(`step ${stepId} dropped unavailable skill ${skill}`);
      return false;
    });
    steps.push({
      step_id: stepId,
      agent_role: agentRole,
      title,
      status: "pending",
      depends_on: stringArray(raw.depends_on).filter((id) => id !== stepId),
      skill_refs: skillRefs,
      input: isRecord(raw.input) ? raw.input : {},
    });
  }
  if (steps.length === 0) return { warnings: [...warnings, "no valid steps after normalization"] };
  const plan: AgentPlan = {
    plan_id: `plan_${task.task_id}_candidate`,
    task_id: task.task_id,
    strategy,
    steps,
  };
  try {
    validatePlan(plan, task, { ...options, maxSteps });
  } catch (caught) {
    return {
      warnings: [...warnings, caught instanceof Error ? caught.message : String(caught)],
    };
  }
  return { plan, warnings };
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String).map((item) => item.trim()).filter(Boolean) : [];
}

function isRecord(value: unknown): value is JsonObject {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
