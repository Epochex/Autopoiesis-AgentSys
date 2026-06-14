import { ContractReviewer, SkillExecutorAgent, StaticPlanner } from "../agents/basic.js";
import { ModelPlanner } from "../agents/modelPlanner.js";
import { AgentKernel } from "../core/kernel.js";
import type { DurableRunStore, PlannerModule } from "../core/types.js";
import { InMemoryMemoryStore } from "../context/memoryStore.js";
import type { MemoryStore } from "../context/types.js";
import type { JsonModelClient } from "../providers/types.js";
import { defaultLocalSandboxPolicy, SubprocessSandboxRunner } from "../sandbox/subprocess.js";
import type { SandboxPolicy } from "../sandbox/types.js";
import type { DecisionScenario } from "../simulators/decision.js";
import { DecisionSimulator } from "../simulators/decision.js";
import { createCliSkill } from "../skills/cli.js";
import { createDecisionSimulateSkill } from "../skills/decisionSimulator.js";
import { createDocumentComposeSkill } from "../skills/document.js";
import { createEchoSkill } from "../skills/builtin.js";
import { createMemorySearchSkill } from "../skills/memory.js";
import { SkillRegistry } from "../skills/registry.js";
import { createWorkspaceSearchSkill } from "../skills/workspace.js";

export interface DefaultOrchiterKernelOptions {
  model?: JsonModelClient;
  runStore?: DurableRunStore;
  memory?: MemoryStore;
  workspaceRoot?: string;
  sandboxPolicy?: Partial<SandboxPolicy>;
  decisionScenarios?: DecisionScenario[];
}

export interface DefaultOrchiterKernel {
  kernel: AgentKernel;
  skills: SkillRegistry;
  memory: MemoryStore;
  planner: PlannerModule;
}

export function createDefaultOrchiterKernel(options: DefaultOrchiterKernelOptions = {}): DefaultOrchiterKernel {
  const memory = options.memory ?? new InMemoryMemoryStore();
  const skills = new SkillRegistry();
  skills.register(createEchoSkill());
  skills.register(createDocumentComposeSkill());
  skills.register(createMemorySearchSkill(memory));
  skills.register(createCliSkill(new SubprocessSandboxRunner(defaultLocalSandboxPolicy(options.sandboxPolicy))));
  if (options.workspaceRoot) skills.register(createWorkspaceSearchSkill({ rootDir: options.workspaceRoot }));
  if (options.decisionScenarios) skills.register(createDecisionSimulateSkill(new DecisionSimulator(options.decisionScenarios)));

  const planner = options.model
    ? new ModelPlanner({
        model: options.model,
        fallback: new StaticPlanner(),
        skills,
        availableRoles: ["executor"],
      })
    : new StaticPlanner();
  const kernel = new AgentKernel({
    planner,
    agents: [new SkillExecutorAgent(skills)],
    reviewer: new ContractReviewer(),
    ...(options.runStore ? { runStore: options.runStore } : {}),
  });
  return {
    kernel,
    skills,
    memory,
    planner,
  };
}

export type DefaultHelixKernelOptions = DefaultOrchiterKernelOptions;
export type DefaultHelixRuntime = DefaultOrchiterKernel;

/**
 * Backward-compatible alias for older consumers. New code should use
 * createDefaultOrchiterKernel and the kernel package path.
 */
export const createDefaultHelixRuntime = createDefaultOrchiterKernel;
