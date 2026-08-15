import { describe, expect, it } from 'vitest'

import { composerPromptText } from '../lib/prompt.js'

describe('composerPromptText', () => {
  it('returns shell prompt for ! commands', () => {
    expect(composerPromptText('❯', 'coder', true)).toBe('$')
  })

  it('returns the bare branded prompt, ignoring profile name', () => {
    expect(composerPromptText('❯', 'coder')).toBe('❯')
  })

  it('does not prefix default or custom profiles', () => {
    expect(composerPromptText('❯', 'default')).toBe('❯')
    expect(composerPromptText('❯', 'custom')).toBe('❯')
    expect(composerPromptText('❯')).toBe('❯')
  })

  it('uses a Termux-safe ASCII prompt marker in normal mode', () => {
    expect(composerPromptText('❯', 'coder', false, true, 50)).toBe('>')
  })

  it('keeps profile prefix suppressed on narrow Termux widths', () => {
    expect(composerPromptText('❯', 'upstr', false, true, 72)).toBe('>')
  })

  it('keeps the Termux marker safe on very wide panes', () => {
    expect(composerPromptText('❯', 'upstr', false, true, 120)).toBe('>')
  })
})
