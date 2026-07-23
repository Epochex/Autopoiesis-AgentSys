/* The fixed NetOps pipeline, named ONCE for every surface that draws it: the
 * live-situation panel's stage strip and the topology theater's full-chain rail
 * share this vocabulary (it mirrors the backend's runtime_reader stage ids). */
export const PIPELINE: { id: string; zh: string; en: string }[] = [
  { id: 'correlator', zh: '关联器', en: 'CORRELATOR' },
  { id: 'alerts-topic', zh: '告警流', en: 'ALERTS' },
  { id: 'cluster-window', zh: '簇窗口', en: 'CLUSTER' },
  { id: 'aiops-agent', zh: 'AIOps 推理', en: 'AIOPS' },
  { id: 'suggestions-topic', zh: '建议流', en: 'SUGGEST' },
  { id: 'remediation', zh: '处置预案', en: 'REMEDIATE' },
]
