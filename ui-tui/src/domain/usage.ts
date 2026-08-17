import type { CodexUsageAccount, CodexUsageWindow, Usage } from '../types.js'

export const ZERO: Usage = { calls: 0, input: 0, output: 0, total: 0 }

const remaining = (window?: CodexUsageWindow) =>
  typeof window?.used_percent === 'number' ? Math.max(0, Math.round(100 - window.used_percent)) : null

const windowFor = (account: CodexUsageAccount, kind: 'session' | 'weekly') =>
  (account.windows ?? []).find(window => {
    const label = String(window.label ?? '').toLowerCase()
    return kind === 'session' ? label.includes('session') : label.includes('week')
  })

// The circle is persistent: every account gets exactly one marker, active or
// not, regardless of how many accounts exist. `●` = active (full), `○` =
// inactive (empty). Exhaustion/unavailability is conveyed by the percentage
// slots (`0`, `?`) and the details panel, never by swapping the marker out.
const accountMarker = (account: CodexUsageAccount) => (account.active ? '●' : '○')

export function formatCodexUsage(accounts: CodexUsageAccount[] | undefined, cols: number): string {
  const visible = (accounts ?? []).filter(account => account.provider === 'openai-codex' || !account.provider)
  if (!visible.length) return ''

  if (cols < 72) {
    const values = visible.flatMap(account =>
      [remaining(windowFor(account, 'session')), remaining(windowFor(account, 'weekly'))].filter(
        (value): value is number => value != null
      )
    )
    // Circles persist here too: exactly one compact marker per visible
    // account (● active / ○ inactive), then min/count as before.
    const markers = visible.map(accountMarker).join('')
    return `GPT ${markers} min ${values.length ? `${Math.min(...values)}%` : '?'} · ${visible.length}`
  }

  return `GPT ${visible
    .map(account => {
      const session = remaining(windowFor(account, 'session'))
      const weekly = remaining(windowFor(account, 'weekly'))
      const marker = accountMarker(account)
      return `${marker} ${session ?? '?'}/${weekly ?? '?'}`
    })
    .join(' ')}`
}

export function codexUsageDetails(accounts: CodexUsageAccount[] | undefined): string[] {
  // Only Codex pool accounts (or legacy accounts predating the provider field)
  // belong in this panel — other providers (e.g. opencode-go) must never be
  // rendered as a bogus "Codex N" entry. Mirror formatCodexUsage's filter so
  // index stays a Codex ordinal after filtering.
  const visible = (accounts ?? []).filter(account => account.provider === 'openai-codex' || !account.provider)
  return visible.flatMap((account, index) => {
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

// ── OpenCode Go (env-backed and/or pool rows, read-only) ───────────────────

const openCodeGoWindow = (account: CodexUsageAccount | undefined, kind: 'rolling' | 'weekly' | 'monthly') =>
  (account?.windows ?? []).find(window => {
    const label = String(window.label ?? '').toLowerCase()
    return kind === 'rolling' ? label.includes('rolling') : label.includes(kind)
  })

/**
 * Compact status segment for the OpenCode Go account(s):
 * `Go ● <rolling>/<weekly>/<monthly> ○ <rolling>/<weekly>/<monthly>`
 * remaining percentages — one persistent circle per account (`●` active /
 * `○` inactive), `?` per missing window. Renders every Go account exactly
 * like the Codex slot (active row first, then inactive rows in payload
 * order), so a multi-credential pool shows the same ●/○ shape the Codex
 * segment does. Never exposes account labels or credential-derived
 * identifiers.
 */
export function formatOpenCodeGoUsage(accounts: CodexUsageAccount[] | undefined): string {
  const visible = (accounts ?? []).filter(account => account.provider === 'opencode-go')
  if (!visible.length) return ''
  // The pool can emit the inactive account before the active one; the status
  // segment must always lead with the active row when one exists, keeping
  // payload order for the rest (stable sort — inactive rows stay in order).
  const ordered = [...visible].sort((a, b) => Number(b.active ?? false) - Number(a.active ?? false))
  const fmt = (value: number | null) => (value == null ? '?' : String(value))
  const one = (account: CodexUsageAccount) =>
    `${accountMarker(account)} ${fmt(remaining(openCodeGoWindow(account, 'rolling')))}/${fmt(remaining(openCodeGoWindow(account, 'weekly')))}/${fmt(remaining(openCodeGoWindow(account, 'monthly')))}`
  return `Go ${ordered.map(one).join(' ')}`
}

/** Detailed /usage block: `OpenCode Go` heading + rolling/weekly/monthly lines. */
export function openCodeGoUsageDetails(accounts: CodexUsageAccount[] | undefined): string[] {
  return (accounts ?? []).flatMap(account => {
    if (account.provider !== 'opencode-go') return []
    const marker = accountMarker(account)
    const lines = (account.windows ?? []).map(window => {
      const left = remaining(window)
      const used =
        typeof window.used_percent === 'number' ? Math.max(0, Math.min(100, Math.round(window.used_percent))) : null
      const reset = window.reset_human ? ` · resets ${window.reset_human}` : window.detail ? ` · ${window.detail}` : ''
      return `  ${window.label || 'Window'}: ${left == null ? 'unavailable' : `${left}% remaining (${used}% used)`}${reset}`
    })
    for (const detail of account.details ?? []) lines.push(`  ${detail}`)
    if (account.unavailable_reason) lines.push(`  Unavailable: ${account.unavailable_reason}`)
    return [`${marker} OpenCode Go${account.active ? ' (active)' : ''}`, ...lines]
  })
}
