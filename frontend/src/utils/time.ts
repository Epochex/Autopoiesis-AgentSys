const localTimeFormatter = new Intl.DateTimeFormat(undefined, {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
  timeZoneName: 'short',
})

const localDateTimeFormatter = new Intl.DateTimeFormat(undefined, {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
  timeZoneName: 'short',
})

const utcDateTimeFormatter = new Intl.DateTimeFormat('en-GB', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
  timeZone: 'UTC',
  timeZoneName: 'short',
})

type TimestampStyle = 'time' | 'datetime'

export function parseTimestamp(value: string | null | undefined): Date | null {
  if (!value || value === 'n/a') {
    return null
  }

  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

export function formatMaybeTimestamp(
  value: string | null | undefined,
  style: TimestampStyle = 'datetime',
) {
  const parsed = parseTimestamp(value)
  if (!parsed) {
    return value ?? 'n/a'
  }

  return style === 'time'
    ? localTimeFormatter.format(parsed)
    : localDateTimeFormatter.format(parsed)
}

export function timestampTooltip(value: string | null | undefined) {
  const parsed = parseTimestamp(value)
  if (!parsed) {
    return undefined
  }

  return `${utcDateTimeFormatter.format(parsed)} · source UTC`
}

export function formatEvidenceValue(
  value: string | number | boolean | null | undefined,
) {
  if (value === null || value === undefined || value === '') {
    return '-'
  }

  if (typeof value === 'string') {
    return formatMaybeTimestamp(value)
  }

  return String(value)
}

export function formatDurationMs(value: number | null | undefined) {
  if (value === null || value === undefined) {
    return 'n/a'
  }

  if (value < 1000) {
    return `${value} ms`
  }

  if (value < 60_000) {
    return `${(value / 1000).toFixed(value < 10_000 ? 2 : 1)} s`
  }

  const totalSeconds = Math.floor(value / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  if (minutes < 60) {
    return `${minutes}m ${seconds}s`
  }

  const hours = Math.floor(minutes / 60)
  const remainingMinutes = minutes % 60
  return `${hours}h ${remainingMinutes}m`
}

export function formatPreciseDurationMs(value: number | null | undefined) {
  if (value === null || value === undefined) {
    return 'n/a'
  }

  return `${(Math.max(value, 0) / 1000).toFixed(3)} s`
}
