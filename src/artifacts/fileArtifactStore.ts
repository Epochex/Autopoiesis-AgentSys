import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { basename, join } from "node:path";
import type { ArtifactRecord, ArtifactStore, ArtifactWriteRequest } from "./types.js";

export interface FileArtifactStoreOptions {
  rootDir: string;
}

export class FileArtifactStore implements ArtifactStore {
  private readonly records = new Map<string, ArtifactRecord>();

  constructor(private readonly options: FileArtifactStoreOptions) {}

  async write(request: ArtifactWriteRequest): Promise<ArtifactRecord> {
    const bytes = typeof request.content === "string" ? Buffer.from(request.content, "utf8") : Buffer.from(request.content);
    const sha256 = createHash("sha256").update(bytes).digest("hex");
    const artifactId = `art_${randomUUID().slice(0, 12)}`;
    const safeFile = `${artifactId}_${basename(request.name).replace(/[^a-zA-Z0-9._-]/g, "_")}`;
    const path = join(this.options.rootDir, "artifacts", request.run_id, safeFile);
    await mkdir(join(this.options.rootDir, "artifacts", request.run_id), { recursive: true });
    await writeFile(path, bytes);
    const record: ArtifactRecord = {
      artifact_id: artifactId,
      run_id: request.run_id,
      ...(request.step_id ? { step_id: request.step_id } : {}),
      uri: path,
      media_type: request.media_type,
      sha256,
      size_bytes: bytes.byteLength,
      metadata: request.metadata ?? {},
      created_at: new Date().toISOString(),
    };
    this.records.set(artifactId, record);
    return structuredClone(record);
  }

  async read(artifactId: string): Promise<Uint8Array | undefined> {
    const record = this.records.get(artifactId);
    if (!record) return undefined;
    return readFile(record.uri);
  }

  async get(artifactId: string): Promise<ArtifactRecord | undefined> {
    const record = this.records.get(artifactId);
    return record ? structuredClone(record) : undefined;
  }
}
