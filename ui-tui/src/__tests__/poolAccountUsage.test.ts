import { describe, expect, it } from 'vitest'

import { codexUsageDetails, formatCodexUsage } from '../domain/usage.js'
import type { CodexUsageAccount } from '../types.js'

const accounts: CodexUsageAccount[] = [
  {
    active: true,
    available: true,
    label: 'Codex 1',
    provider: 'openai-codex',
    windows: [
      { label: 'Session', used_percent: 13 },
      { label: 'Weekly', used_percent: 36 }
    ]
  },
  {
    active: false,
    available: true,
    label: 'Codex 2',
    provider: 'openai-codex',
    windows: [
      { label: 'Session', used_percent: 58 },
      { label: 'Weekly', used_percent: 9 }
    ]
  }
]

describe('pool-aware Codex usage formatting', () => {
  it('renders wide, medium, and narrow layouts from remaining percentages', () => {
    expect(formatCodexUsage(accounts, 120)).toBe('● S87 W64 │ ○ S42 W91')
    expect(formatCodexUsage(accounts, 80)).toBe('● 87/64 ○ 42/91')
    expect(formatCodexUsage(accounts, 60)).toBe('Codex min 42% · 2')
  })

  it('drops C1/C2 identifiers from status output but keeps them in the detail panel', () => {
    const wide = formatCodexUsage(accounts, 120)
    const medium = formatCodexUsage(accounts, 80)

    expect(wide).not.toMatch(/C[12]/)
    expect(medium).not.toMatch(/C[12]/)
    expect(wide).toBe('● S87 W64 │ ○ S42 W91')
    expect(medium).toBe('● 87/64 ○ 42/91')

    const details = codexUsageDetails(accounts).join('\n')
    expect(details).toContain('● Codex 1 (active)')
    expect(details).toContain('○ Codex 2')
  })

  it('marks exhausted and unavailable accounts without hiding peers', () => {
    const mixed: CodexUsageAccount[] = [
      { ...accounts[0], windows: [{ label: 'Session', used_percent: 100 }] },
      {
        active: false,
        available: false,
        label: 'Codex 2',
        provider: 'openai-codex',
        unavailable_reason: 'The stored OAuth credential was rejected.',
        windows: []
      }
    ]
    expect(formatCodexUsage(mixed, 120)).toBe('! S0 W? │ ? S? W?')
    expect(formatCodexUsage(mixed, 80)).toBe('! 0/? ? ?/?')
    expect(codexUsageDetails(mixed).join('\n')).toContain('? Codex 2')
    expect(codexUsageDetails(mixed).join('\n')).toContain('stored OAuth credential was rejected')
  })

  it('clamps out-of-range percentages in detailed output', () => {
    const overLimit: CodexUsageAccount[] = [
      { ...accounts[0], windows: [{ label: 'Session', used_percent: 130 }] }
    ]

    expect(codexUsageDetails(overLimit)).toContain('  Session: 0% remaining (100% used)')
  })
})
