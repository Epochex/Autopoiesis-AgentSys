import { readFile } from "node:fs/promises";
import { evaluateMatrixRegression, type MatrixRegressionPolicy } from "../harness/regression.js";
import type { MatrixHarnessReport } from "../harness/matrix.js";

async function main(): Promise<void> {
  const reportPath = process.argv[2];
  const policyPath = process.argv[3];
  if (!reportPath || !policyPath) {
    console.error("Usage: npm run harness:gate -- <report.json> <policy.json>");
    process.exitCode = 2;
    return;
  }

  const report = JSON.parse(await readFile(reportPath, "utf8")) as MatrixHarnessReport;
  const policy = JSON.parse(await readFile(policyPath, "utf8")) as MatrixRegressionPolicy;
  const gate = evaluateMatrixRegression(report, policy);
  console.log(JSON.stringify(gate, null, 2));
  if (!gate.accepted) process.exitCode = 1;
}

main().catch((error: unknown) => {
  console.error(error);
  process.exitCode = 1;
});
