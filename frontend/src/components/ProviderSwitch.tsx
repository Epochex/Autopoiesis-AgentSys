import type { Provider } from '../types'
import { makeT, type Lang } from '../i18n'

export function ProviderSwitch({
  providers,
  active,
  lang,
  onChange,
}: {
  providers: Provider[]
  active: string
  lang: Lang
  onChange: (id: string) => void
}) {
  const t = makeT(lang)
  return (
    <div className="provider-switch" role="group" aria-label={t('reasoner')}>
      <span className="provider-label">{t('reasoner')}</span>
      {providers.map((p) => {
        const disabled = !p.reachable && p.id !== 'rule'
        return (
          <button
            key={p.id}
            className={`provider-btn ${p.id === active ? 'active' : ''}`}
            onClick={() => onChange(p.id)}
            disabled={disabled}
            title={`${p.model} — ${p.note}`}
          >
            <span className={`status-dot ${p.reachable ? 'dot-ok' : 'dot-off'}`} />
            {p.label}
          </button>
        )
      })}
    </div>
  )
}
