import { spawn } from "node:child_process";
import { resolve } from "node:path";
import type { SandboxCommand, SandboxPolicy, SandboxResult, SandboxRunner } from "./types.js";

export class SubprocessSandboxRunner implements SandboxRunner {
  constructor(private readonly policy: SandboxPolicy) {}

  async run(command: SandboxCommand): Promise<SandboxResult> {
    const started = performance.now();
    const policyError = this.policyError(command);
    const printable = [command.command, ...command.args].join(" ");
    if (policyError) {
      return {
        status: "policy_denied",
        exitCode: null,
        stdout: "",
        stderr: "",
        durationMs: elapsed(started),
        command: printable,
        error: policyError,
      };
    }
    const timeoutMs = Math.min(command.timeoutMs ?? this.policy.defaultTimeoutMs, this.policy.maxTimeoutMs);
    const maxOutputBytes = Math.min(command.maxOutputBytes ?? this.policy.maxOutputBytes, this.policy.maxOutputBytes);
    return new Promise((resolveResult) => {
      let stdout = "";
      let stderr = "";
      let settled = false;
      const child = spawn(command.command, command.args, {
        cwd: command.cwd,
        env: { ...process.env, ...(command.env ?? {}) },
        shell: false,
      });
      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        child.kill("SIGKILL");
        resolveResult({
          status: "timeout",
          exitCode: null,
          stdout,
          stderr,
          durationMs: elapsed(started),
          command: printable,
          error: `Command timed out after ${timeoutMs}ms`,
        });
      }, timeoutMs);
      child.stdout.on("data", (chunk: Buffer) => {
        stdout = appendBounded(stdout, chunk, maxOutputBytes);
      });
      child.stderr.on("data", (chunk: Buffer) => {
        stderr = appendBounded(stderr, chunk, maxOutputBytes);
      });
      child.on("error", (error) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolveResult({
          status: "error",
          exitCode: null,
          stdout,
          stderr,
          durationMs: elapsed(started),
          command: printable,
          error: error.message,
        });
      });
      child.on("close", (code) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolveResult({
          status: code === 0 ? "ok" : "error",
          exitCode: code,
          stdout,
          stderr,
          durationMs: elapsed(started),
          command: printable,
          ...(code === 0 ? {} : { error: `Command exited with code ${code ?? "null"}` }),
        });
      });
    });
  }

  private policyError(command: SandboxCommand): string | undefined {
    if (!this.policy.allowedCommands.includes(command.command)) return `Command is not allowed: ${command.command}`;
    if (command.timeoutMs && command.timeoutMs > this.policy.maxTimeoutMs) return `Timeout exceeds policy max: ${command.timeoutMs}ms`;
    if (command.cwd && this.policy.cwdAllowlist && !this.policy.cwdAllowlist.some((allowed) => isWithin(command.cwd ?? "", allowed))) {
      return `Working directory is outside sandbox allowlist: ${command.cwd}`;
    }
    return undefined;
  }
}

export function defaultLocalSandboxPolicy(overrides: Partial<SandboxPolicy> = {}): SandboxPolicy {
  return {
    allowedCommands: ["node", "npm", "git"],
    allowNetwork: false,
    defaultTimeoutMs: 30_000,
    maxTimeoutMs: 120_000,
    maxOutputBytes: 128_000,
    ...overrides,
  };
}

function appendBounded(current: string, chunk: Buffer, maxBytes: number): string {
  const combined = current + chunk.toString("utf8");
  if (Buffer.byteLength(combined, "utf8") <= maxBytes) return combined;
  return combined.slice(0, maxBytes) + "\n[selfevo:output_truncated]";
}

function elapsed(started: number): number {
  return Math.round((performance.now() - started) * 100) / 100;
}

function isWithin(path: string, allowedRoot: string): boolean {
  const resolvedPath = resolve(path);
  const resolvedRoot = resolve(allowedRoot);
  return resolvedPath === resolvedRoot || resolvedPath.startsWith(`${resolvedRoot}/`);
}
