import type { JsonObject } from "../core/types.js";

export interface ArtifactRecord {
  artifact_id: string;
  run_id: string;
  step_id?: string;
  uri: string;
  media_type: string;
  sha256: string;
  size_bytes: number;
  metadata: JsonObject;
  created_at: string;
}

export interface ArtifactWriteRequest {
  run_id: string;
  step_id?: string;
  name: string;
  media_type: string;
  content: string | Uint8Array;
  metadata?: JsonObject;
}

export interface ArtifactStore {
  write(request: ArtifactWriteRequest): Promise<ArtifactRecord>;
  read(artifactId: string): Promise<Uint8Array | undefined>;
  get(artifactId: string): Promise<ArtifactRecord | undefined>;
}
