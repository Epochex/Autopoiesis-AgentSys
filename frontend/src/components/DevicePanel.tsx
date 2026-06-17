import { useState } from 'react'
import type { Subnet } from '../types'
import type { Lang } from '../i18n'

const short = (n: number) => (n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`)

const TH: Record<string, [string, string]> = {
  high: ['threat', '高威胁'],
  watch: ['watch', '观察'],
  ok: ['clean', '正常'],
}

export function DevicePanel({ subnet, lang, onClose }: { subnet: Subnet; lang: Lang; onClose: () => void }) {
  const [open, setOpen] = useState<string | null>(null)
  const devices = subnet.devices ?? []
  const high = devices.filter((d) => d.threat === 'high').length
  return (
    <aside className="drill">
      <div className="drill-head">
        <div>
          <span className="drill-cidr">{subnet.cidr}</span>
          <span className="drill-sub">
            {subnet.hosts} hosts · {short(subnet.flows)} flows{high ? ` · ${high} ${lang === 'zh' ? '高威胁' : 'flagged'}` : ''}
          </span>
        </div>
        <button className="drill-x" onClick={onClose} aria-label="close">✕</button>
      </div>
      <ul className="drill-list">
        {devices.map((d) => {
          const isOpen = open === d.ip
          return (
            <li key={d.ip} className={`dev ${d.threat}`}>
              <button className="dev-row" onClick={() => setOpen(isOpen ? null : d.ip)}>
                <span className={`dev-dot ${d.threat}`} />
                <span className="dev-ip">{d.ip}</span>
                <span className="dev-bar">
                  <b style={{ width: `${Math.min(100, (d.deny / Math.max(1, subnet.flows)) * 100 * 1.6)}%` }} className={d.threat} />
                </span>
                <span className="dev-n">{short(d.deny)}</span>
              </button>
              {isOpen ? (
                <div className="dev-detail">
                  <span className="dev-tag">{TH[d.threat][lang === 'zh' ? 1 : 0]}</span>
                  <span>{lang === 'zh' ? '目标端口' : 'target ports'}: {d.top_ports.map((p) => `:${p}`).join(' ')}</span>
                  <span>{lang === 'zh' ? '拒绝/接受' : 'deny/accept'}: {short(d.deny)} / {d.accept}</span>
                </div>
              ) : null}
            </li>
          )
        })}
      </ul>
    </aside>
  )
}
