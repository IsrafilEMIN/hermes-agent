import { beforeEach, describe, expect, it, vi } from 'vitest'

import { sessionCommands } from '../app/slash/commands/session.js'
import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'
import type { SessionUsageResponse } from '../gatewayTypes.js'

const usageCommand = sessionCommands.find(cmd => cmd.name === 'usage')!

const USAGE_CTA = 'Run /subscription to change plan · /topup to add to your balance'

const guarded =
  <T>(fn: (r: T) => void) =>
  (r: null | T) => {
    if (r) {
      fn(r)
    }
  }

/** Build a ctx whose rpc routes by method name to a supplied map of results. */
const buildCtx = (results: Record<string, unknown>) => {
  const sys = vi.fn()
  const panel = vi.fn()

  const rpc = vi.fn((method: string, _params: unknown) => Promise.resolve(results[method]))

  const ctx = {
    gateway: { rpc },
    guarded,
    guardedErr: vi.fn(),
    sid: 'sid-1',
    stale: () => false,
    transcript: { page: vi.fn(), panel, sys }
  }

  const run = async (arg: string) => {
    usageCommand.run(arg, ctx as any, 'usage')
    await rpc.mock.results[0]?.value
    await Promise.resolve()
    await Promise.resolve()
  }

  return { ctx, panel, run, sys }
}

const baseUsage = (overrides: Partial<SessionUsageResponse> = {}): SessionUsageResponse =>
  ({ calls: 0, input: 0, output: 0, total: 0, ...overrides }) as SessionUsageResponse

const printed = (sys: ReturnType<typeof vi.fn>) => sys.mock.calls.map(c => c[0]).join('\n')

const balancePanel = (panel: ReturnType<typeof vi.fn>) => {
  const sections = panel.mock.calls.find(c => c[0] === 'Balance')?.[1] as { text?: string }[] | undefined

  return (sections ?? []).map(s => s.text ?? '').join('\n')
}

describe('/usage slash command', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    resetUiState()
  })

  it('always shows the CTA; "no API calls yet" only when there is no balance', async () => {
    const empty = buildCtx({ 'session.usage': baseUsage({ calls: 0, credits_lines: [] }) })
    await empty.run('')
    expect(printed(empty.sys)).toContain('no API calls yet')
    expect(printed(empty.sys)).toContain(USAGE_CTA)

    const withBalance = buildCtx({ 'session.usage': baseUsage({ calls: 0, credits_lines: ['$50.00 remaining'] }) })
    await withBalance.run('')
    expect(printed(withBalance.sys)).not.toContain('no API calls yet')
    expect(printed(withBalance.sys)).toContain(USAGE_CTA)
  })

  it('renders the dollar two-bar model (no "credits" wording) when available', async () => {
    const { panel, run } = buildCtx({
      'session.usage': baseUsage({
        usage: {
          available: true,
          status: 'healthy',
          plan_name: 'Plus',
          renews_display: 'Jul 1, 2026',
          total_spendable_display: '$26.00',
          has_topup: true,
          plan_bar: {
            kind: 'plan',
            remaining_display: '$14.00',
            total_display: '$20.00',
            spent_display: '$6.00',
            pct_used: 30,
            fill_fraction: 0.7
          },
          topup_bar: {
            kind: 'topup',
            remaining_display: '$12.00',
            total_display: '$12.00',
            spent_display: '$0.00',
            pct_used: null,
            fill_fraction: 1
          }
        }
      })
    })

    await run('')

    const body = balancePanel(panel)
    expect(body).toContain('Plus')
    expect(body).toContain('$14.00 left of $20.00')
    expect(body).toContain('30% used')
    expect(body).toContain('top-up')
    expect(body).toContain('$12.00')
    expect(body.toLowerCase()).not.toContain('credits')
  })

  it('shows the free-models upsell for a free account', async () => {
    const { panel, run } = buildCtx({
      'session.usage': baseUsage({ usage: { available: true, status: 'free', plan_name: null } })
    })

    await run('')

    const body = balancePanel(panel)
    expect(body).toContain('free models only')
    expect(body).toContain('/subscription')
  })

  it('renders both Codex pool accounts without suppressing token usage', async () => {
    patchUiState(state => ({
      ...state,
      usage: {
        ...state.usage,
        active_subagents: 2,
        compressions: 1,
        context_max: 200_000,
        context_percent: 25,
        context_used: 50_000,
        cost_usd: 0.5
      }
    }))
    const { panel, run } = buildCtx({
      'session.usage': baseUsage({
        calls: 1,
        accounts: [
          {
            active: true,
            available: true,
            label: 'Codex 1',
            provider: 'openai-codex',
            windows: [{ label: 'Session', used_percent: 13, reset_human: 'in 2h' }]
          },
          {
            active: false,
            available: false,
            label: 'Codex 2',
            provider: 'openai-codex',
            unavailable_reason: 'The stored OAuth credential was rejected.',
            windows: []
          }
        ]
      })
    })

    await run('')

    const sections = panel.mock.calls.find(c => c[0] === 'Codex limits')?.[1] as { text?: string }[]
    const body = sections.map(section => section.text ?? '').join('\n')
    expect(body).toContain('● Codex 1 (active)')
    expect(body).toContain('Session: 87% remaining (13% used) · resets in 2h')
    expect(body).toContain('○ Codex 2')
    expect(body).toContain('stored OAuth credential was rejected')
    expect(panel.mock.calls.some(c => c[0] === 'Usage')).toBe(true)
    expect(getUiState().usage).toMatchObject({
      active_subagents: 2,
      compressions: 1,
      context_max: 200_000,
      context_percent: 25,
      context_used: 50_000,
      cost_usd: 0.5
    })
  })

  it('adds the OpenCode Go limits panel alongside Codex without clobbering state', async () => {
    patchUiState(state => ({
      ...state,
      usage: {
        ...state.usage,
        active_subagents: 2,
        compressions: 1,
        context_max: 200_000,
        context_percent: 25,
        context_used: 50_000,
        cost_usd: 0.5
      }
    }))
    const { panel, run } = buildCtx({
      'session.usage': baseUsage({
        calls: 1,
        accounts: [
          {
            active: true,
            available: true,
            label: 'Codex 1',
            provider: 'openai-codex',
            windows: [{ label: 'Session', used_percent: 13, reset_human: 'in 2h' }]
          },
          {
            active: true,
            available: true,
            label: 'Go 1',
            provider: 'opencode-go',
            windows: [
              { label: 'Rolling 5h', used_percent: 10, reset_human: 'in 2h' },
              { label: 'Weekly', used_percent: 41 },
              { label: 'Monthly', used_percent: 47 }
            ]
          }
        ]
      })
    })

    await run('')

    const codexSections = panel.mock.calls.find(c => c[0] === 'Codex limits')?.[1] as { text?: string }[]
    const codexBody = (codexSections ?? []).map(s => s.text ?? '').join('\n')
    expect(codexBody).toContain('● Codex 1 (active)')
    // The Go account must never leak into the Codex panel as a bogus entry.
    expect(codexBody).not.toContain('Go 1')

    const goSections = panel.mock.calls.find(c => c[0] === 'OpenCode Go limits')?.[1] as { text?: string }[]
    const goBody = (goSections ?? []).map(s => s.text ?? '').join('\n')
    expect(goBody).toContain('OpenCode Go')
    expect(goBody).toContain('  Rolling 5h: 90% remaining (10% used) · resets in 2h')
    expect(goBody).toContain('  Weekly: 59% remaining (41% used)')
    expect(goBody).toContain('  Monthly: 53% remaining (47% used)')
    expect(goBody).not.toContain('Codex 1')

    // Additive: both provider panels coexist exactly once, and the rpc response
    // merges into (never replaces) pre-existing token-usage state.
    expect(panel.mock.calls.filter(c => c[0] === 'Codex limits')).toHaveLength(1)
    expect(panel.mock.calls.filter(c => c[0] === 'OpenCode Go limits')).toHaveLength(1)
    expect(getUiState().usage).toMatchObject({
      active_subagents: 2,
      compressions: 1,
      context_max: 200_000,
      context_percent: 25,
      context_used: 50_000,
      cost_usd: 0.5,
      calls: 1
    })
    expect(getUiState().usage.accounts?.filter(a => a.provider === 'opencode-go')).toHaveLength(1)
  })

  it('shows only the OpenCode Go limits panel for a Go-only payload (no Codex panel)', async () => {
    const { panel, run } = buildCtx({
      'session.usage': baseUsage({
        accounts: [
          {
            active: true,
            available: false,
            label: 'Go 1',
            provider: 'opencode-go',
            unavailable_reason: 'The stored OpenCode Go API key was rejected.',
            windows: []
          }
        ]
      })
    })

    await run('')

    const goSections = panel.mock.calls.find(c => c[0] === 'OpenCode Go limits')?.[1] as { text?: string }[]
    expect((goSections ?? []).map(s => s.text ?? '').join('\n')).toContain(
      'Unavailable: The stored OpenCode Go API key was rejected.'
    )
    // The gateway gates accounts to the current provider, so a Go-only payload
    // must not fabricate a Codex limits panel — and the Go panel appears once.
    expect(panel.mock.calls.filter(c => c[0] === 'Codex limits')).toHaveLength(0)
    expect(panel.mock.calls.filter(c => c[0] === 'OpenCode Go limits')).toHaveLength(1)
  })
})
