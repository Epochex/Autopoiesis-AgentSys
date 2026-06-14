import { createDefaultOrchiterKernel, type DefaultOrchiterKernelOptions } from "../kernel/defaultKernel.js";
import type { ArchitectureProfile } from "./matrix.js";

export function createStaticArchitectureProfile(options: DefaultOrchiterKernelOptions = {}): ArchitectureProfile {
  return {
    profile_id: "static_default",
    label: "Static default kernel",
    tags: ["static", "deterministic"],
    buildKernel: () => createDefaultOrchiterKernel(options),
    buildRuntime: () => createDefaultOrchiterKernel(options),
  };
}

export function createModelArchitectureProfile(options: DefaultOrchiterKernelOptions): ArchitectureProfile {
  if (!options.model) throw new Error("Model architecture profile requires options.model");
  return {
    profile_id: "model_planner_default",
    label: "Model planner kernel",
    tags: ["model_planner"],
    buildKernel: () => createDefaultOrchiterKernel(options),
    buildRuntime: () => createDefaultOrchiterKernel(options),
  };
}
