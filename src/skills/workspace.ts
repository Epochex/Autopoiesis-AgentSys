import { readdir, readFile, stat } from "node:fs/promises";
import { join, relative, resolve } from "node:path";
import type { JsonObject } from "../core/types.js";
import type { Skill } from "./types.js";

export interface WorkspaceSearchOptions {
  rootDir: string;
  maxFiles?: number;
  maxMatches?: number;
  maxFileBytes?: number;
}

export function createWorkspaceSearchSkill(options: WorkspaceSearchOptions): Skill<JsonObject, JsonObject> {
  const rootDir = resolve(options.rootDir);
  const maxFiles = options.maxFiles ?? 500;
  const maxMatches = options.maxMatches ?? 20;
  const maxFileBytes = options.maxFileBytes ?? 128_000;
  return {
    name: "workspace.search",
    version: "0.1.0",
    description: "Read-only workspace search for coding-agent repository inspection.",
    input_schema: {
      type: "object",
      required: ["query"],
      properties: {
        query: { type: "string" },
        repository_path: { type: "string" },
        target_paths: { type: "array" },
      },
    },
    output_schema: {
      type: "object",
      required: ["matches", "searched_files"],
      properties: {
        matches: { type: "array" },
        searched_files: { type: "number" },
      },
    },
    permissions: [
      {
        permission: "workspace.read",
        risk: "read_only",
        description: "Reads files from an allowlisted workspace root.",
        approval_required: false,
      },
    ],
    async invoke(invocation) {
      const requestedRoot = typeof invocation.input.repository_path === "string" ? resolve(invocation.input.repository_path) : rootDir;
      if (!isWithin(requestedRoot, rootDir)) {
        return {
          status: "error",
          output: { matches: [], searched_files: 0 },
          observations: [],
          error: `Repository path is outside workspace root: ${requestedRoot}`,
        };
      }
      const query = String(invocation.input.query ?? "");
      const terms = tokenize(query);
      const targetPaths = parseTargetPaths(invocation.input.target_paths).map((target) => resolve(requestedRoot, target)).filter((target) => isWithin(target, requestedRoot));
      const files = await collectFiles(targetPaths.length > 0 ? targetPaths : [requestedRoot], {
        rootDir: requestedRoot,
        maxFiles,
        maxFileBytes,
      });
      const matches = [];
      for (const file of files) {
        const content = await readFile(file.absolute_path, "utf8");
        const score = scoreContent(content, terms);
        if (score <= 0) continue;
        matches.push({
          path: file.relative_path,
          score,
          snippets: snippets(content, terms, 3),
        });
      }
      matches.sort((left, right) => right.score - left.score);
      const selected = matches.slice(0, maxMatches);
      return {
        status: "ok",
        output: {
          matches: selected,
          searched_files: files.length,
          truncated: matches.length > selected.length,
        },
        observations: [
          {
            observation_id: `${invocation.invocation_id}:obs:workspace`,
            skill_name: "workspace.search",
            summary: `Searched ${files.length} files and found ${selected.length} relevant matches.`,
            cited_resource_refs: selected.map((match) => `file:${match.path}`),
            data: {
              searched_files: files.length,
              matches: selected.length,
            },
          },
        ],
      };
    },
  };
}

interface CollectedFile {
  absolute_path: string;
  relative_path: string;
}

async function collectFiles(paths: string[], options: { rootDir: string; maxFiles: number; maxFileBytes: number }): Promise<CollectedFile[]> {
  const files: CollectedFile[] = [];
  const queue = [...paths];
  while (queue.length > 0 && files.length < options.maxFiles) {
    const current = queue.shift();
    if (!current) continue;
    const info = await stat(current).catch(() => undefined);
    if (!info) continue;
    if (info.isDirectory()) {
      if (skipDir(current)) continue;
      const entries = await readdir(current);
      for (const entry of entries) queue.push(join(current, entry));
      continue;
    }
    if (!info.isFile() || info.size > options.maxFileBytes || skipFile(current)) continue;
    files.push({
      absolute_path: current,
      relative_path: relative(options.rootDir, current),
    });
  }
  return files;
}

function scoreContent(content: string, terms: string[]): number {
  if (terms.length === 0) return 0;
  const lower = content.toLowerCase();
  const hits = terms.filter((term) => lower.includes(term)).length;
  return hits / terms.length;
}

function snippets(content: string, terms: string[], limit: number): string[] {
  const lines = content.split(/\r?\n/);
  const snippetsOut: string[] = [];
  for (const [index, line] of lines.entries()) {
    const lower = line.toLowerCase();
    if (!terms.some((term) => lower.includes(term))) continue;
    snippetsOut.push(`${index + 1}: ${line.trim()}`.slice(0, 240));
    if (snippetsOut.length >= limit) break;
  }
  return snippetsOut;
}

function tokenize(value: string): string[] {
  return value.toLowerCase().split(/[^a-z0-9_/-]+/).filter((term) => term.length >= 3);
}

function parseTargetPaths(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String).filter(Boolean) : [];
}

function isWithin(path: string, allowedRoot: string): boolean {
  const resolvedPath = resolve(path);
  const resolvedRoot = resolve(allowedRoot);
  return resolvedPath === resolvedRoot || resolvedPath.startsWith(`${resolvedRoot}/`);
}

function skipDir(path: string): boolean {
  return /(^|\/)(\.git|node_modules|dist|coverage|\.venv|__pycache__)$/.test(path);
}

function skipFile(path: string): boolean {
  return /\.(png|jpe?g|gif|webp|pdf|zip|tar|gz|lock)$/i.test(path);
}
