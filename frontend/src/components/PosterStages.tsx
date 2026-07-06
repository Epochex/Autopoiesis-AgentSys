/* ── Stage diagrams · one clean single-focus figure per ledger step.
   Shared vocabulary (tp-dia-*), shared 720×280 content viewBox, no floating
   noise labels, no heavy black mass. The poster chrome (tag / verb / ghost /
   headline / caption) is the shared StageFrame in TrajectoryPage. ── */

const VBW = 720, VBH = 280
const notch = (x: number, y: number, w: number, h: number, n = 6) =>
  `M${x + n} ${y} H${x + w} V${y + h - n} L${x + w - n} ${y + h} H${x} V${y + n} Z`

function Dia({ children }: { children: React.ReactNode }) {
  return (
    <svg className="tp-dia" viewBox={`0 0 ${VBW} ${VBH}`} preserveAspectRatio="xMidYMid meet">
      <defs>
        <pattern id="tp-hatch" width={7} height={7} patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
          <rect width={2.4} height={7} fill="var(--ink)" />
        </pattern>
      </defs>
      {children}
    </svg>
  )
}

/* 01 · ALERT — the raw signal fans to its in-scope assets */
export function GAlert({ v, zh }: { v: { query: string; assets: string[] }; zh: boolean }) {
  const a = v.assets.length ? v.assets : ['—']
  const y = (i: number) => (a.length <= 1 ? 140 : 66 + i * (148 / (a.length - 1)))
  return (
    <Dia>
      <path className="tp-dia-node acid" d={notch(40, 118, 130, 44)} />
      <text className="tp-dia-lab ink" x={105} y={144} textAnchor="middle">{zh ? '告警信号' : 'SIGNAL IN'}</text>
      {a.map((s, i) => (
        <g key={i} className="tp-dia-in" style={{ animationDelay: `${120 + i * 90}ms` }}>
          <line className="tp-dia-line" x1={170} y1={140} x2={318} y2={y(i)} />
          <circle className="tp-dia-pin" cx={318} cy={y(i)} r={3.5} />
          <path className="tp-dia-node" d={notch(318, y(i) - 17, 320, 34)} />
          <text className="tp-dia-lab" x={334} y={y(i) + 4}>{(s.length > 30 ? s.slice(0, 29) + '…' : s).toUpperCase()}<title>{s}</title></text>
        </g>
      ))}
      <text className="tp-dia-cap" x={40} y={246}>{zh ? `${v.assets.length} 台资产在范围内` : `${v.assets.length} ASSETS IN SCOPE`}</text>
    </Dia>
  )
}

/* 02 · MEMORY — four tiers, bar length = hits recalled */
export function GMemoryRead({ v, zh }: { v: { tiers: { id: string; zh: string; en: string; keys: string[] }[]; total: number }; zh: boolean }) {
  const t = v.tiers.slice(0, 4)
  const max = Math.max(1, ...t.map((x) => x.keys.length))
  return (
    <Dia>
      {t.map((tier, i) => {
        const yy = 34 + i * 56
        const w = (tier.keys.length / max) * 430
        const empty = tier.keys.length === 0
        return (
          <g key={tier.id}>
            <text className="tp-dia-lab" x={40} y={yy + 20}>{tier.en} · {zh ? tier.zh : tier.id.slice(0, 4).toUpperCase()}</text>
            <path className="tp-dia-track" d={notch(160, yy + 4, 430, 22, 4)} />
            {!empty ? <rect className="tp-dia-fill grow" x={160} y={yy + 4} width={Math.max(6, w)} height={22} style={{ animationDelay: `${i * 90}ms` }} /> : null}
            <text className={`tp-dia-num ${empty ? 'zero' : ''}`} x={632} y={yy + 22} textAnchor="end">{tier.keys.length}</text>
          </g>
        )
      })}
    </Dia>
  )
}

/* 03 · SKILLS — the controller clamps the full set down to N read-only skills */
export function GSkillsExposed({ v, zh }: { v: { skills: string[] }; zh: boolean }) {
  const sk = v.skills.slice(0, 3)
  const code = (s: string) => (s.replace(/^check[_-]?/i, '').replace(/_/g, ' ').slice(0, 18) || 'skill').toUpperCase()
  return (
    <Dia>
      {/* full-set field, restrained hatch, bleeds left */}
      <rect className="tp-dia-hatchbox" x={40} y={70} width={132} height={140} />
      <text className="tp-dia-sub" x={106} y={230} textAnchor="middle">{zh ? '技能全集' : 'FULL TOOLSET'}</text>
      {/* funnel — the attention controller narrows the full set to the exposed few */}
      <path className="tp-dia-funnel" d="M172 74 L352 104 L352 176 L172 206 Z" />
      <text className="tp-dia-sub" x={262} y={137} textAnchor="middle">TOP-K 3</text>
      <text className="tp-dia-sub" x={262} y={154} textAnchor="middle">SCORE &gt;0.5</text>
      <polygon className="tp-dia-ptr" points="352,134 352,146 364,140" />
      {/* exposed read-only skills */}
      {sk.map((s, i) => {
        const yy = 96 + i * 34
        return (
          <g key={i} className="tp-dia-in" style={{ animationDelay: `${140 + i * 100}ms` }}>
            <path className="tp-dia-node acid" d={notch(360, yy, 250, 26, 4)} />
            <text className="tp-dia-lab ink" x={374} y={yy + 18}>{code(s)}<title>{s}</title></text>
          </g>
        )
      })}
      <path className="tp-dia-node" d={notch(628, 96, 62, 26 + (sk.length - 1) * 34, 4)} fill="var(--ink)" />
      <text className="tp-dia-lab" x={659} y={96 + (26 + (sk.length - 1) * 34) / 2 + 4} textAnchor="middle" fill="var(--paper)">RO</text>
      <text className="tp-dia-cap" x={40} y={252}>{zh ? `仅放行 ${v.skills.length} · 只读 · 硬阻断写` : `${v.skills.length} EXPOSED · READ-ONLY · WRITE-BLOCKED`}</text>
    </Dia>
  )
}

/* 04 · PROBE — each read-only probe pins to observed evidence */
export function GToolCalled({ v, zh }: { v: { probes: { skill: string; ev: string[]; cost: number | null }[]; faces: string[] }; zh: boolean }) {
  const P = v.probes, F = v.faces
  const py = (i: number) => (P.length <= 1 ? 140 : 60 + i * (160 / (P.length - 1)))
  const fy = (i: number) => (F.length <= 1 ? 140 : 60 + i * (160 / (F.length - 1)))
  const code = (s: string) => (s.replace(/^check[_-]?/i, '').replace(/_/g, ' ').slice(0, 12) || 'FN').toUpperCase()
  return (
    <Dia>
      <text className="tp-dia-sub" x={130} y={34}>{zh ? '只读探针' : 'PROBES'}</text>
      <text className="tp-dia-sub" x={470} y={34} textAnchor="end">{zh ? '被观测证据' : 'OBSERVED EVIDENCE'}</text>
      {P.map((p, pi) => p.ev.map((e) => {
        const fi = F.indexOf(e); if (fi < 0) return null
        return <path key={pi + e} className="tp-dia-line draw" style={{ animationDelay: `${200 + pi * 110}ms` }}
          d={`M258 ${py(pi)} C 340 ${py(pi)}, 360 ${fy(fi)}, 438 ${fy(fi)}`} pathLength={1} />
      }))}
      {P.map((p, i) => (
        <g key={'p' + i}>
          <path className="tp-dia-node acid" d={notch(48, py(i) - 17, 210, 34, 4)} />
          <text className="tp-dia-lab ink" x={62} y={py(i) + 4}>{code(p.skill)}<title>{p.skill}</title></text>
        </g>
      ))}
      {F.map((f, i) => (
        <g key={'f' + i} className="tp-dia-in" style={{ animationDelay: `${420 + i * 90}ms` }}>
          <circle className="tp-dia-pin" cx={438} cy={fy(i)} r={3.5} />
          <path className="tp-dia-node" d={notch(438, fy(i) - 15, 234, 30, 4)} />
          <text className="tp-dia-lab" x={452} y={fy(i) + 4}>{f.length > 22 ? f.slice(0, 21) + '…' : f}<title>{f}</title></text>
        </g>
      ))}
    </Dia>
  )
}

/* 05 · CONTEXT — evidence squeezed into the token budget */
export function GContextCompiled({ v, zh }: { v: { before: number | null; after: number | null; ratio: number | null; kept: number; missing: string[] }; zh: boolean }) {
  const before = v.before ?? 0, after = v.after ?? before
  const X = 40, Y = 70, FULL = 470, H = 46
  const keepW = before > 0 ? Math.max(10, (after / before) * FULL) : FULL
  const pips = Math.min(Math.max(v.kept, 1), 16)
  return (
    <Dia>
      <text className="tp-dia-sub" x={X} y={Y - 12}>{zh ? '上下文 → 预算' : 'CONTEXT → BUDGET'}</text>
      <path className="tp-dia-track" d={notch(X, Y, FULL, H)} />
      {keepW < FULL - 2 ? <rect className="tp-dia-drop" x={X + keepW} y={Y} width={FULL - keepW} height={H} /> : null}
      <rect className="tp-dia-fill grow" x={X} y={Y} width={keepW} height={H} />
      <text className="tp-dia-lab ink" x={X + 14} y={Y + 30}>{after} / {before} TK</text>
      <text className="tp-dia-big" x={X + FULL + 30} y={Y + 38}>{(v.ratio ?? 1).toFixed(2)}<tspan className="tp-dia-x">×</tspan></text>
      <text className="tp-dia-sub" x={X + FULL + 30} y={Y + 58}>{zh ? '压缩比' : 'RATIO'}</text>
      <text className="tp-dia-sub" x={X} y={Y + 90}>{zh ? '证据覆盖' : 'EVIDENCE COVERAGE'}</text>
      {Array.from({ length: pips }).map((_, i) => (
        <rect key={i} className="tp-dia-pip grow" x={X + i * 22} y={Y + 100} width={16} height={16} style={{ animationDelay: `${i * 40}ms` }} />
      ))}
      <text className="tp-dia-cap" x={X} y={Y + 148}>{v.missing.length ? (zh ? `缺失面 ${v.missing.length}` : `${v.missing.length} MISSING`) : (zh ? '无缺失 · 全覆盖' : 'NO DROP · FULL COVERAGE')}</text>
    </Dia>
  )
}

/* 06 · VERIFY — recall meter + a pass/reject seal */
export function GVerifierResult({ v, zh }: { v: { passed: boolean; recall: number | null }; zh: boolean }) {
  const recall = v.recall ?? 0
  const segs = 20, lit = Math.round(recall * segs)
  const X = 40, Y = 96, SW = 22, GAP = 4
  return (
    <Dia>
      <text className="tp-dia-sub" x={X} y={Y - 16}>{zh ? '被观测 → 被引用 · 召回' : 'OBSERVED → CITED · RECALL'}</text>
      <path className="tp-dia-track" d={notch(X - 6, Y - 6, segs * (SW + GAP) + 8, 44, 4)} />
      {Array.from({ length: segs }).map((_, i) => (
        <rect key={i} className={`tp-dia-seg ${i < lit ? 'on' : ''} grow`} x={X + i * (SW + GAP)} y={Y} width={SW} height={32} style={{ animationDelay: `${i * 24}ms` }} />
      ))}
      <text className="tp-dia-big" x={X} y={Y + 96}>{Math.round(recall * 100)}<tspan className="tp-dia-x">%</tspan></text>
      <text className="tp-dia-sub" x={X + 110} y={Y + 96}>{zh ? '召回' : 'RECALL'}</text>
      <g className={`tp-dia-seal ${v.passed ? 'pass' : 'reject'}`}>
        <path className="tp-dia-node" d={notch(X + 300, Y + 66, 150, 46, 6)} />
        <text className="tp-dia-seal-t" x={X + 375} y={Y + 96} textAnchor="middle">{v.passed ? 'PASS' : 'REJECT'}</text>
      </g>
    </Dia>
  )
}

/* 07 · DIAGNOSE — confidence gauge + the cited evidence (verdict is the headline) */
export function GDiagnosisCompleted({ v, zh }: { v: { confidence: number | null; rootKey: string; readonly: boolean; cited: string[]; label: string }; zh: boolean }) {
  const conf = v.confidence ?? 0
  const X = 40, Y = 60, W = 470
  const px = X + conf * W
  const cited = v.cited.slice(0, 2)
  return (
    <Dia>
      <text className="tp-dia-sub" x={X} y={Y - 14}>{zh ? '诊断置信 · 对齐真值' : 'CONFIDENCE · ALIGNED TO GT'}</text>
      <line className="tp-dia-axis" x1={X} y1={Y} x2={X + W} y2={Y} />
      {[0, 0.25, 0.5, 0.75, 1].map((f) => (
        <g key={f}><line className="tp-dia-tick" x1={X + f * W} y1={Y} x2={X + f * W} y2={Y + 7} /><text className="tp-dia-sub" x={X + f * W} y={Y + 22} textAnchor="middle">{f.toFixed(2)}</text></g>
      ))}
      <rect className="tp-dia-fill grow" x={X} y={Y - 8} width={conf * W} height={8} />
      <polygon className="tp-dia-ptr" points={`${px - 6},${Y - 16} ${px + 6},${Y - 16} ${px},${Y - 6}`} />
      <text className="tp-dia-big" x={X + W + 24} y={Y + 8}>{conf.toFixed(2)}</text>
      <text className="tp-dia-sub" x={X} y={Y + 70}>{zh ? '判决所引用的证据' : 'EVIDENCE THIS VERDICT CITES'}</text>
      {cited.map((id, i) => (
        <g key={id} className="tp-dia-in" style={{ animationDelay: `${160 + i * 100}ms` }}>
          <path className="tp-dia-node acid" d={notch(X + i * 320, Y + 84, 300, 34, 4)} />
          <circle className="tp-dia-pin" cx={X + i * 320 + 12} cy={Y + 101} r={3} />
          <text className="tp-dia-lab ink" x={X + i * 320 + 28} y={Y + 105}>{id}<title>{id}</title></text>
        </g>
      ))}
      <text className="tp-dia-cap" x={X} y={Y + 150}>{zh ? `只读 ${v.readonly ? '✓' : '✕'} · 缺失证据 无 · 核验通过` : `READ-ONLY ${v.readonly ? '✓' : '✕'} · NO MISSING · VERIFIED`}</text>
    </Dia>
  )
}
