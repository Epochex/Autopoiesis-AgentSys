import { useMemo, useState } from 'react'
import { Canvas, useFrame } from '@react-three/fiber'
import { OrbitControls, Line, Html } from '@react-three/drei'
import { useRef } from 'react'
import * as THREE from 'three'
import type { MeshNode } from '../types'
import type { Lang } from '../i18n'
import { Scramble } from './Motion'

type Model = { links: { src: string; dst: string; relation: string; strength: number }[]; nodes: Record<string, { severity: string; label: string; summary: string }> }
type N3 = { ip: string; cidr: string; label: string; role: string; sev: string; summary: string; out: number; deny: number; ports: string[]; size: number; pos: [number, number, number] }

const SEV = (s: string) => (s === 'high' ? '#ff4d5e' : s === 'medium' || s === 'watch' ? '#ffb347' : '#5fe4d1')

function layout(meshes: Record<string, MeshNode[]>, model: Model | null): { nodes: N3[]; links: [N3, N3, number][] } {
  // cluster by (cidr, role); place clusters on a fibonacci sphere, nodes in a small ball
  const groups = new Map<string, { cidr: string; items: MeshNode[] }>()
  for (const [cidr, list] of Object.entries(meshes)) {
    for (const n of list) {
      const k = `${cidr}|${n.role}`
      if (!groups.has(k)) groups.set(k, { cidr, items: [] })
      groups.get(k)!.items.push(n)
    }
  }
  const G = [...groups.values()]
  const R = 26
  const pos: Record<string, [number, number, number]> = {}
  const nodes: N3[] = []
  G.forEach((g, gi) => {
    const phi = Math.acos(1 - (2 * (gi + 0.5)) / G.length)
    const theta = Math.PI * (1 + Math.sqrt(5)) * gi
    const cx = R * Math.sin(phi) * Math.cos(theta)
    const cy = R * Math.cos(phi)
    const cz = R * Math.sin(phi) * Math.sin(theta)
    g.items.forEach((n, i) => {
      const a = (i / Math.max(1, g.items.length)) * Math.PI * 2
      const rr = 3 + g.items.length * 0.7
      const p: [number, number, number] = [cx + Math.cos(a) * rr, cy + (i % 2 ? rr * 0.4 : -rr * 0.4), cz + Math.sin(a) * rr]
      pos[n.ip] = p
      const m = model?.nodes[n.ip]
      nodes.push({ ip: n.ip, cidr: g.cidr, label: m?.label ?? n.role, role: n.role, sev: m?.severity ?? n.threat, summary: m?.summary ?? '', out: n.out, deny: n.deny, ports: n.ports, size: 0.32 + Math.log10(n.out + 1) * 0.3, pos: p })
    })
  })
  const byIp = new Map(nodes.map((n) => [n.ip, n]))
  const links: [N3, N3, number][] = []
  for (const l of model?.links ?? []) {
    const a = byIp.get(l.src)
    const b = byIp.get(l.dst)
    if (a && b) links.push([a, b, l.strength])
  }
  return { nodes, links }
}

function Node({ n, onHover, onClick, dim }: { n: N3; onHover: (ip: string | null) => void; onClick: (ip: string, cidr: string) => void; dim: boolean }) {
  const [hov, setHov] = useState(false)
  const c = SEV(n.sev)
  const k = (v: number) => (v >= 1000 ? `${Math.round(v / 1000)}k` : `${v}`)
  return (
    <group position={n.pos}>
      <mesh
        onPointerOver={(e) => { e.stopPropagation(); setHov(true); onHover(n.ip); document.body.style.cursor = 'pointer' }}
        onPointerOut={() => { setHov(false); onHover(null); document.body.style.cursor = '' }}
        onClick={(e) => { e.stopPropagation(); onClick(n.ip, n.cidr) }}
      >
        <sphereGeometry args={[n.size, 24, 24]} />
        <meshStandardMaterial color={c} emissive={c} emissiveIntensity={hov ? 2.6 : dim ? 0.4 : 1.3} roughness={0.3} metalness={0.1} transparent opacity={dim ? 0.4 : 1} />
      </mesh>
      <mesh scale={hov ? 2.3 : 1.7}>
        <sphereGeometry args={[n.size, 16, 16]} />
        <meshBasicMaterial color={c} transparent opacity={hov ? 0.28 : dim ? 0.03 : 0.09} blending={THREE.AdditiveBlending} depthWrite={false} />
      </mesh>
      {hov ? (
        <Html position={[0, n.size + 0.8, 0]} center distanceFactor={34} zIndexRange={[40, 0]} pointerEvents="none">
          <div className="c3d-card" style={{ borderColor: c }}>
            <div className="c3d-card-top"><b><Scramble text={n.ip} /></b><span style={{ color: c }}>{n.sev}</span></div>
            <div className="c3d-card-role" style={{ color: c }}><Scramble text={n.label} /> · {n.cidr}</div>
            <div className="c3d-card-meta">out {k(n.out)} · deny {k(n.deny)} · {n.ports.map((p) => `:${p}`).join(' ')}</div>
            {n.summary ? <div className="c3d-card-sum">{n.summary}</div> : null}
          </div>
        </Html>
      ) : n.sev === 'high' && !dim ? (
        <Html position={[0, n.size + 0.6, 0]} center distanceFactor={48} zIndexRange={[10, 0]} pointerEvents="none">
          <div className="c3d-tag" style={{ color: c }}>{n.ip}</div>
        </Html>
      ) : null}
    </group>
  )
}

// subnet anchors on the left seam → lines that track the rotating clusters (topology↔3D bridge)
function SubnetBridges({ nodes, grp, focusCidr }: { nodes: N3[]; grp: React.RefObject<THREE.Group | null>; focusCidr: string | null }) {
  const cidrs = useMemo(() => {
    const m = new Map<string, [number, number, number][]>()
    nodes.forEach((n) => { if (!m.has(n.cidr)) m.set(n.cidr, []); m.get(n.cidr)!.push(n.pos) })
    const arr = [...m.entries()]
    return arr.map(([cidr, ps], i) => {
      const c = ps.reduce((a, p) => [a[0] + p[0], a[1] + p[1], a[2] + p[2]], [0, 0, 0]).map((v) => v / ps.length)
      return { cidr, centroid: new THREE.Vector3(c[0], c[1], c[2]), anchor: new THREE.Vector3(-46, (i - (arr.length - 1) / 2) * 18, 0) }
    })
  }, [nodes])
  return (
    <>
      {cidrs.map((cl) => (
        <Bridge key={cl.cidr} cl={cl} grp={grp} dim={!!focusCidr && focusCidr !== cl.cidr} />
      ))}
    </>
  )
}

function Bridge({ cl, grp, dim }: { cl: { cidr: string; centroid: THREE.Vector3; anchor: THREE.Vector3 }; grp: React.RefObject<THREE.Group | null>; dim: boolean }) {
  const ref = useRef<{ geometry: { setPositions: (a: number[]) => void } }>(null)
  const v = useRef(new THREE.Vector3())
  useFrame(() => {
    if (!ref.current || !grp.current) return
    v.current.copy(cl.centroid).applyQuaternion(grp.current.quaternion)
    ref.current.geometry.setPositions([cl.anchor.x, cl.anchor.y, cl.anchor.z, v.current.x, v.current.y, v.current.z])
  })
  return (
    <>
      <Line ref={ref as never} points={[[cl.anchor.x, cl.anchor.y, cl.anchor.z], [0, 0, 0]]} color="#5fe4d1" lineWidth={1} transparent opacity={dim ? 0.06 : 0.34} dashed dashScale={3} />
      <mesh position={cl.anchor}>
        <sphereGeometry args={[0.7, 12, 12]} />
        <meshBasicMaterial color="#5fe4d1" />
      </mesh>
      <Html position={[cl.anchor.x, cl.anchor.y + 2, 0]} center distanceFactor={50} pointerEvents="none">
        <div className="c3d-anchor" style={{ opacity: dim ? 0.3 : 1 }}>{cl.cidr}</div>
      </Html>
    </>
  )
}

function Scene({ meshes, model, onHoverIp, onClickIp, focusCidr }: { meshes: Record<string, MeshNode[]>; model: Model | null; onHoverIp: (ip: string | null) => void; onClickIp: (ip: string, cidr: string) => void; focusCidr: string | null }) {
  const { nodes, links } = useMemo(() => layout(meshes, model), [meshes, model])
  const grp = useRef<THREE.Group>(null)
  useFrame((_, dt) => { if (grp.current) grp.current.rotation.y += dt * 0.04 })
  return (
    <>
      <ambientLight intensity={0.6} />
      <pointLight position={[40, 40, 40]} intensity={1.4} />
      <fog attach="fog" args={['#05080a', 60, 130]} />
      <SubnetBridges nodes={nodes} grp={grp} focusCidr={focusCidr} />
      <group ref={grp}>
        {links.map(([a, b, w], i) => {
          const dim = !!focusCidr && a.cidr !== focusCidr && b.cidr !== focusCidr
          return (
            <Line key={i} points={[a.pos, b.pos]} color={a.sev === 'high' || b.sev === 'high' ? '#ff4d5e' : '#5fe4d1'} lineWidth={0.4 + w * 0.5} transparent opacity={dim ? 0.05 : 0.35} />
          )
        })}
        {nodes.map((n) => (
          <Node key={n.ip} n={n} onHover={onHoverIp} onClick={onClickIp} dim={!!focusCidr && n.cidr !== focusCidr} />
        ))}
      </group>
    </>
  )
}

export function Constellation3D({ meshes, model, lang, onClose, onHoverIp, onClickIp, focusCidr }: { meshes: Record<string, MeshNode[]>; model: Model | null; lang: Lang; onClose: () => void; onHoverIp: (ip: string | null) => void; onClickIp: (ip: string, cidr: string) => void; focusCidr: string | null }) {
  const count = Object.values(meshes).reduce((a, l) => a + l.length, 0)
  return (
    <div className="c3d-inline">
      <div className="c3d-bar">
        <span className="c3d-kicker">{lang === 'zh' ? '三维全网星座' : '3D constellation'} · {count}{model ? ` · ${model.links.length}↔` : ''}</span>
        <span className="c3d-hint">{lang === 'zh' ? '拖拽旋转 · 滚轮缩放 · 悬停看画像' : 'orbit · zoom · hover'}</span>
        <button className="tc-x" onClick={onClose}>✕</button>
      </div>
      <Canvas camera={{ position: [0, 0, 74], fov: 46 }} dpr={[1, 2]} gl={{ antialias: true, alpha: true }} style={{ background: 'transparent' }}>
        <Scene meshes={meshes} model={model} onHoverIp={onHoverIp} onClickIp={onClickIp} focusCidr={focusCidr} />
        <OrbitControls enablePan={false} autoRotate autoRotateSpeed={0.3} minDistance={34} maxDistance={120} />
      </Canvas>
    </div>
  )
}
