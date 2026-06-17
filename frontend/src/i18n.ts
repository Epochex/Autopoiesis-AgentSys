export type UiLocale = 'en' | 'zh'

export function pick(locale: UiLocale, en: string, zh: string) {
  return locale === 'zh' ? zh : en
}
