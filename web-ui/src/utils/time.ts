const CHINA_TIME_ZONE = 'Asia/Shanghai'

function parseUtcTimestamp(value: string): Date | null {
  const normalized = value.trim().replace(' ', 'T')
  if (!normalized) return null
  const hasTimeZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(normalized)
  const date = new Date(hasTimeZone ? normalized : `${normalized}Z`)
  return Number.isNaN(date.getTime()) ? null : date
}

export function formatChinaTimestamp(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) return '—'
  const date = parseUtcTimestamp(value)
  if (!date) return value
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: CHINA_TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(date)
  const fields = Object.fromEntries(parts.map((part) => [part.type, part.value]))
  return `${fields.year}-${fields.month}-${fields.day} ${fields.hour}:${fields.minute}:${fields.second}`
}
