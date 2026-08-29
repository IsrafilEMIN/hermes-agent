import { describe, expect, it } from 'vitest'

import { formatQuotaChip } from '../domain/usage.js'
import type { UsageQuotaAccount } from '../types.js'

describe('formatQuotaChip', () => {
  it('formats opencode-go accounts with 5h/7d/monthly windows', () => {
    const accounts: UsageQuotaAccount[] = [
      { provider: 'opencode-go', active: true, five_hour: 25, seven_day: 60, monthly: 35 },
      { provider: 'opencode-go', active: false, five_hour: 50, seven_day: 90, monthly: 100 }
    ]

    expect(formatQuotaChip(accounts)).toBe('GO ● 75/40/65 ○ 50/10/0')
  })

  it('maps xai-oauth to GROK with 5h/7d windows', () => {
    expect(formatQuotaChip([{ provider: 'xai-oauth', active: true, five_hour: 10, seven_day: 20 }])).toBe(
      'GROK ● 90/80'
    )
  })

  it('maps cursor to CURSOR with monthly and monthly_other windows', () => {
    expect(formatQuotaChip([{ provider: 'cursor', active: true, monthly: 6, monthly_other: 5 }])).toBe(
      'CURSOR ● 94/95'
    )
  })

  it('maps openai-codex to GPT with 5h/7d windows', () => {
    expect(formatQuotaChip([{ provider: 'openai-codex', active: true, five_hour: 24, seven_day: 8 }])).toBe(
      'GPT ● 76/92'
    )
  })

  it('renders backup accounts with missing windows as ?', () => {
    const accounts: UsageQuotaAccount[] = [
      { provider: 'openai', active: true, five_hour: 24, seven_day: 8 },
      { provider: 'openai', active: false, five_hour: 66 }
    ]

    expect(formatQuotaChip(accounts)).toBe('GPT ● 76/92 ○ 34/?')
  })

  it('returns an empty string for empty or null quota', () => {
    expect(formatQuotaChip(null)).toBe('')
    expect(formatQuotaChip(undefined)).toBe('')
    expect(formatQuotaChip([])).toBe('')
  })

  it('keeps unknown providers as raw ids', () => {
    expect(formatQuotaChip([{ provider: 'anthropic', active: true, five_hour: 24 }])).toBe('anthropic ● 76/?')
  })

  it('clamps remaining to 0-100', () => {
    expect(formatQuotaChip([{ provider: 'openai', active: true, five_hour: 110, seven_day: -5 }])).toBe(
      'GPT ● 0/100'
    )
  })
})
