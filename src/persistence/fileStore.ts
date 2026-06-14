import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import type { AgentRunState, CheckpointStore } from "../core/types.js";

export interface FileCheckpointStoreOptions {
  rootDir: string;
}

export class FileCheckpointStore implements CheckpointStore {
  constructor(private readonly options: FileCheckpointStoreOptions) {}

  async save(state: AgentRunState): Promise<void> {
    const path = this.pathFor(state.run_id);
    await mkdir(dirname(path), { recursive: true });
    const tempPath = `${path}.${process.pid}.${Date.now()}.tmp`;
    await writeFile(tempPath, `${JSON.stringify(state, null, 2)}\n`, "utf8");
    await rename(tempPath, path);
  }

  async load(runId: string): Promise<AgentRunState | undefined> {
    try {
      const raw = await readFile(this.pathFor(runId), "utf8");
      return JSON.parse(raw) as AgentRunState;
    } catch (caught) {
      if (caught instanceof Error && "code" in caught && caught.code === "ENOENT") return undefined;
      throw caught;
    }
  }

  pathFor(runId: string): string {
    return join(this.options.rootDir, "checkpoints", `${safeName(runId)}.json`);
  }
}

function safeName(value: string): string {
  return value.replace(/[^a-zA-Z0-9._-]/g, "_");
}
