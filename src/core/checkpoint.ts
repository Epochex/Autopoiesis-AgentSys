import type { AgentRunState, CheckpointStore, EventSink, AgentEvent } from "./types.js";

export class InMemoryCheckpointStore implements CheckpointStore {
  private readonly states = new Map<string, AgentRunState>();

  async save(state: AgentRunState): Promise<void> {
    this.states.set(state.run_id, structuredClone(state));
  }

  async load(runId: string): Promise<AgentRunState | undefined> {
    const state = this.states.get(runId);
    return state ? structuredClone(state) : undefined;
  }
}

export class InMemoryEventSink implements EventSink {
  readonly events: AgentEvent[] = [];

  async append(event: AgentEvent): Promise<void> {
    this.events.push(structuredClone(event));
  }
}
