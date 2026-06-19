import { useMemo, useRef, useState } from 'react'
import { Canvas, useFrame } from '@react-three/fiber'
import { OrbitControls, Line, Html } from '@react-three/drei'
import * as THREE from 'three'
import type { DataStats, MeshNode, Topology } from '../types'
import type { Lang } from '../i18n'
import { Scramble } from './Motion'

type Model = { links: { src: string; dst: string; relation: string; strength: number }[]; nodes: Record<string, { severity: string; label: string; summary: string }> }
type Vec = [number, number, number]
type GNode = {
  id: string; kind: 'attacker' | 'fg' | 'intf' | 'subnet' | 'device'
  label: string; sub?: string; sev: string; size: number; pos: Vec
  ip?: string; cidr?: string; out?: number; deny?: number; ports?: string[]; summary?: string
}
type GEdge = { a: Vec; b: Vec; kind: 'flow' | 'rel'; sev: string }

const SEV = (s: string) => (s === 'high' ? '#ff4d5e' : s === 'medium' || s === 'watch' ? '#ffb347' : '#5fe4d1')
// flow layers along X (data flows WAN -> device)
const LX = { attacker: -58, fg: -32, intf: -8, subnet: 20, device: 52 }

function build(topo: Topology, stats: DataStats, meshes: Record<string, MeshNode[]>, model: Model | null): { nodes: GNode[]; edges: GEdge[] } {
  const nodes: GNode[] = []
  const edges: GEdge[] = []
  const at = (id: string) => nodes.find((n) => n.id === id)?.pos
  // attackers
  const atk = stats.topAttackerSrc.slice(0, 3)
  atk.forEach(([ip, v], i) => nodes.push({ id: `a${i}`, kind: 'attacker', label: ip, sev: 'high', size: 1.2 + Math.log10(v + 1) * 0.2, pos: [LX.attacker, (i - (atk.length - 1) / 2) * 18, 0] }))
  // fortigate
  nodes.push({ id: 'fg', kind: 'fg', label: 'FortiGate', sub: topo.core.ip, sev: 'low', size: 3, pos: [LX.fg, 0, 0] })
  // interfaces (lan first, layered in Y)
  const lan = topo.interfaces
  lan.forEach((it, i) => nodes.push({ id: `if-${it.name}`, kind: 'intf', label: it.name, sub: `${Math.round(it.flows / 1000)}k`, sev: 'low', size: 1.6 + Math.log10(it.flows + 1) * 0.18, pos: [LX.intf, (i - (lan.length - 1) / 2) * 16, 0] }))
  // subnets
  const subs = topo.subnets.filter((s) => s.hosts > 1)
  const subY: Record<string, number> = {}
  subs.forEach((s, i) => { subY[s.cidr] = (i - (subs.length - 1) / 2) * 34; nodes.push({ id: `s-${s.cidr}`, kind: 'subnet', label: s.cidr, sub: `${s.hosts}h`, sev: 'medium', size: 2.2, pos: [LX.subnet, subY[s.cidr], 0] }) })
  // devices clustered behind their subnet
  for (const [cidr, list] of Object.entries(meshes)) {
    const cy = subY[cidr] ?? 0
    list.slice(0, 40).forEach((n, i) => {
      const a = (i / Math.max(1, list.length)) * Math.PI * 2
      const rr = 5 + list.length * 0.55
      const m = model?.nodes[n.ip]
      const pos: Vec = [LX.device + (i % 3) * 4, cy + Math.cos(a) * rr, Math.sin(a) * rr]
      nodes.push({ id: `d-${n.ip}`, kind: 'device', label: m?.label ?? n.role, cidr, ip: n.ip, sev: m?.severity ?? n.threat, summary: m?.summary, out: n.out, deny: n.deny, ports: n.ports, size: 0.5 + Math.log10(n.out + 1) * 0.3, pos })
    })
  }
  // flow edges across layers
  atk.forEach((_, i) => { const a = at(`a${i}`); const b = at('fg'); if (a && b) edges.push({ a, b, kind: 'flow', sev: 'high' }) })
  lan.forEach((it) => { const a = at('fg'); const b = at(`if-${it.name}`); if (a && b) edges.push({ a, b, kind: 'flow', sev: 'low' }) })
  subs.forEach((s) => { const a = at(`if-${s.intf}`); const b = at(`s-${s.cidr}`); if (a && b) edges.push({ a, b, kind: 'flow', sev: 'low' }) })
  for (const [cidr, list] of Object.entries(meshes)) {
    const a = at(`s-${cidr}`)
    list.slice(0, 40).forEach((n) => { const b = at(`d-${n.ip}`); if (a && b) edges.push({ a, b, kind: 'flow', sev: 'low' }) })
  }
  // device relationships from DeepSeek
  for (const l of model?.links ?? []) { const a = at(`d-${l.src}`); const b = at(`d-${l.dst}`); if (a && b) edges.push({ a, b, kind: 'rel', sev: 'high' }) }
  return { nodes, edges }
}

function Particle({ a, b, color, off, speed }: { a: Vec; b: Vec; color: string; off: number; speed: number }) {
  const ref = useRef<THREE.Mesh>(null)
  const va = useMemo(() => new THREE.Vector3(...a), [a])
  const vb = useMemo(() => new THREE.Vector3(...b), [b])
  useFrame((s) => { if (ref.current) { const t = (s.clock.elapsedTime * speed + off) % 1; ref.current.position.lerpVectors(va, vb, t) } })
  return <mesh ref={ref}><sphereGeometry args={[0.5, 8, 8]} /><meshBasicMaterial color={color} /></mesh>
}

function Device({ n, onHover, onClick, dim }: { n: GNode; onHover: (ip: string | null) => void; onClick: (ip: string, cidr: string) => void; dim: boolean }) {
  const [hov, setHov] = useState(false)
  const c = SEV(n.sev)
  const k = (v: number) => (v >= 1000 ? `${Math.round(v / 1000)}k` : `${v}`)
  return (
    <group position={n.pos}>
      <mesh
        onPointerOver={(e) => { e.stopPropagation(); setHov(true); onHover(n.ip!); document.body.style.cursor = 'pointer' }}
        onPointerOut={() => { setHov(false); onHover(null); document.body.style.cursor = '' }}
        onClick={(e) => { e.stopPropagation(); onClick(n.ip!, n.cidr!) }}
      >
        <sphereGeometry args={[n.size, 22, 22]} />
        <meshStandardMaterial color={c} emissive={c} emissiveIntensity={hov ? 2.6 : dim ? 0.4 : 1.3} roughness={0.3} transparent opacity={dim ? 0.4 : 1} />
      </mesh>
      <mesh scale={hov ? 2.3 : 1.7}><sphereGeometry args={[n.size, 14, 14]} /><meshBasicMaterial color={c} transparent opacity={hov ? 0.28 : dim ? 0.03 : 0.09} blending={THREE.AdditiveBlending} depthWrite={false} /></mesh>
      {hov ? (
        <Html position={[0, n.size + 0.8, 0]} center distanceFactor={30} zIndexRange={[40, 0]} pointerEvents="none">
          <div className="c3d-card" style={{ borderColor: c }}>
            <div className="c3d-card-top"><b><Scramble text={n.ip!} /></b><span style={{ color: c }}>{n.sev}</span></div>
            <div className="c3d-card-role" style={{ color: c }}><Scramble text={n.label} /> · {n.cidr}</div>
            <div className="c3d-card-meta">out {k(n.out!)} · deny {k(n.deny!)} · {(n.ports ?? []).map((p) => `:${p}`).join(' ')}</div>
            {n.summary ? <div className="c3d-card-sum">{n.summary}</div> : null}
          </div>
        </Html>
      ) : null}
    </group>
  )
}

function Backbone({ n }: { n: GNode }) {
  const c = n.kind === 'attacker' ? '#ff4d5e' : n.kind === 'subnet' ? '#ffb347' : '#5fe4d1'
  return (
    <group position={n.pos}>
      <mesh><sphereGeometry args={[n.size, 22, 22]} /><meshStandardMaterial color={c} emissive={c} emissiveIntensity={1.2} roughness={0.35} /></mesh>
      <mesh scale={2.1}><sphereGeometry args={[n.size, 14, 14]} /><meshBasicMaterial color={c} transparent opacity={0.07} blending={THREE.AdditiveBlending} depthWrite={false} /></mesh>
      <Html position={[0, n.size + 1.6, 0]} center distanceFactor={62} pointerEvents="none">
        <div className={`c3d-bb k-${n.kind}`}>{n.label}{n.sub ? <span>{n.sub}</span> : null}</div>
      </Html>
    </group>
  )
}

function Plane({ x, label }: { x: number; label: string }) {
  return (
    <group position={[x, 0, 0]} rotation={[0, Math.PI / 2, 0]}>
      <mesh><ringGeometry args={[1, 46, 64]} /><meshBasicMaterial color="#5fe4d1" transparent opacity={0.02} side={THREE.DoubleSide} /></mesh>
      <Html position={[0, -44, 0]} center distanceFactor={80} pointerEvents="none"><div className="c3d-plane">{label}</div></Html>
    </group>
  )
}

function Scene({ topo, stats, meshes, model, onHoverIp, onClickIp, focusCidr, lang }: { topo: Topology; stats: DataStats; meshes: Record<string, MeshNode[]>; model: Model | null; onHoverIp: (ip: string | null) => void; onClickIp: (ip: string, cidr: string) => void; focusCidr: string | null; lang: Lang }) {
  const { nodes, edges } = useMemo(() => build(topo, stats, meshes, model), [topo, stats, meshes, model])
  const grp = useRef<THREE.Group>(null)
  useFrame((_, dt) => { if (grp.current) grp.current.rotation.y += dt * 0.03 })
  const flow = edges.filter((e) => e.kind === 'flow')
  return (
    <>
      <ambientLight intensity={0.65} />
      <pointLight position={[40, 50, 60]} intensity={1.5} />
      <fog attach="fog" args={['#05080a', 90, 200]} />
      <group ref={grp}>
        {([['attacker', LX.attacker, lang === 'zh' ? 'WAN' : 'WAN'], ['fg', LX.fg, 'FortiGate'], ['intf', LX.intf, lang === 'zh' ? '接口' : 'intf'], ['subnet', LX.subnet, lang === 'zh' ? '子网' : 'subnet'], ['device', LX.device, lang === 'zh' ? '设备' : 'device']] as [string, number, string][]).map(([k, x, l]) => (
          <Plane key={k} x={x} label={l} />
        ))}
        {edges.map((e, i) => {
          const dim = e.kind === 'rel' && false
          return <Line key={i} points={[e.a, e.b]} color={e.kind === 'rel' ? '#ff4d5e' : '#3a5a55'} lineWidth={e.kind === 'rel' ? 1 : 0.6} transparent opacity={dim ? 0.05 : e.kind === 'rel' ? 0.32 : 0.22} />
        })}
        {flow.map((e, i) => (<Particle key={i} a={e.a} b={e.b} color={SEV(e.sev)} off={(i % 5) / 5} speed={0.18} />))}
        {nodes.filter((n) => n.kind !== 'device').map((n) => <Backbone key={n.id} n={n} />)}
        {nodes.filter((n) => n.kind === 'device').map((n) => (
          <Device key={n.id} n={n} onHover={onHoverIp} onClick={onClickIp} dim={!!focusCidr && n.cidr !== focusCidr} />
        ))}
      </group>
    </>
  )
}

export function Constellation3D({ topo, stats, meshes, model, lang, onClose, onHoverIp, onClickIp, focusCidr }: { topo: Topology; stats: DataStats; meshes: Record<string, MeshNode[]>; model: Model | null; lang: Lang; onClose: () => void; onHoverIp: (ip: string | null) => void; onClickIp: (ip: string, cidr: string) => void; focusCidr: string | null }) {
  const count = Object.values(meshes).reduce((a, l) => a + l.length, 0)
  return (
    <div className="c3d-inline c3d-full">
      <div className="c3d-bar">
        <span className="c3d-kicker">{lang === 'zh' ? '三维分层数据流 · 全网' : '3D layered flow'} · {count}{model ? ` · ${model.links.length}↔` : ''}</span>
        <span className="c3d-hint">{lang === 'zh' ? 'WAN→网关→接口→子网→设备 · 拖拽旋转 · 点设备研判' : 'WAN→gw→intf→subnet→device · orbit · click device'}</span>
        <button className="tc-x" onClick={onClose}>✕</button>
      </div>
      <Canvas camera={{ position: [0, 6, 150], fov: 48 }} dpr={[1, 2]} gl={{ antialias: true, alpha: true }} style={{ background: 'radial-gradient(70% 70% at 50% 45%, #0a1417 0%, #05080a 72%)' }}>
        <Scene topo={topo} stats={stats} meshes={meshes} model={model} onHoverIp={onHoverIp} onClickIp={onClickIp} focusCidr={focusCidr} lang={lang} />
        <OrbitControls enablePan autoRotate autoRotateSpeed={0.25} minDistance={60} maxDistance={260} />
      </Canvas>
    </div>
  )
}
