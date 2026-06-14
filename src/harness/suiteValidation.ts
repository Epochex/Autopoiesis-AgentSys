import type { ScenarioSuite } from "./matrix.js";

export interface SuiteValidationIssue {
  path: string;
  message: string;
}

export class SuiteValidationError extends Error {
  constructor(readonly issues: SuiteValidationIssue[]) {
    super(`Scenario suite validation failed with ${issues.length} issue(s).`);
    this.name = "SuiteValidationError";
  }
}

export function parseScenarioSuiteJson(source: string): ScenarioSuite {
  try {
    return validateScenarioSuite(JSON.parse(source));
  } catch (error) {
    if (error instanceof SuiteValidationError) throw error;
    throw new SuiteValidationError([{ path: "$", message: "Expected valid JSON." }]);
  }
}

export function validateScenarioSuite(value: unknown): ScenarioSuite {
  const issues: SuiteValidationIssue[] = [];
  if (!isRecord(value)) {
    throw new SuiteValidationError([{ path: "$", message: "Expected an object." }]);
  }
  requireString(value, "suite_id", "$", issues);
  optionalString(value, "description", "$", issues);
  requireArray(value, "scenarios", "$", issues);

  if (Array.isArray(value.scenarios)) {
    value.scenarios.forEach((scenario, index) => validateScenario(scenario, `$.scenarios[${index}]`, issues));
  }
  if (issues.length > 0) throw new SuiteValidationError(issues);
  return value as unknown as ScenarioSuite;
}

function validateScenario(value: unknown, path: string, issues: SuiteValidationIssue[]): void {
  if (!isRecord(value)) {
    issues.push({ path, message: "Expected a scenario object." });
    return;
  }
  requireString(value, "scenario_id", path, issues);
  optionalStringArray(value, "tags", path, issues);
  if (!isRecord(value.work_item)) {
    issues.push({ path: `${path}.work_item`, message: "Expected a work item object." });
    return;
  }
  validateWorkItem(value.work_item, `${path}.work_item`, issues);
}

function validateWorkItem(value: Record<string, unknown>, path: string, issues: SuiteValidationIssue[]): void {
  requireString(value, "work_item_id", path, issues);
  requireString(value, "title", path, issues);
  requireString(value, "objective", path, issues);
  requireString(value, "domain", path, issues);
  requireRecord(value, "payload", path, issues);
  requireArtifactArray(value, path, issues);
  requireStringArray(value, "available_tools", path, issues);
  requireStringArray(value, "context_refs", path, issues);
  optionalMemoryScope(value, path, issues);
  requireApprovalPolicy(value, path, issues);
  requireStringArray(value, "constraints", path, issues);
  requireStringArray(value, "success_criteria", path, issues);
  optionalEvalContract(value, path, issues);
}

function requireArtifactArray(value: Record<string, unknown>, path: string, issues: SuiteValidationIssue[]): void {
  if (!Array.isArray(value.input_artifacts)) {
    issues.push({ path: `${path}.input_artifacts`, message: "Expected an array." });
    return;
  }
  value.input_artifacts.forEach((artifact, index) => {
    const artifactPath = `${path}.input_artifacts[${index}]`;
    if (!isRecord(artifact)) {
      issues.push({ path: artifactPath, message: "Expected an artifact object." });
      return;
    }
    requireString(artifact, "artifact_id", artifactPath, issues);
    requireString(artifact, "kind", artifactPath, issues);
    optionalString(artifact, "uri", artifactPath, issues);
    optionalString(artifact, "media_type", artifactPath, issues);
    optionalString(artifact, "role", artifactPath, issues);
    optionalRecord(artifact, "metadata", artifactPath, issues);
  });
}

function optionalMemoryScope(value: Record<string, unknown>, path: string, issues: SuiteValidationIssue[]): void {
  const memoryScope = value.memory_scope;
  if (memoryScope === undefined) return;
  if (!isRecord(memoryScope)) {
    issues.push({ path: `${path}.memory_scope`, message: "Expected an object." });
    return;
  }
  requireStringArray(memoryScope, "scopes", `${path}.memory_scope`, issues);
  optionalString(memoryScope, "query", `${path}.memory_scope`, issues);
  optionalStringArray(memoryScope, "tags", `${path}.memory_scope`, issues);
}

function requireApprovalPolicy(value: Record<string, unknown>, path: string, issues: SuiteValidationIssue[]): void {
  const approvalPolicy = value.approval_policy;
  if (!isRecord(approvalPolicy)) {
    issues.push({ path: `${path}.approval_policy`, message: "Expected an object." });
    return;
  }
  if (approvalPolicy.mode !== "none" && approvalPolicy.mode !== "human_gate" && approvalPolicy.mode !== "preapproved") {
    issues.push({
      path: `${path}.approval_policy.mode`,
      message: "Expected one of: none, human_gate, preapproved.",
    });
  }
  optionalStringArray(approvalPolicy, "required_permissions", `${path}.approval_policy`, issues);
  optionalStringArray(approvalPolicy, "preapproved_permissions", `${path}.approval_policy`, issues);
  optionalString(approvalPolicy, "reason", `${path}.approval_policy`, issues);
}

function optionalEvalContract(value: Record<string, unknown>, path: string, issues: SuiteValidationIssue[]): void {
  const evalContract = value.eval_contract;
  if (evalContract === undefined) return;
  if (!isRecord(evalContract)) {
    issues.push({ path: `${path}.eval_contract`, message: "Expected an object." });
    return;
  }
  requireStringArray(evalContract, "metrics", `${path}.eval_contract`, issues);
  optionalRecord(evalContract, "success_thresholds", `${path}.eval_contract`, issues);
  optionalStringArray(evalContract, "regression_tags", `${path}.eval_contract`, issues);
}

function requireString(value: Record<string, unknown>, key: string, path: string, issues: SuiteValidationIssue[]): void {
  if (typeof value[key] !== "string" || value[key].length === 0) {
    issues.push({ path: `${path}.${key}`, message: "Expected a non-empty string." });
  }
}

function optionalString(value: Record<string, unknown>, key: string, path: string, issues: SuiteValidationIssue[]): void {
  if (value[key] !== undefined && typeof value[key] !== "string") {
    issues.push({ path: `${path}.${key}`, message: "Expected a string." });
  }
}

function requireStringArray(value: Record<string, unknown>, key: string, path: string, issues: SuiteValidationIssue[]): void {
  if (!Array.isArray(value[key]) || !value[key].every((item) => typeof item === "string" && item.length > 0)) {
    issues.push({ path: `${path}.${key}`, message: "Expected an array of non-empty strings." });
  }
}

function optionalStringArray(value: Record<string, unknown>, key: string, path: string, issues: SuiteValidationIssue[]): void {
  if (value[key] !== undefined && (!Array.isArray(value[key]) || !value[key].every((item) => typeof item === "string"))) {
    issues.push({ path: `${path}.${key}`, message: "Expected an array of strings." });
  }
}

function requireArray(value: Record<string, unknown>, key: string, path: string, issues: SuiteValidationIssue[]): void {
  if (!Array.isArray(value[key])) {
    issues.push({ path: `${path}.${key}`, message: "Expected an array." });
  }
}

function requireRecord(value: Record<string, unknown>, key: string, path: string, issues: SuiteValidationIssue[]): void {
  if (!isRecord(value[key])) {
    issues.push({ path: `${path}.${key}`, message: "Expected an object." });
  }
}

function optionalRecord(value: Record<string, unknown>, key: string, path: string, issues: SuiteValidationIssue[]): void {
  if (value[key] !== undefined && !isRecord(value[key])) {
    issues.push({ path: `${path}.${key}`, message: "Expected an object." });
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
