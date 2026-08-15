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
    expect(formatCodexUsage(accounts, 120)).toBe('●C1 S87 W64 │ ○C2 S42 W91')
    expect(formatCodexUsage(accounts, 80)).toBe('●C1 87/64 ○C2 42/91')
    expect(formatCodexUsage(accounts, 60)).toBe('Codex min 42% · 2')
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
    expect(formatCodexUsage(mixed, 120)).toBe('!C1 S0 W? │ ?C2 S? W?')
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
