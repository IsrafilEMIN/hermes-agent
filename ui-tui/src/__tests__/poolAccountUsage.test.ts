import { describe, expect, it } from 'vitest'

import {
  codexUsageDetails,
  formatCodexUsage,
  formatOpenCodeGoUsage,
  openCodeGoUsageDetails
} from '../domain/usage.js'
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
    expect(formatCodexUsage(accounts, 120)).toBe('GPT ● 87/64 ○ 42/91')
    expect(formatCodexUsage(accounts, 80)).toBe('GPT ● 87/64 ○ 42/91')
    expect(formatCodexUsage(accounts, 60)).toBe('GPT ●○ min 42% · 2')
  })

  it('uses the medium circle-only form at every non-narrow width, dropping S/W labels and the wide separator', () => {
    const wide = formatCodexUsage(accounts, 120)
    const medium = formatCodexUsage(accounts, 80)

    expect(wide).toBe('GPT ● 87/64 ○ 42/91')
    expect(medium).toBe('GPT ● 87/64 ○ 42/91')
    expect(wide).not.toMatch(/S\d+ W\d+/)
    expect(medium).not.toMatch(/S\d+ W\d+/)
    expect(wide).not.toContain('│')
    expect(medium).not.toContain('│')
    expect(wide).not.toMatch(/C[12]/)
    expect(medium).not.toMatch(/C[12]/)

    const details = codexUsageDetails(accounts).join('\n')
    expect(details).toContain('● Codex 1 (active)')
    expect(details).toContain('○ Codex 2')
  })

  it('persists active/inactive circles for exhausted and unavailable accounts without hiding peers', () => {
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
    // The circle is persistent: exhaustion shows up in the 0 slot, unavailability
    // in the ? slots — never by swapping the marker out.
    expect(formatCodexUsage(mixed, 120)).toBe('GPT ● 0/? ○ ?/?')
    expect(formatCodexUsage(mixed, 80)).toBe('GPT ● 0/? ○ ?/?')
    expect(formatCodexUsage(mixed, 60)).toBe('GPT ●○ min 0% · 2')
    expect(codexUsageDetails(mixed).join('\n')).toContain('○ Codex 2')
    expect(codexUsageDetails(mixed).join('\n')).toContain('stored OAuth credential was rejected')
  })

  it('keeps the account circle for a single Codex account (active ● and inactive ○)', () => {
    const single = [accounts[0]]
    const inactiveSingle = [{ ...accounts[1] }]

    expect(formatCodexUsage(single, 120)).toBe('GPT ● 87/64')
    expect(formatCodexUsage(single, 80)).toBe('GPT ● 87/64')
    expect(formatCodexUsage(single, 60)).toBe('GPT ● min 64% · 1')
    expect(formatCodexUsage(inactiveSingle, 120)).toBe('GPT ○ 42/91')
    expect(formatCodexUsage(inactiveSingle, 80)).toBe('GPT ○ 42/91')
    expect(formatCodexUsage(inactiveSingle, 60)).toBe('GPT ○ min 42% · 1')
  })

  it('renders the GPT prefix with ? placeholders for missing windows', () => {
    const partial: CodexUsageAccount[] = [
      { ...accounts[0], windows: [{ label: 'Weekly', used_percent: 62 }] },
      { ...accounts[1], windows: [{ label: 'Weekly', used_percent: 99 }] }
    ]

    expect(formatCodexUsage(partial, 120)).toBe('GPT ● ?/38 ○ ?/1')
  })

  it('clamps out-of-range percentages in detailed output', () => {
    const overLimit: CodexUsageAccount[] = [
      { ...accounts[0], windows: [{ label: 'Session', used_percent: 130 }] }
    ]

    expect(codexUsageDetails(overLimit)).toContain('  Session: 0% remaining (100% used)')
  })

  it('renders no Codex details for a Go-only or empty payload', () => {
    const goOnly: CodexUsageAccount[] = [
      {
        active: true,
        available: false,
        label: 'Go 1',
        provider: 'opencode-go',
        unavailable_reason: 'The stored OpenCode Go API key was rejected.',
        windows: []
      }
    ]

    expect(codexUsageDetails(undefined)).toEqual([])
    expect(codexUsageDetails([])).toEqual([])
    expect(codexUsageDetails(goOnly)).toEqual([])
  })

  it('keeps OpenCode Go out of Codex details; legacy missing-provider accounts still render', () => {
    const mixed: CodexUsageAccount[] = [
      { ...accounts[0] }, // openai-codex
      {
        active: false,
        available: true,
        label: 'Legacy',
        provider: null,
        windows: [{ label: 'Session', used_percent: 20 }]
      },
      {
        active: true,
        available: true,
        label: 'Go 1',
        provider: 'opencode-go',
        windows: [{ label: 'Rolling 5h', used_percent: 10 }]
      }
    ]

    const codexDetails = codexUsageDetails(mixed)
    expect(codexDetails).toContain('● Codex 1 (active)')
    expect(codexDetails).toContain('○ Legacy')
    expect(codexDetails.join('\n')).not.toContain('Go 1')
    // Each Codex/legacy account appears exactly once.
    expect(codexDetails.filter(line => line.includes('Codex 1'))).toHaveLength(1)
    expect(codexDetails.filter(line => line.includes('Legacy'))).toHaveLength(1)

    const goDetails = openCodeGoUsageDetails(mixed)
    expect(goDetails.filter(line => line === '● OpenCode Go (active)')).toHaveLength(1)
    expect(goDetails.join('\n')).not.toContain('Codex 1')
  })

  it('numbers Codex accounts by ordinal after filtering out other providers', () => {
    const mixed: CodexUsageAccount[] = [
      { active: true, available: true, provider: 'opencode-go', windows: [] },
      { active: true, available: true, label: null, provider: 'openai-codex', windows: [] },
      { active: true, available: true, label: null, provider: 'openai-codex', windows: [] }
    ]

    const details = codexUsageDetails(mixed).join('\n')
    expect(details).toContain('● Codex 1 (active)')
    expect(details).toContain('● Codex 2 (active)')
    expect(details).not.toContain('Codex 3')
  })
})

describe('OpenCode Go (env-backed) usage formatting', () => {
  // The gateway never sends credential identifiers; even if one leaked into
  // the label it must never surface in the status read-out or details.
  const goAccounts: CodexUsageAccount[] = [
    {
      active: true,
      available: true,
      label: 'opencode-go-key-9f3c',
      provider: 'opencode-go',
      windows: [
        { label: 'Rolling 5h', used_percent: 10, reset_human: 'in 2h' },
        { label: 'Weekly', used_percent: 41 },
        { label: 'Monthly', used_percent: 47 }
      ]
    }
  ]

  it('renders the full Go circle + rolling/weekly/monthly remaining read-out', () => {
    expect(formatOpenCodeGoUsage(goAccounts)).toBe('Go ● 90/59/53')
  })

  it('renders ? for missing windows instead of dropping the segment', () => {
    const partial: CodexUsageAccount[] = [
      { ...goAccounts[0], windows: [{ label: 'Weekly', used_percent: 41 }] }
    ]

    expect(formatOpenCodeGoUsage(partial)).toBe('Go ● ?/59/?')
  })

  it('keeps the persistent circle on the single env-backed account (○ when inactive)', () => {
    const inactive: CodexUsageAccount[] = [{ ...goAccounts[0], active: false }]

    expect(formatOpenCodeGoUsage(inactive)).toBe('Go ○ 90/59/53')
  })

  it('shows the active row when the backend emits an inactive Go account before the active one', () => {
    const outOfOrder: CodexUsageAccount[] = [
      {
        ...goAccounts[0],
        active: false,
        windows: [
          { label: 'Rolling 5h', used_percent: 60 },
          { label: 'Weekly', used_percent: 90 },
          { label: 'Monthly', used_percent: 95 }
        ]
      },
      { ...goAccounts[0] } // active, 90/59/53 remaining
    ]

    // Regression: visible[0] would read the inactive row (`Go ○ 40/10/5`);
    // the status must reflect the active row's marker and values.
    expect(formatOpenCodeGoUsage(outOfOrder)).toBe('Go ● 90/59/53')
  })

  it('renders Go ● ?/?/? for an unavailable account', () => {
    const unavailable: CodexUsageAccount[] = [
      {
        active: true,
        available: false,
        label: 'opencode-go-key-9f3c',
        provider: 'opencode-go',
        unavailable_reason: 'The stored OpenCode Go API key was rejected.',
        windows: []
      }
    ]

    expect(formatOpenCodeGoUsage(unavailable)).toBe('Go ● ?/?/?')
  })

  it('hides the segment entirely when no Go account is present', () => {
    expect(formatOpenCodeGoUsage(undefined)).toBe('')
    expect(formatOpenCodeGoUsage([])).toBe('')
    expect(formatOpenCodeGoUsage(accounts)).toBe('') // Codex-only payload
  })

  it('keeps the constant OpenCode Go details heading with remaining/used/reset lines', () => {
    const details = openCodeGoUsageDetails(goAccounts).join('\n')

    expect(details.split('\n')[0]).toBe('● OpenCode Go (active)')
    expect(details).toContain('OpenCode Go')
    expect(details).toContain('  Rolling 5h: 90% remaining (10% used) · resets in 2h')
    expect(details).toContain('  Weekly: 59% remaining (41% used)')
    expect(details).toContain('  Monthly: 53% remaining (47% used)')
  })

  it('drops to the ○ marker and no (active) suffix for an inactive Go account in details', () => {
    const inactive: CodexUsageAccount[] = [{ ...goAccounts[0], active: false }]

    expect(openCodeGoUsageDetails(inactive).join('\n').split('\n')[0]).toBe('○ OpenCode Go')
  })

  it('never surfaces the account label or credential identifiers in details', () => {
    const details = openCodeGoUsageDetails(goAccounts).join('\n')

    expect(details).not.toContain('opencode-go-key-9f3c')
    expect(details).not.toContain('9f3c')
    // The heading is the persistent circle + constant safe label, never
    // account.label or an index.
    expect(details.split('\n')[0]).toBe('● OpenCode Go (active)')
  })

  it('clamps out-of-range percentages and reports unavailable windows in details', () => {
    const overLimit: CodexUsageAccount[] = [
      { ...goAccounts[0], windows: [{ label: 'Rolling 5h', used_percent: 130 }] }
    ]

    expect(openCodeGoUsageDetails(overLimit)).toContain('  Rolling 5h: 0% remaining (100% used)')

    const unavailable: CodexUsageAccount[] = [
      {
        active: true,
        available: false,
        label: 'opencode-go-key-9f3c',
        provider: 'opencode-go',
        unavailable_reason: 'The stored OpenCode Go API key was rejected.',
        windows: []
      }
    ]

    expect(openCodeGoUsageDetails(unavailable).join('\n')).toContain(
      '  Unavailable: The stored OpenCode Go API key was rejected.'
    )
  })
})
