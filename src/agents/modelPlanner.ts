import { normalizePlanCandidate } from "../core/planValidation.js";
import type { AgentPlan, AgentRole, AgentTask, PlannerModule } from "../core/types.js";
import type { JsonModelClient } from "../providers/types.js";
import type { SkillRegistry } from "../skills/registry.js";
import { StaticPlanner } from "./basic.js";

export interface ModelPlannerOptions {
  model: JsonModelClient;
  fallback?: PlannerModule;
  skills?: SkillRegistry;
  availableRoles?: AgentRole[];
  maxSteps?: number;
  promptVersion?: string;
}

export class ModelPlanner implements PlannerModule {
  private readonly fallback: PlannerModule;

  constructor(private readonly options: ModelPlannerOptions) {
    this.fallback = options.fallback ?? new StaticPlanner();
  }

  async createPlan(task: AgentTask): Promise<AgentPlan> {
    const promptVersion = this.options.promptVersion ?? "planner.v1";
    const allowedSkills = this.options.skills?.list().map((skill) => skill.name);
    const availableRoles = this.options.availableRoles ?? ["executor"];
    try {
      const response = await this.options.model.chatJson(
        [
          {
            role: "system",
            content: [
              "You are selfevo-orchiter's planning module for long-running agentic engineering tasks.",
              "Return strict JSON only.",
              "Create a bounded task DAG with explicit agent roles, dependencies, skill refs, and success-oriented inputs.",
              "Use only available roles and skills. Do not invent tools.",
            ].join(" "),
          },
          {
            role: "user",
            content: JSON.stringify({
              prompt_version: promptVersion,
              task,
              available_roles: availableRoles,
              available_skills: allowedSkills ?? [],
              output_schema: {
                strategy: "string",
                steps: [
                  {
                    step_id: "string",
                    agent_role: "string",
                    title: "string",
                    depends_on: ["step_id"],
                    skill_refs: ["skill_name"],
                    input: {},
                  },
                ],
              },
            }),
          },
        ],
        { temperature: 0, maxTokens: 1800 },
      );
      const validationOptions = {
        maxSteps: this.options.maxSteps ?? 8,
        allowedRoles: availableRoles,
        ...(allowedSkills ? { allowedSkills } : {}),
      };
      const candidate = normalizePlanCandidate(task, response.parsed, validationOptions);
      if (!candidate.plan) return this.fallbackPlan(task, ["model output did not normalize to a valid plan", ...candidate.warnings]);
      return {
        ...candidate.plan,
        plan_id: `plan_${task.task_id}_model`,
        metadata: {
          source: "model",
          planner_name: "ModelPlanner",
          prompt_version: promptVersion,
          provider: response.metadata.provider,
          model: response.metadata.model,
          latency_ms: response.metadata.latency_ms,
          warnings: candidate.warnings,
        },
      };
    } catch (caught) {
      return this.fallbackPlan(task, [caught instanceof Error ? caught.message : String(caught)]);
    }
  }

  private async fallbackPlan(task: AgentTask, warnings: string[]): Promise<AgentPlan> {
    const plan = await this.fallback.createPlan(task);
    return {
      ...plan,
      metadata: {
        ...(plan.metadata ?? { planner_name: this.fallback.constructor.name }),
        source: "fallback",
        planner_name: plan.metadata?.planner_name ?? this.fallback.constructor.name,
        warnings,
      },
    };
  }
}
