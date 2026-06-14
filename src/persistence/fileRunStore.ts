import { mkdir, readFile, readdir } from "node:fs/promises";
import { join } from "node:path";
import type { AgentEvent, AgentRunState, DurableRunStore } from "../core/types.js";
import { FileCheckpointStore } from "./fileStore.js";
import { JsonlEventSink } from "./jsonlEventSink.js";

export interface FileRunStoreOptions {
  rootDir: string;
}

export class FileRunStore implements DurableRunStore {
  private readonly checkpoints: FileCheckpointStore;
  private readonly eventSink: JsonlEventSink;

  constructor(private readonly options: FileRunStoreOptions) {
    this.checkpoints = new FileCheckpointStore({ rootDir: options.rootDir });
    this.eventSink = new JsonlEventSink({ rootDir: join(options.rootDir, "events") });
  }

  async append(event: AgentEvent): Promise<void> {
    await this.eventSink.append(event);
  }

  async save(state: AgentRunState): Promise<void> {
    await this.checkpoints.save(state);
  }

  async appendAndCheckpoint(event: AgentEvent, state: AgentRunState): Promise<void> {
    await mkdir(this.options.rootDir, { recursive: true });
    await this.eventSink.append(event);
    await this.checkpoints.save(state);
  }

  async load(runId: string): Promise<AgentRunState | undefined> {
    return this.checkpoints.load(runId);
  }

  async listEvents(runId: string): Promise<AgentEvent[]> {
    try {
      const raw = await readFile(this.eventSink.path(), "utf8");
      return raw
        .trim()
        .split("\n")
        .filter(Boolean)
        .map((line) => JSON.parse(line) as AgentEvent)
        .filter((event) => event.run_id === runId);
    } catch (caught) {
      if (caught instanceof Error && "code" in caught && caught.code === "ENOENT") return [];
      throw caught;
    }
  }

  async listRuns(): Promise<AgentRunState[]> {
    const dir = join(this.options.rootDir, "checkpoints");
    try {
      const entries = await readdir(dir);
      const runs: AgentRunState[] = [];
      for (const entry of entries) {
        if (!entry.endsWith(".json")) continue;
        const raw = await readFile(join(dir, entry), "utf8");
        runs.push(JSON.parse(raw) as AgentRunState);
      }
      return runs;
    } catch (caught) {
      if (caught instanceof Error && "code" in caught && caught.code === "ENOENT") return [];
      throw caught;
    }
  }

  eventLogPath(): string {
    return this.eventSink.path();
  }
}
