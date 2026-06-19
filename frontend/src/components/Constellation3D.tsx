import { useMemo, useState } from 'react'
import { Canvas, useFrame } from '@react-three/fiber'
import { OrbitControls, Line, Html } from '@react-three/drei'
import { useRef } from 'react'
import * as THREE from 'three'
import type { MeshNode } from '../types'
import type { Lang } from '../i18n'

type Model = { links: { src: string; dst: string; relation: string; strength: number }[]; nodes: Record<string, { severity: string; label: string; summary: string }> }
type N3 = { ip: string; label: string; sev: string; size: number; pos: [number, number, number] }

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
      nodes.push({ ip: n.ip, label: m?.label ?? n.role, sev: m?.severity ?? n.threat, size: 0.6 + Math.log10(n.out + 1) * 0.6, pos: p })
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

function Node({ n, onHover }: { n: N3; onHover: (ip: string | null) => void }) {
  const [hov, setHov] = useState(false)
  const c = SEV(n.sev)
  return (
    <group position={n.pos}>
      <mesh
        onPointerOver={(e) => { e.stopPropagation(); setHov(true); onHover(n.ip) }}
        onPointerOut={() => { setHov(false); onHover(null) }}
      >
        <sphereGeometry args={[n.size, 24, 24]} />
        <meshStandardMaterial color={c} emissive={c} emissiveIntensity={hov ? 2.4 : 1.3} roughness={0.3} metalness={0.1} />
      </mesh>
      <mesh scale={hov ? 3.4 : 2.6}>
        <sphereGeometry args={[n.size, 16, 16]} />
        <meshBasicMaterial color={c} transparent opacity={hov ? 0.22 : 0.1} blending={THREE.AdditiveBlending} depthWrite={false} />
      </mesh>
      {(hov || n.sev === 'high') && (
        <Html position={[0, n.size + 1.4, 0]} center distanceFactor={46} zIndexRange={[10, 0]} pointerEvents="none">
          <div className="c3d-label" style={{ color: c, borderColor: c }}>{n.label}<span className="c3d-label-ip">{n.ip}</span></div>
        </Html>
      )}
    </group>
  )
}

function Scene({ meshes, model }: { meshes: Record<string, MeshNode[]>; model: Model | null }) {
  const { nodes, links } = useMemo(() => layout(meshes, model), [meshes, model])
  const grp = useRef<THREE.Group>(null)
  useFrame((_, dt) => { if (grp.current) grp.current.rotation.y += dt * 0.04 })
  const [, setHov] = useState<string | null>(null)
  return (
    <>
      <ambientLight intensity={0.6} />
      <pointLight position={[40, 40, 40]} intensity={1.4} />
      <fog attach="fog" args={['#05080a', 60, 130]} />
      <group ref={grp}>
        {links.map(([a, b, w], i) => (
          <Line key={i} points={[a.pos, b.pos]} color={a.sev === 'high' || b.sev === 'high' ? '#ff4d5e' : '#5fe4d1'} lineWidth={0.4 + w * 0.5} transparent opacity={0.35} />
        ))}
        {nodes.map((n) => (
          <Node key={n.ip} n={n} onHover={setHov} />
        ))}
      </group>
    </>
  )
}

export function Constellation3D({ meshes, model, lang, onClose }: { meshes: Record<string, MeshNode[]>; model: Model | null; lang: Lang; onClose: () => void }) {
  const count = Object.values(meshes).reduce((a, l) => a + l.length, 0)
  return (
    <div className="c3d-overlay">
      <div className="c3d-head">
        <span className="c3d-kicker">{lang === 'zh' ? '全网设备 · 三维关系星座' : 'network · 3D constellation'} · {count} {lang === 'zh' ? '设备' : 'devices'}{model ? ` · ${model.links.length} ${lang === 'zh' ? '关系' : 'links'}` : ''}</span>
        <span className="c3d-hint">{lang === 'zh' ? '拖拽旋转 · 滚轮缩放 · 悬停看画像' : 'drag to orbit · scroll to zoom · hover for profile'}</span>
        <button className="tc-x" onClick={onClose}>✕</button>
      </div>
      <Canvas camera={{ position: [0, 0, 78], fov: 48 }} dpr={[1, 2]} gl={{ antialias: true }} style={{ background: 'radial-gradient(60% 60% at 50% 45%, #0a1417 0%, #05080a 70%)' }}>
        <Scene meshes={meshes} model={model} />
        <OrbitControls enablePan={false} autoRotate autoRotateSpeed={0.35} minDistance={36} maxDistance={120} />
      </Canvas>
    </div>
  )
}
