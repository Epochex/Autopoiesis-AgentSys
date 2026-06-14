import { appendFile, mkdir, readFile } from "node:fs/promises";
import { dirname } from "node:path";
import { InMemoryMemoryStore } from "./memoryStore.js";
import type { MemoryQuery, MemoryRecord, MemorySearchResult, MemoryStore } from "./types.js";

export interface FileMemoryStoreOptions {
  path: string;
}

export class FileMemoryStore implements MemoryStore {
  private readonly memory = new InMemoryMemoryStore();

  private constructor(private readonly options: FileMemoryStoreOptions) {}

  static async open(options: FileMemoryStoreOptions): Promise<FileMemoryStore> {
    const store = new FileMemoryStore(options);
    await store.loadExisting();
    return store;
  }

  async put(record: Omit<MemoryRecord, "created_at" | "updated_at">): Promise<MemoryRecord> {
    const saved = await this.memory.put(record);
    await mkdir(dirname(this.options.path), { recursive: true });
    await appendFile(this.options.path, `${JSON.stringify(saved)}\n`, "utf8");
    return saved;
  }

  async search(query: MemoryQuery): Promise<MemorySearchResult[]> {
    return this.memory.search(query);
  }

  async get(memoryId: string): Promise<MemoryRecord | undefined> {
    return this.memory.get(memoryId);
  }

  private async loadExisting(): Promise<void> {
    try {
      const raw = await readFile(this.options.path, "utf8");
      for (const line of raw.split("\n").filter(Boolean)) {
        const parsed = JSON.parse(line) as MemoryRecord;
        await this.memory.put({
          memory_id: parsed.memory_id,
          scope: parsed.scope,
          subject: parsed.subject,
          content: parsed.content,
          tags: parsed.tags,
          metadata: parsed.metadata,
        });
      }
    } catch (caught) {
      if (caught instanceof Error && "code" in caught && caught.code === "ENOENT") return;
      throw caught;
    }
  }
}
