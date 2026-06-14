export interface SandboxCommand {
  command: string;
  args: string[];
  cwd?: string;
  env?: Record<string, string>;
  timeoutMs?: number;
  maxOutputBytes?: number;
}

export interface SandboxPolicy {
  allowedCommands: string[];
  allowNetwork: boolean;
  defaultTimeoutMs: number;
  maxTimeoutMs: number;
  maxOutputBytes: number;
  cwdAllowlist?: string[];
}

export interface SandboxResult {
  status: "ok" | "timeout" | "policy_denied" | "error";
  exitCode: number | null;
  stdout: string;
  stderr: string;
  durationMs: number;
  command: string;
  error?: string;
}

export interface SandboxRunner {
  run(command: SandboxCommand): Promise<SandboxResult>;
}
