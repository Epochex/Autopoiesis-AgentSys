import { appendFile, mkdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import type { AgentEvent, EventSink } from "../core/types.js";

export interface JsonlEventSinkOptions {
  rootDir: string;
  fileName?: string;
}

export class JsonlEventSink implements EventSink {
  private readonly filePath: string;

  constructor(options: JsonlEventSinkOptions) {
    this.filePath = join(options.rootDir, options.fileName ?? "events.jsonl");
  }

  async append(event: AgentEvent): Promise<void> {
    await mkdir(dirname(this.filePath), { recursive: true });
    await appendFile(this.filePath, `${JSON.stringify(event)}\n`, "utf8");
  }

  path(): string {
    return this.filePath;
  }
}
