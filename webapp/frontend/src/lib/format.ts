const NUM = new Intl.NumberFormat('zh-CN')

/** 缺失值统一用半角连字符, 不用长破折号 */
export const DASH = '-'

export function fmtInt(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return DASH
  return NUM.format(n)
}

export function fmtBytes(bytes: number | null | undefined): string {
  if (!bytes && bytes !== 0) return DASH
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB']
  let value = bytes / 1024
  let i = 0
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024
    i += 1
  }
  return `${value >= 100 ? Math.round(value) : value.toFixed(1)} ${units[i]}`
}

export function extOf(name: string): string {
  const i = name.lastIndexOf('.')
  return i < 0 ? '' : name.slice(i + 1).toLowerCase()
}

export function dirname(path: string): string {
  return path.replace(/[\\/][^\\/]*$/, '')
}

/** 后端给的是 Python repr(',')，直接显示成 "','" 很难看，翻成人话 */
export function delimLabel(raw: string | null | undefined): string {
  if (!raw) return DASH
  const s = raw.replace(/^['"]|['"]$/g, '')
  if (s === '\\t') return 'Tab'
  if (s === ',') return '逗号'
  if (s === ';') return '分号'
  if (s === '|') return '竖线'
  return s
}
