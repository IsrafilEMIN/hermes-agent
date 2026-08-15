import React from 'react'
import { describe, expect, it, vi } from 'vitest'

import { StatusRule } from '../components/appChrome.js'
import { DEFAULT_THEME } from '../theme.js'

type ReactNodeLike = React.ReactNode

const textContent = (node: ReactNodeLike): string => {
  if (node === null || node === undefined || typeof node === 'boolean') {
    return ''
  }

  if (typeof node === 'string' || typeof node === 'number') {
    return String(node)
  }

  if (Array.isArray(node)) {
    return node.map(textContent).join('')
  }

  if (React.isValidElement(node)) {
    return textContent(node.props.children)
  }

  return ''
}

const findClickableWithText = (node: ReactNodeLike, needle: string): React.ReactElement | null => {
  if (node === null || node === undefined || typeof node === 'boolean') {
    return null
  }

  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findClickableWithText(child, needle)

      if (found) {
        return found
      }
    }

    return null
  }

  if (!React.isValidElement(node)) {
    return null
  }

  if (typeof node.props.onClick === 'function' && textContent(node).includes(needle)) {
    return node
  }

  return findClickableWithText(node.props.children, needle)
}

// Find the innermost element whose own (direct) text content includes the
// needle. Used to assert the colour the notice text is rendered with.
const findElementWithText = (node: ReactNodeLike, needle: string): React.ReactElement | null => {
  if (node === null || node === undefined || typeof node === 'boolean') {
    return null
  }

  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findElementWithText(child, needle)

      if (found) {
        return found
      }
    }

    return null
  }

  if (!React.isValidElement(node)) {
    return null
  }

  // Prefer the deepest matching element so we get the leaf <Text> that
  // actually carries the colour, not an ancestor Box.
  const deeper = findElementWithText(node.props.children, needle)

  if (deeper) {
    return deeper
  }

  return textContent(node).includes(needle) ? node : null
}

const baseProps = {
  bgCount: 0,
  busy: false,
  cols: 100,
  cwdLabel: '~/repo',
  liveSessionCount: 0,
  model: 'opus-4.8',
  sessionStartedAt: null,
  status: 'ready',
  statusColor: DEFAULT_THEME.color.ok,
  t: DEFAULT_THEME,
  turnStartedAt: null,
  usage: { context_max: 200_000, context_percent: 25, context_used: 50_000, total: 50_000 },
  voiceLabel: ''
}

describe('StatusRule session title', () => {
  it('pins the named session at the far-right edge instead of the cwd label', () => {
    const element = StatusRule({
      ...baseProps,
      sessionTitle: 'weekly-digest'
    })

    const rendered = textContent(element)
    const title = findElementWithText(element, 'weekly-digest')

    expect(rendered).toContain('weekly-digest')
    expect(rendered).not.toContain('~/repo')
    // Regression for issue #82465: a raw, full-saturation accent-hue
    // background (e.g. #FFBF00 on DARK_SEEDS) paired with statusFg (a
    // near-white tone never designed to sit on it) rendered at roughly a
    // 1.5-2:1 contrast ratio -- unreadable. No background fill at all;
    // the accent color goes on the text instead, matching the theme's
    // own convention that a raw accent hue is never used as a solid
    // fill elsewhere (fills are always softened, e.g. activeRow).
    expect(title?.props.backgroundColor).toBeUndefined()
    expect(title?.props.color).toBe(DEFAULT_THEME.color.accent)
  })

  it('shows the session-title badge by default when the flag is absent (backward compat)', () => {
    const element = StatusRule({
      ...baseProps,
      sessionTitle: 'weekly-digest'
    })

    const rendered = textContent(element)
    const title = findElementWithText(element, 'weekly-digest')

    expect(rendered).toContain('weekly-digest')
    expect(rendered).not.toContain('~/repo')
    expect(title?.props.color).toBe(DEFAULT_THEME.color.accent)
    expect(title?.props.bold).toBe(true)
  })

  it('keeps the cwd/workspace label in ordinary label styling when showSessionTitle is off', () => {
    const element = StatusRule({
      ...baseProps,
      sessionTitle: 'weekly-digest',
      showSessionTitle: false
    })

    const rendered = textContent(element)
    const cwd = findElementWithText(element, '~/repo')

    expect(rendered).toContain('~/repo')
    expect(rendered).not.toContain('weekly-digest')
    // Plain label styling — no accent badge treatment.
    expect(cwd?.props.color).toBe(DEFAULT_THEME.color.label)
    expect(cwd?.props.bold).not.toBe(true)
  })
})

describe('StatusRule background-subagent indicator', () => {
  it('renders ⛓ N on a wide terminal when subagents are running', () => {
    const element = StatusRule({
      ...baseProps,
      usage: { ...baseProps.usage, active_subagents: 3 }
    })

    expect(textContent(element)).toContain('⛓ 3')
  })

  it('omits the segment when no subagents are running', () => {
    const element = StatusRule({
      ...baseProps,
      usage: { ...baseProps.usage, active_subagents: 0 }
    })

    expect(textContent(element)).not.toContain('⛓')
  })

  it('omits the segment when the field is absent', () => {
    const element = StatusRule({ ...baseProps })

    expect(textContent(element)).not.toContain('⛓')
  })

  it('spells out the auto-resume hint when idle with subagents in flight', () => {
    const element = StatusRule({
      ...baseProps,
      usage: { ...baseProps.usage, active_subagents: 1 }
    })

    expect(textContent(element)).toContain('resumes when subagent finishes')
  })

  it('pluralizes the resume hint for multiple in-flight subagents', () => {
    const element = StatusRule({
      ...baseProps,
      usage: { ...baseProps.usage, active_subagents: 3 }
    })

    expect(textContent(element)).toContain('resumes when 3 subagents finish')
  })

  it('hides the resume hint mid-turn (a busy turn owns the indicator)', () => {
    const element = StatusRule({
      ...baseProps,
      busy: true,
      turnStartedAt: Date.now(),
      usage: { ...baseProps.usage, active_subagents: 2 }
    })

    expect(textContent(element)).not.toContain('resumes when')
  })

  it('omits the resume hint when no subagents are running', () => {
    const element = StatusRule({ ...baseProps })

    expect(textContent(element)).not.toContain('resumes when')
  })

  it('drops the subagent segment before the bg segment on a narrow terminal', () => {
    // cols=44 is below the subagents breakpoint (92) but the bg breakpoint
    // (88) too — both gone. Assert the lower-priority subagent indicator is
    // not shown when space is tight even with a live count.
    const element = StatusRule({
      ...baseProps,
      cols: 44,
      bgCount: 1,
      usage: { ...baseProps.usage, active_subagents: 2 }
    })

    expect(textContent(element)).not.toContain('⛓')
  })
})

describe('StatusRule session count click target', () => {
  it('makes the live session count itself clickable', () => {
    const openSwitcher = vi.fn()

    const element = StatusRule({
      bgCount: 0,
      busy: false,
      cols: 100,
      cwdLabel: '~/repo',
      liveSessionCount: 1,
      model: 'kimi-k2.6',
      onSessionCountClick: openSwitcher,
      sessionStartedAt: null,
      status: 'ready',
      statusColor: DEFAULT_THEME.color.ok,
      t: DEFAULT_THEME,
      turnStartedAt: null,
      usage: { total: 0 },
      voiceLabel: ''
    })

    const clickableSessionCount = findClickableWithText(element, '1 session')

    expect(clickableSessionCount).not.toBeNull()
    clickableSessionCount!.props.onClick({ stopImmediatePropagation: vi.fn() })
    expect(openSwitcher).toHaveBeenCalledOnce()
  })

  it('keeps status + model and drops the low-value tail on a narrow terminal', () => {
    const element = StatusRule({
      bgCount: 0,
      busy: false,
      cols: 44,
      cwdLabel: '~/src/hermes-agent/apps/desktop (bb/tui-statusbar-responsive)',
      liveSessionCount: 3,
      model: 'opus-4.8',
      onSessionCountClick: vi.fn(),
      sessionStartedAt: Date.now() - 60_000,
      status: 'ready',
      statusColor: DEFAULT_THEME.color.ok,
      t: DEFAULT_THEME,
      turnStartedAt: null,
      usage: {
        calls: 0,
        context_max: 200_000,
        context_percent: 25,
        context_used: 50_000,
        input: 0,
        output: 0,
        total: 50_000
      },
      voiceLabel: 'voice off'
    })

    const rendered = textContent(element)

    // Must-keep essentials survive intact …
    expect(rendered).toContain('ready')
    expect(rendered).toContain('opus 4.8')
    // … while the low-value tail (session count) is dropped, not truncated.
    expect(rendered).not.toContain('3 sessions')
  })

  it('keeps the collapsed Codex pool summary while the cwd yields on narrow terminals', () => {
    const element = StatusRule({
      ...baseProps,
      cols: 60,
      cwdLabel: '~/src/hermes-agent/a/very/long/project/path',
      usage: {
        ...baseProps.usage,
        accounts: [
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
      }
    })

    expect(textContent(element)).toContain('GPT ●○ min 42% · 2')
  })

  it('shows active/inactive account markers without C1/C2 identifiers on wide terminals', () => {
    const element = StatusRule({
      ...baseProps,
      cols: 120,
      usage: {
        ...baseProps.usage,
        accounts: [
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
      }
    })

    const rendered = textContent(element)
    expect(rendered).toContain('GPT ● 87/64 ○ 42/91')
    expect(rendered).not.toMatch(/S\d+ W\d+/)
    // The Codex segment must not regress to the old wide form (S/W labels
    // plus an inter-account separator). Scope the rejection to the Codex
    // marker: the bar itself legitimately contains │ between model/context.
    expect(rendered).not.toMatch(/●\s*S\d+ W\d+\s*│/)
    expect(rendered).not.toMatch(/C[12]/)
  })

  it('keeps the GPT prefix and the persistent account circle with a single Codex account', () => {
    const element = StatusRule({
      ...baseProps,
      cols: 120,
      usage: {
        ...baseProps.usage,
        accounts: [
          {
            active: true,
            available: true,
            label: 'Codex 1',
            provider: 'openai-codex',
            windows: [
              { label: 'Session', used_percent: 13 },
              { label: 'Weekly', used_percent: 36 }
            ]
          }
        ]
      }
    })

    expect(textContent(element)).toContain('GPT ● 87/64')
  })

  it('uses the empty circle for a single inactive Codex account', () => {
    const element = StatusRule({
      ...baseProps,
      cols: 120,
      usage: {
        ...baseProps.usage,
        accounts: [
          {
            active: false,
            available: true,
            label: 'Codex 1',
            provider: 'openai-codex',
            windows: [
              { label: 'Session', used_percent: 58 },
              { label: 'Weekly', used_percent: 9 }
            ]
          }
        ]
      }
    })

    expect(textContent(element)).toContain('GPT ○ 42/91')
  })
})

describe('StatusRule credits notice render priority', () => {
  it('replaces the idle status with the notice text and keeps model + context', () => {
    const element = StatusRule({
      ...baseProps,
      notice: { key: 'credits.depleted', kind: 'sticky', level: 'error', text: '✕ credits exhausted' }
    })

    const rendered = textContent(element)

    // Notice replaces the status verb slot …
    expect(rendered).toContain('✕ credits exhausted')
    expect(rendered).not.toContain('ready')
    // … but model + context stay visible.
    expect(rendered).toContain('opus 4.8')
    expect(rendered).toContain('50k')
  })

  it('busy wins: the FaceTicker shows, the notice is hidden mid-turn', () => {
    const element = StatusRule({
      ...baseProps,
      busy: true,
      notice: { key: 'credits.90', kind: 'sticky', level: 'warn', text: '⚠ 90% used' },
      turnStartedAt: Date.now()
    })

    const rendered = textContent(element)

    // Notice must NOT render while busy.
    expect(rendered).not.toContain('⚠ 90% used')
    // Model still visible.
    expect(rendered).toContain('opus 4.8')
  })

  it('colours the notice by level (error → theme error, success → statusGood)', () => {
    const errEl = StatusRule({
      ...baseProps,
      notice: { key: 'credits.depleted', kind: 'sticky', level: 'error', text: '✕ exhausted' }
    })

    const errText = findElementWithText(errEl, '✕ exhausted')
    expect(errText?.props.color).toBe(DEFAULT_THEME.color.error)

    const okEl = StatusRule({
      ...baseProps,
      notice: { key: 'credits.restored', kind: 'ttl', level: 'success', text: '✓ restored', ttl_ms: 8000 }
    })

    const okText = findElementWithText(okEl, '✓ restored')
    expect(okText?.props.color).toBe(DEFAULT_THEME.color.statusGood)
  })

  it('does NOT add a glyph — the notice text is rendered verbatim', () => {
    const element = StatusRule({
      ...baseProps,
      notice: { key: 'credits.90', kind: 'sticky', level: 'warn', text: '⚠ 90% used' }
    })

    const noticeText = findElementWithText(element, '90% used')

    // The leaf carries exactly the policy text — no extra prepended glyph.
    expect(noticeText?.props.children).toBe('⚠ 90% used')
  })

  it('the notice text is the shrinkable element (flexShrink=1 + truncate-end) so a long notice ellipsizes', () => {
    const longText = '⚠ ' + 'x'.repeat(200)

    const element = StatusRule({
      ...baseProps,
      cols: 50,
      notice: { key: 'credits.90', kind: 'sticky', level: 'warn', text: longText }
    })

    // The leaf <Text> truncates rather than wrapping/clipping the pinned tail.
    const noticeText = findElementWithText(element, 'xxxxx')
    expect(noticeText?.props.wrap).toBe('truncate-end')

    // Its container box yields first (flexShrink=1) so model stays visible.
    const findShrinkBoxContaining = (node: ReactNodeLike): React.ReactElement | null => {
      if (!React.isValidElement(node)) {
        if (Array.isArray(node)) {
          for (const c of node) {
            const f = findShrinkBoxContaining(c)

            if (f) {
              return f
            }
          }
        }

        return null
      }

      if (node.props.flexShrink === 1 && textContent(node).includes('xxxxx') && node.type !== StatusRule) {
        // Prefer the closest shrink box that wraps the notice text.
        const deeper = findShrinkBoxContaining(node.props.children)

        return deeper ?? node
      }

      return findShrinkBoxContaining(node.props.children)
    }

    const shrinkBox = findShrinkBoxContaining(element)
    expect(shrinkBox).not.toBeNull()

    // Model survives on a narrow terminal because the notice yields.
    expect(textContent(element)).toContain('opus 4.8')
  })
})

describe('StatusRule battery indicator', () => {
  it('renders the battery label with a battery glyph on AC-off', () => {
    const element = StatusRule({
      ...baseProps,
      battery: { available: true, category: 'good', percent: 82, plugged: false }
    })

    expect(textContent(element)).toContain('🔋 82%')
  })

  it('uses a bolt glyph while charging', () => {
    const element = StatusRule({
      ...baseProps,
      battery: { available: true, category: 'good', percent: 82, plugged: true }
    })

    expect(textContent(element)).toContain('⚡ 82%')
  })

  it('colours the read-out by category (critical → theme statusCritical)', () => {
    const element = StatusRule({
      ...baseProps,
      battery: { available: true, category: 'critical', percent: 7, plugged: false }
    })

    const leaf = findElementWithText(element, '7%')
    expect(leaf?.props.color).toBe(DEFAULT_THEME.color.statusCritical)
  })

  it('omits the segment when battery is null', () => {
    const element = StatusRule({ ...baseProps, battery: null })

    expect(textContent(element)).not.toContain('%🔋')
    expect(textContent(element)).not.toContain('🔋')
  })

  it('omits the segment when no battery is available (desktop/server)', () => {
    const element = StatusRule({
      ...baseProps,
      battery: { available: false, category: 'dim', percent: null, plugged: null }
    })

    expect(textContent(element)).not.toContain('🔋')
  })
})

describe('StatusRule idle-since read-out', () => {
  // The IdleSince component uses hooks, so it can't be invoked outside a
  // renderer — assert on the element tree instead (same reason the duration
  // tests don't check SessionDuration's text).
  const findComponentByName = (node: ReactNodeLike, name: string): React.ReactElement | null => {
    if (node === null || node === undefined || typeof node === 'boolean') {
      return null
    }

    if (Array.isArray(node)) {
      for (const child of node) {
        const found = findComponentByName(child, name)

        if (found) {
          return found
        }
      }

      return null
    }

    if (!React.isValidElement(node)) {
      return null
    }

    if (typeof node.type === 'function' && node.type.name === name) {
      return node
    }

    return findComponentByName(node.props.children, name)
  }

  it('shows time since the last final agent response when idle', () => {
    const endedAt = Date.now() - 42_000

    const element = StatusRule({
      ...baseProps,
      lastTurnEndedAt: endedAt,
      sessionStartedAt: Date.now() - 60_000
    })

    const idle = findComponentByName(element, 'IdleSince')

    expect(idle).not.toBeNull()
    expect(idle!.props.endedAt).toBe(endedAt)
  })

  it('is hidden while a turn is busy', () => {
    const element = StatusRule({
      ...baseProps,
      busy: true,
      lastTurnEndedAt: Date.now() - 42_000,
      turnStartedAt: Date.now()
    })

    expect(findComponentByName(element, 'IdleSince')).toBeNull()
  })

  it('is hidden before the first turn completes', () => {
    const element = StatusRule({
      ...baseProps,
      lastTurnEndedAt: null,
      sessionStartedAt: Date.now() - 60_000
    })

    expect(findComponentByName(element, 'IdleSince')).toBeNull()
  })
})

describe('StatusRule OpenCode Go segment', () => {
  const goAccounts = [
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

  // Synthetic payload with the Go account FIRST — the render order must be
  // provider-fixed (Codex then Go), not payload order.
  const bothProviders = [
    ...goAccounts,
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

  it('renders the full Go r/w/m read-out pinned on wide and medium terminals', () => {
    const wide = textContent(
      StatusRule({ ...baseProps, cols: 120, usage: { ...baseProps.usage, accounts: [...goAccounts] } })
    )
    const medium = textContent(
      StatusRule({ ...baseProps, cols: 80, usage: { ...baseProps.usage, accounts: [...goAccounts] } })
    )

    expect(wide).toContain('Go ● 90/59/53')
    expect(medium).toContain('Go ● 90/59/53')
  })

  it('stays full-form on a narrow terminal where the Codex read-out collapses', () => {
    const rendered = textContent(
      StatusRule({ ...baseProps, cols: 60, usage: { ...baseProps.usage, accounts: [...bothProviders] } })
    )

    // Codex collapses to the min-percentage form below 72 cols, keeping its
    // persistent account circles …
    expect(rendered).toContain('GPT ●○ min 42% · 2')
    // … but Go is pinned in the essential slot: the full r/w/m form survives.
    expect(rendered).toContain('Go ● 90/59/53')
  })

  it('renders Codex then Go in fixed order regardless of payload order', () => {
    const rendered = textContent(
      StatusRule({ ...baseProps, cols: 120, usage: { ...baseProps.usage, accounts: [...bothProviders] } })
    )

    // Go sits in the payload BEFORE the Codex accounts, yet the bar still
    // emits the Codex segment first, then the Go segment, sharing one
    // separator — provider-fixed order.
    expect(rendered).toContain('GPT ● 87/64 ○ 42/91 │ Go ● 90/59/53')
    expect(rendered.indexOf('GPT ● 87/64')).toBeLessThan(rendered.indexOf('Go ● 90/59/53'))
  })

  it('shows no Go segment when the payload has no opencode-go account', () => {
    const noAccounts = textContent(StatusRule({ ...baseProps }))
    const emptyAccounts = textContent(
      StatusRule({ ...baseProps, usage: { ...baseProps.usage, accounts: [] } })
    )
    const codexOnly = textContent(
      StatusRule({
        ...baseProps,
        usage: { ...baseProps.usage, accounts: [...bothProviders].filter(a => a.provider !== 'opencode-go') }
      })
    )

    expect(noAccounts).not.toContain('Go ')
    expect(emptyAccounts).not.toContain('Go ')
    expect(codexOnly).not.toContain('Go ')
  })

  it('renders only the Go segment (no Codex markers) for a Go-only payload', () => {
    const rendered = textContent(
      StatusRule({ ...baseProps, cols: 80, usage: { ...baseProps.usage, accounts: [...goAccounts] } })
    )

    // The Go segment carries its own persistent full circle …
    expect(rendered).toContain('Go ● 90/59/53')
    // … but no Codex branding or empty-circle account markers.
    expect(rendered).not.toContain('GPT')
    expect(rendered).not.toContain('○')
    expect(rendered).not.toContain('Codex')
  })
})
