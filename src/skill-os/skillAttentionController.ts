import type { SkillRisk } from "../skills/types.js";
import type { SkillAttentionDecision, SkillAttentionQuery, SkillOutcomeUpdate, SkillProfile } from "./types.js";

const RISK_ORDER: SkillRisk[] = ["read_only", "local_write", "network", "side_effect", "privileged"];

export class SkillAttentionController {
  private readonly profiles = new Map<string, SkillProfile>();

  constructor(profiles: SkillProfile[] = []) {
    for (const profile of profiles) this.register(profile);
  }

  register(profile: SkillProfile): void {
    this.profiles.set(profile.skill.name, cloneProfile(profile));
  }

  decide(query: SkillAttentionQuery): SkillAttentionDecision {
    const candidates = [...this.profiles.values()].filter((profile) => withinRisk(profile, query.maxRisk));
    const scored = candidates
      .map((profile) => scoreProfile(profile, query))
      .sort((a, b) => b.score - a.score || a.profile.skill.name.localeCompare(b.profile.skill.name));
    const selected = scored.slice(0, query.topK).map((item) => cloneProfile(item.profile));
    const selectedNames = new Set(selected.map((profile) => profile.skill.name));
    const hidden = candidates.filter((profile) => !selectedNames.has(profile.skill.name)).map(cloneProfile);
    const irrelevantAll = irrelevantExposure(candidates, query.tags);
    const irrelevantSelected = irrelevantExposure(selected, query.tags);
    return {
      selected,
      hidden,
      scores: scored.map((item) => ({
        skill_name: item.profile.skill.name,
        score: round(item.score),
        reasons: item.reasons,
      })),
      expected_irrelevant_exposure_reduction: round(irrelevantAll - irrelevantSelected),
    };
  }

  update(outcome: SkillOutcomeUpdate): SkillProfile | undefined {
    const profile = this.profiles.get(outcome.skill_name);
    if (!profile) return undefined;
    const stats = profile.stats;
    stats.attempts += 1;
    stats.successes += outcome.success ? 1 : 0;
    stats.wrong_invocations += outcome.wrong_invocation ? 1 : 0;
    stats.bypasses += outcome.bypassed ? 1 : 0;
    stats.unsafe_blocks += outcome.unsafe_blocked ? 1 : 0;
    stats.total_token_cost += outcome.token_cost ?? 0;
    stats.total_latency_ms += outcome.latency_ms ?? 0;
    if (outcome.success) stats.last_success_at = outcome.happened_at ?? new Date().toISOString();
    return cloneProfile(profile);
  }

  snapshot(): SkillProfile[] {
    return [...this.profiles.values()].sort((a, b) => a.skill.name.localeCompare(b.skill.name)).map(cloneProfile);
  }
}

function scoreProfile(profile: SkillProfile, query: SkillAttentionQuery): { profile: SkillProfile; score: number; reasons: string[] } {
  const taskTerms = tokenize(`${query.objective} ${query.tags.join(" ")}`);
  const skillTerms = tokenize(`${profile.skill.name} ${profile.skill.description} ${profile.tags.join(" ")}`);
  const termHits = [...taskTerms].filter((term) => skillTerms.has(term)).length;
  const tagHits = profile.tags.filter((tag) => query.tags.includes(tag)).length;
  const attempts = Math.max(1, profile.stats.attempts);
  const successRate = profile.stats.successes / attempts;
  const wrongRate = profile.stats.wrong_invocations / attempts;
  const bypassRate = profile.stats.bypasses / attempts;
  const avgTokenCost = profile.stats.total_token_cost / attempts;
  const avgLatency = profile.stats.total_latency_ms / attempts;
  const riskPenalty = maxRiskIndex(profile) * (query.risk >= 0.7 ? 0.45 : 0.15);
  const score =
    (profile.prior ?? 0) +
    tagHits * 3 +
    termHits * 0.8 +
    successRate * 2.5 -
    wrongRate * 3 -
    bypassRate * 1.5 -
    avgTokenCost / 2500 -
    avgLatency / 10000 -
    riskPenalty;
  return {
    profile,
    score,
    reasons: [
      `tag_hits:${tagHits}`,
      `term_hits:${termHits}`,
      `success_rate:${round(successRate)}`,
      `wrong_rate:${round(wrongRate)}`,
      `bypass_rate:${round(bypassRate)}`,
      `risk_penalty:${round(riskPenalty)}`,
    ],
  };
}

function irrelevantExposure(profiles: SkillProfile[], tags: string[]): number {
  if (profiles.length === 0) return 0;
  const irrelevant = profiles.filter((profile) => !profile.tags.some((tag) => tags.includes(tag))).length;
  return irrelevant / profiles.length;
}

function withinRisk(profile: SkillProfile, maxRisk?: SkillRisk): boolean {
  if (!maxRisk) return true;
  return maxRiskIndex(profile) <= RISK_ORDER.indexOf(maxRisk);
}

function maxRiskIndex(profile: SkillProfile): number {
  return Math.max(0, ...profile.skill.permissions.map((permission) => RISK_ORDER.indexOf(permission.risk)));
}

function tokenize(value: string): Set<string> {
  return new Set(value.toLowerCase().split(/[^a-z0-9_.:-]+/).filter((term) => term.length > 1));
}

function cloneProfile(profile: SkillProfile): SkillProfile {
  return {
    skill: {
      name: profile.skill.name,
      version: profile.skill.version,
      description: profile.skill.description,
      permissions: profile.skill.permissions.map((permission) => ({ ...permission })),
    },
    tags: [...profile.tags],
    stats: { ...profile.stats },
    ...(profile.prior !== undefined ? { prior: profile.prior } : {}),
  };
}

function round(value: number): number {
  return Math.round(value * 10000) / 10000;
}
