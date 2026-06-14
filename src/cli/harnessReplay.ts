import { readFile, writeFile } from "node:fs/promises";
import { MatrixHarnessRunner } from "../harness/matrix.js";
import { createModelArchitectureProfile, createStaticArchitectureProfile } from "../harness/profiles.js";
import { parseScenarioSuiteJson, SuiteValidationError } from "../harness/suiteValidation.js";
import { OpenAICompatibleJsonClient } from "../providers/openaiCompatible.js";
import type { DecisionScenario } from "../simulators/decision.js";

async function main(): Promise<void> {
  const suitePath = process.argv[2];
  if (!suitePath) {
    console.error("Usage: npm run harness:replay -- <suite.json> [--out report.json]");
    process.exitCode = 2;
    return;
  }
  const outputPath = outputPathFromArgs(process.argv.slice(3));
  const suite = parseScenarioSuiteJson(await readFile(suitePath, "utf8"));
  const decisionScenarios = suite.scenarios.flatMap((scenario) => {
    const maybeScenario = scenario.work_item.payload.scenario;
    return isDecisionScenario(maybeScenario) ? [maybeScenario] : [];
  });
  const workspaceRoot = process.env.SELFEVO_WORKSPACE_ROOT ?? process.env.HELIX_WORKSPACE_ROOT;
  const profiles = [
    createStaticArchitectureProfile({
      ...(workspaceRoot ? { workspaceRoot } : {}),
      decisionScenarios,
    }),
  ];
  if ((process.env.SELFEVO_ENABLE_MODEL_PROFILE ?? process.env.HELIX_ENABLE_MODEL_PROFILE) === "1") {
    profiles.push(
      createModelArchitectureProfile({
        model: OpenAICompatibleJsonClient.fromEnv(),
        ...(workspaceRoot ? { workspaceRoot } : {}),
        decisionScenarios,
      }),
    );
  }
  const report = await new MatrixHarnessRunner(profiles).runSuite(suite);
  const reportJson = `${JSON.stringify(report, null, 2)}\n`;
  if (outputPath) {
    await writeFile(outputPath, reportJson, "utf8");
  } else {
    console.log(reportJson.trimEnd());
  }
  if (report.aggregate.failed > 0) process.exitCode = 1;
}

function outputPathFromArgs(args: string[]): string | undefined {
  const index = args.indexOf("--out");
  if (index === -1) return undefined;
  return args[index + 1];
}

function isDecisionScenario(value: unknown): value is DecisionScenario {
  return value !== null && typeof value === "object" && "scenario_id" in value && "actions" in value;
}

main().catch((error: unknown) => {
  if (error instanceof SuiteValidationError) {
    console.error(error.message);
    for (const issue of error.issues) console.error(`- ${issue.path}: ${issue.message}`);
    process.exitCode = 2;
    return;
  }
  console.error(error);
  process.exitCode = 1;
});
