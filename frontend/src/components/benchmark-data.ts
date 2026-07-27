/* ── PAGE · 基准场景 / BENCHMARK — data contract + real fallback ────────────────
 * GET /api/rca/benchmark serves the benchmark scenario: REAL LongMemEval-500
 * results (this repo's tiered store vs Mem0 / Reflexion / flat-vector / BM25) +
 * the ITBench catalog & published baselines. The fixture below is the REAL
 * numbers read out of eval_mem_compare/results_{core,mem0}.json (verbatim), used
 * only if the fetch fails; nothing is invented. ITBench figures are PUBLISHED
 * reference (arXiv:2502.05352), labelled not-local, honestly. */

export type I18n = { zh: string; en: string }
export interface LmeSystem {
  id: string; label: string; self: boolean
  recall: Record<'1' | '3' | '5' | '10', number>
  ans_hit5: number
  by_ability5: Record<string, number>
}
export interface ItbGroup { id: string; label: I18n; maps_to: I18n; sota_pct: number }
export interface BenchmarkResp {
  ok: boolean
  longmemeval: {
    dataset: string; n: number; live: boolean; metric: string
    ks: number[]; abilities: string[]; systems: LmeSystem[]; note: I18n
  }
  itbench: {
    name: string; live: boolean; status: I18n
    paper_url: string; repo_url: string; leaderboard_url: string
    total_scenarios: number; groups: ItbGroup[]; note: I18n
  }
  coverage: { cap: I18n; benchmark: string | I18n; covered: boolean }[]
}

export const ABILITY_LABEL: Record<string, I18n> = {
  'knowledge-update': { zh: '知识更新', en: 'knowledge-update' },
  'multi-session': { zh: '多会话', en: 'multi-session' },
  'single-session-assistant': { zh: '单会话·助手', en: 'single-session-assistant' },
  'single-session-preference': { zh: '单会话·偏好', en: 'single-session-preference' },
  'single-session-user': { zh: '单会话·用户', en: 'single-session-user' },
  'temporal-reasoning': { zh: '时序推理', en: 'temporal-reasoning' },
}

/* real numbers, verbatim from eval_mem_compare/results_{core,mem0}.json */
export const BENCHMARK_FIXTURE: BenchmarkResp = {
  ok: true,
  longmemeval: {
    dataset: 'LongMemEval-500', n: 500, live: true, metric: 'recall@k · LLM-free',
    ks: [1, 3, 5, 10],
    abilities: ['knowledge-update', 'multi-session', 'single-session-assistant', 'single-session-preference', 'single-session-user', 'temporal-reasoning'],
    systems: [
      { id: 'tiered', label: 'tiered · 本仓库', self: true, recall: { '1': 0.722, '3': 0.838, '5': 0.906, '10': 0.954 }, ans_hit5: 0.432,
        by_ability5: { 'knowledge-update': 0.9487, 'multi-session': 0.9549, 'single-session-assistant': 0.9821, 'single-session-preference': 0.5667, 'single-session-user': 0.7857, 'temporal-reasoning': 0.9398 } },
      { id: 'mem0', label: 'Mem0 (mem0ai)', self: false, recall: { '1': 0.756, '3': 0.876, '5': 0.916, '10': 0.966 }, ans_hit5: 0.436,
        by_ability5: { 'knowledge-update': 0.9744, 'multi-session': 0.9474, 'single-session-assistant': 0.9821, 'single-session-preference': 0.8333, 'single-session-user': 0.7857, 'temporal-reasoning': 0.9098 } },
      { id: 'reflexion', label: 'Reflexion', self: false, recall: { '1': 0.732, '3': 0.872, '5': 0.918, '10': 0.966 }, ans_hit5: 0.432,
        by_ability5: { 'knowledge-update': 1.0, 'multi-session': 0.9398, 'single-session-assistant': 0.9821, 'single-session-preference': 0.8333, 'single-session-user': 0.8, 'temporal-reasoning': 0.9023 } },
      { id: 'flat', label: 'flat vector', self: false, recall: { '1': 0.756, '3': 0.876, '5': 0.916, '10': 0.966 }, ans_hit5: 0.436,
        by_ability5: { 'knowledge-update': 0.9744, 'multi-session': 0.9474, 'single-session-assistant': 0.9821, 'single-session-preference': 0.8333, 'single-session-user': 0.7857, 'temporal-reasoning': 0.9098 } },
      { id: 'bm25', label: 'BM25 (lexical floor)', self: false, recall: { '1': 0.874, '3': 0.944, '5': 0.970, '10': 0.986 }, ans_hit5: 0.474,
        by_ability5: { 'knowledge-update': 0.9872, 'multi-session': 0.9699, 'single-session-assistant': 1.0, 'single-session-preference': 0.8667, 'single-session-user': 0.9857, 'temporal-reasoning': 0.9624 } },
    ],
    note: {
      zh: '诚实结论:在这个 LLM-free 的 recall 指标上,BM25 词法下界反而领先(tiered @1 落后约 15 点)。分层记忆的价值不在刷这个指标,而在结构化多层召回与可解释性——数字照实摆,不粉饰。',
      en: 'Honest: on this LLM-free recall metric the BM25 lexical floor leads (tiered trails ~15pts @1). The tiered store’s value is structured multi-tier recall + interpretability, not topping this number — shown as-is.',
    },
  },
  itbench: {
    name: 'ITBench', live: false,
    status: { zh: '公开基线 · 本地 k3s 待跑', en: 'published baseline · local k3s run pending' },
    paper_url: 'https://arxiv.org/abs/2502.05352',
    repo_url: 'https://github.com/itbench-hub/ITBench',
    leaderboard_url: 'https://artificialanalysis.ai/evaluations/itbench-aa',
    total_scenarios: 102,
    groups: [
      { id: 'sre', label: { zh: 'SRE · 事件 / 根因', en: 'SRE · incident / RCA' }, maps_to: { zh: '内网 RCA(page 1/2)', en: 'network RCA (page 1/2)' }, sota_pct: 11.4 },
      { id: 'ciso', label: { zh: 'CISO · 安全合规运营', en: 'CISO · security & compliance' }, maps_to: { zh: '自我渗透 / 安全(page 3)', en: 'self-pentest / security (page 3)' }, sota_pct: 25.2 },
      { id: 'finops', label: { zh: 'FinOps · 成本运营', en: 'FinOps · cost ops' }, maps_to: { zh: '(加成场景)', en: '(bonus)' }, sota_pct: 25.8 },
    ],
    note: {
      zh: 'ICML 2025 · IBM。SOTA agent 也只解 SRE 11.4% / CISO 25.2% / FinOps 25.8%,提升空间大。真跑需在 k8s 上部署微服务并注入故障——本机 r450 已有 k3s,可落地,但属重活,分步来。',
      en: 'ICML 2025 · IBM. SOTA agents solve only SRE 11.4% / CISO 25.2% / FinOps 25.8% — lots of headroom. A real run deploys microservices on k8s and injects faults — feasible on r450’s k3s, but heavy; staged.',
    },
  },
  coverage: [
    { cap: { zh: '内网根因分析', en: 'network RCA' }, benchmark: 'ITBench · SRE', covered: true },
    { cap: { zh: '自我渗透 / 安全', en: 'self-pentest / security' }, benchmark: 'ITBench · CISO', covered: true },
    { cap: { zh: '长期记忆 / 检索', en: 'long-term memory / retrieval' }, benchmark: 'LongMemEval-500', covered: true },
    { cap: { zh: '自演化 / 技能归纳', en: 'self-evolution / skill induction' }, benchmark: { zh: '无公开基准 · 原创贡献', en: 'no public benchmark · novel' }, covered: false },
  ],
}
