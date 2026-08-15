import type { CodexUsageAccount, CodexUsageWindow, Usage } from '../types.js'

export const ZERO: Usage = { calls: 0, input: 0, output: 0, total: 0 }

const remaining = (window?: CodexUsageWindow) =>
  typeof window?.used_percent === 'number' ? Math.max(0, Math.round(100 - window.used_percent)) : null

const windowFor = (account: CodexUsageAccount, kind: 'session' | 'weekly') =>
  (account.windows ?? []).find(window => {
    const label = String(window.label ?? '').toLowerCase()
    return kind === 'session' ? label.includes('session') : label.includes('week')
  })

const accountMarker = (account: CodexUsageAccount) => {
  if (!account.available) return '?'
  const values = [remaining(windowFor(account, 'session')), remaining(windowFor(account, 'weekly'))].filter(
    (value): value is number => value != null
  )
  if (values.some(value => value <= 0)) return '!'
  return account.active ? '●' : '○'
}

export function formatCodexUsage(accounts: CodexUsageAccount[] | undefined, cols: number): string {
  const visible = (accounts ?? []).filter(account => account.provider === 'openai-codex' || !account.provider)
  if (!visible.length) return ''

  if (cols < 72) {
    const values = visible.flatMap(account =>
      [remaining(windowFor(account, 'session')), remaining(windowFor(account, 'weekly'))].filter(
        (value): value is number => value != null
      )
    )
    return `Codex ${values.length ? `min ${Math.min(...values)}%` : '?'} · ${visible.length}`
  }

  return visible
    .map(account => {
      const session = remaining(windowFor(account, 'session'))
      const weekly = remaining(windowFor(account, 'weekly'))
      const marker = accountMarker(account)
      if (cols < 100) return `${marker} ${session ?? '?'}/${weekly ?? '?'}`
      return `${marker} S${session ?? '?'} W${weekly ?? '?'}`
    })
    .join(cols < 100 ? ' ' : ' │ ')
}

export function codexUsageDetails(accounts: CodexUsageAccount[] | undefined): string[] {
  return (accounts ?? []).flatMap((account, index) => {
    const marker = accountMarker(account)
    const heading = `${marker} ${account.label || `Codex ${index + 1}`}${account.active ? ' (active)' : ''}`
    const lines = (account.windows ?? []).map(window => {
      const left = remaining(window)
      const used =
        typeof window.used_percent === 'number' ? Math.max(0, Math.min(100, Math.round(window.used_percent))) : null
      const reset = window.reset_human ? ` · resets ${window.reset_human}` : window.detail ? ` · ${window.detail}` : ''
      return `  ${window.label || 'Window'}: ${left == null ? 'unavailable' : `${left}% remaining (${used}% used)`}${reset}`
    })
    for (const detail of account.details ?? []) lines.push(`  ${detail}`)
    if (account.unavailable_reason) lines.push(`  Unavailable: ${account.unavailable_reason}`)
    return [heading, ...lines]
  })
}
