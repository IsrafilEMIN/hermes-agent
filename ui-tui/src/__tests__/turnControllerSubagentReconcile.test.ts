import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createGatewayEventHandler } from '../app/createGatewayEventHandler.js'
import { clearSpawnHistory, getSpawnHistory, pushSnapshot } from '../app/spawnHistoryStore.js'
import { turnController } from '../app/turnController.js'
import { getTurnState, resetTurnState } from '../app/turnStore.js'
import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'
import type { Msg, SubagentEventPayload } from '../gatewayTypes.js'
import type { SubagentProgress, SubagentStatus } from '../types.js'

// ── Shared harness helpers ─────────────────────────────────────────────
//
// Patch callbacks mirror the real event wiring in createGatewayEventHandler
// (subagent.start / .thinking / .progress / .complete cases) so the
// reconcile logic is exercised with production semantics.

const isTerminal = (s: SubagentStatus) =>
  s === 'completed' || s === 'error' || s === 'failed' || s === 'interrupted' || s === 'timeout'

const keepTerminalElseRunning = (s: SubagentStatus) => (isTerminal(s) ? s : 'running')

const startPatch = (c: SubagentProgress) => (isTerminal(c.status) ? {} : { status: 'running' as const })

// Mirrors the handler's `subagent.complete` case: terminal status from the
// payload (normalized with 'completed' fallback) + duration/summary.
const completePatchFor =
  (p: SubagentEventPayload) =>
  (c: SubagentProgress) => ({
    durationSeconds: p.duration_seconds ?? c.durationSeconds,
    status: (p.status ?? 'completed') as SubagentStatus,
    summary: p.summary || p.text || c.summary
  })

// Mirrors the handler's `subagent.progress` case: append note, never flip a
// terminal row back to running.
const progressPatchFor =
  (p: SubagentEventPayload) =>
  (c: SubagentProgress) => ({
    notes: [...c.notes, String(p.text ?? '').trim()].filter(Boolean),
    status: keepTerminalElseRunning(c.status)
  })

// Mirrors the handler's `subagent.thinking` case.
const thinkingPatchFor =
  (p: SubagentEventPayload) =>
  (c: SubagentProgress) => ({
    status: keepTerminalElseRunning(c.status),
    thinking: [...c.thinking, String(p.text ?? '').trim()].filter(Boolean)
  })

const runningRow = (id: string, goal = id): SubagentProgress => ({
  depth: 0,
  goal,
  id,
  index: 0,
  notes: [],
  parentId: null,
  startedAt: 1_700_000_000,
  status: 'running',
  taskCount: 1,
  thinking: [],
  toolCount: 0,
  tools: [],
  toolsets: []
})

const completePayload = (id: string, extra: Partial<SubagentEventPayload> = {}): SubagentEventPayload => ({
  goal: id,
  status: 'completed',
  subagent_id: id,
  task_index: 0,
  ...extra
})

const ref = <T>(current: T) => ({ current })

const buildCtx = (appended: Msg[]) =>
  ({
    composer: {
      dequeue: () => undefined,
      queueEditRef: ref<null | number>(null),
      sendQueued: vi.fn(),
      setInput: vi.fn()
    },
    gateway: {
      gw: { request: vi.fn() },
      rpc: vi.fn(async () => null)
    },
    session: {
      STARTUP_RESUME_ID: '',
      colsRef: ref(80),
      newSession: vi.fn(),
      resetSession: vi.fn(),
      resumeById: vi.fn(),
      setCatalog: vi.fn()
    },
    submission: {
      submitRef: { current: vi.fn() }
    },
    system: {
      bellOnComplete: false,
      sys: vi.fn()
    },
    transcript: {
      appendMessage: (msg: Msg) => appended.push(msg),
      panel: (title: string, sections: any[]) =>
        appended.push({ kind: 'panel', panelData: { sections, title }, role: 'system', text: '' }),
      setHistoryItems: vi.fn()
    },
    voice: {
      setProcessing: vi.fn(),
      setRecording: vi.fn(),
      setVoiceEnabled: vi.fn()
    }
  }) as any

// ── Regression: late subagent events after message.complete ────────────
//
// recordMessageComplete snapshots turn.subagents into spawn history, then
// idle() clears the live list.  A `subagent.complete` (or thinking/tool/
// progress) arriving after that used to be dropped by the createIfMissing:
// false guard, leaving the archived row 'running' forever — /agents showed
// a finished background batch still "running".  The fix reconciles the
// archived snapshot instead of resurrecting live state.

describe('turnController — late subagent events reconcile archived snapshots', () => {
  beforeEach(() => {
    resetUiState()
    resetTurnState()
    clearSpawnHistory()
    turnController.fullReset()
    patchUiState({ sid: 'sess-1' })
  })

  it('archives a running row at message.complete, then a late subagent.complete makes it terminal without resurrecting live state', () => {
    const appended: Msg[] = []
    const onEvent = createGatewayEventHandler(buildCtx(appended))

    onEvent({ payload: { goal: 'fix bug', subagent_id: 'sa-1', task_index: 0 }, type: 'subagent.start' } as any)
    expect(getTurnState().subagents.find(s => s.id === 'sa-1')?.status).toBe('running')

    onEvent({ payload: { text: 'done' }, type: 'message.complete' } as any)

    // Turn ended: live list cleared, tree archived with the row still running.
    expect(getTurnState().subagents).toEqual([])
    expect(getSpawnHistory()[0]?.subagents.find(s => s.id === 'sa-1')?.status).toBe('running')

    // Late completion from the background batch arrives after the archive.
    onEvent(
      {
        payload: {
          duration_seconds: 12,
          goal: 'fix bug',
          status: 'completed',
          subagent_id: 'sa-1',
          summary: 'fixed',
          task_index: 0
        },
        type: 'subagent.complete'
      } as any
    )

    expect(getTurnState().subagents).toEqual([])
    const archived = getSpawnHistory()[0]!.subagents.find(s => s.id === 'sa-1')!

    expect(archived.status).toBe('completed')
    expect(archived.summary).toBe('fixed')
    expect(archived.durationSeconds).toBe(12)
  })

  it('drops late events whose id matches neither live state nor any archived snapshot', () => {
    turnController.startMessage()
    turnController.upsertSubagent({ goal: 'keep', subagent_id: 'sa-keep', task_index: 0 }, startPatch)
    turnController.recordMessageComplete({ text: 'done' })

    const payload = completePayload('sa-ghost')
    turnController.upsertSubagent(payload, completePatchFor(payload), { createIfMissing: false })

    // No resurrection into live state, no new history entry, archived row untouched.
    expect(getTurnState().subagents).toEqual([])
    expect(getSpawnHistory()).toHaveLength(1)
    expect(getSpawnHistory()[0]!.subagents[0]!.id).toBe('sa-keep')
    expect(getSpawnHistory()[0]!.subagents[0]!.status).toBe('running')
  })

  it('reconciles into the correct snapshot when background batches finish out of order', () => {
    // Turn 1 archives sa-a, turn 2 archives sa-b.  sa-a's snapshot is NOT
    // history[0] — a naive "always fix the newest snapshot" would miss it.
    pushSnapshot([runningRow('sa-a')], { sessionId: 'sess-1', startedAt: null })
    pushSnapshot([runningRow('sa-b')], { sessionId: 'sess-1', startedAt: null })

    const payload = completePayload('sa-a', { duration_seconds: 5, summary: 'old batch done' })
    turnController.upsertSubagent(payload, completePatchFor(payload), { createIfMissing: false })

    expect(getSpawnHistory()[0]!.subagents[0]!.id).toBe('sa-b')
    expect(getSpawnHistory()[0]!.subagents[0]!.status).toBe('running')

    const older = getSpawnHistory()[1]!.subagents[0]!

    expect(older.id).toBe('sa-a')
    expect(older.status).toBe('completed')
    expect(older.summary).toBe('old batch done')
    expect(getTurnState().subagents).toEqual([])
  })

  it('prefers the current session snapshot when composite ids collide across sessions', () => {
    // Same composite-style id in two sessions; the OTHER session's snapshot
    // is newer.  Newest-first without a session preference would corrupt it.
    pushSnapshot([runningRow('sa-x')], { sessionId: 'sess-1', startedAt: null })
    pushSnapshot([runningRow('sa-x')], { sessionId: 'sess-2', startedAt: null })

    const payload = completePayload('sa-x')
    turnController.upsertSubagent(payload, completePatchFor(payload), { createIfMissing: false })

    expect(getSpawnHistory()[0]!.sessionId).toBe('sess-2')
    expect(getSpawnHistory()[0]!.subagents[0]!.status).toBe('running')
    expect(getSpawnHistory()[1]!.sessionId).toBe('sess-1')
    expect(getSpawnHistory()[1]!.subagents[0]!.status).toBe('completed')
  })

  it('keeps archived terminal status monotonic under late update-only events', () => {
    pushSnapshot([{ ...runningRow('sa-1'), status: 'completed' }], { sessionId: 'sess-1', startedAt: null })

    turnController.upsertSubagent(
      { goal: 'sa-1', subagent_id: 'sa-1', task_index: 0, text: 'still streaming thoughts' },
      thinkingPatchFor({ goal: 'sa-1', subagent_id: 'sa-1', task_index: 0, text: 'still streaming thoughts' }),
      { createIfMissing: false }
    )
    turnController.upsertSubagent(
      { goal: 'sa-1', subagent_id: 'sa-1', task_index: 0, text: 'progress note' },
      progressPatchFor({ goal: 'sa-1', subagent_id: 'sa-1', task_index: 0, text: 'progress note' }),
      { createIfMissing: false }
    )

    const archived = getSpawnHistory()[0]!.subagents[0]!

    // Never flipped back to running; late details still land.
    expect(archived.status).toBe('completed')
    expect(archived.thinking).toEqual(['still streaming thoughts'])
    expect(archived.notes).toEqual(['progress note'])
    expect(getTurnState().subagents).toEqual([])
  })

  it('leaves the live path untouched: in-turn events still update the live row and never touch history', () => {
    turnController.startMessage()
    turnController.upsertSubagent({ goal: 'live', subagent_id: 'sa-live', task_index: 0 }, startPatch)
    turnController.upsertSubagent(
      { goal: 'live', subagent_id: 'sa-live', task_index: 0, text: 'thinking' },
      thinkingPatchFor({ goal: 'live', subagent_id: 'sa-live', task_index: 0, text: 'thinking' })
    )

    const live = getTurnState().subagents.find(s => s.id === 'sa-live')!

    expect(live.status).toBe('running')
    expect(live.thinking).toEqual(['thinking'])
    expect(getSpawnHistory()).toEqual([])
    expect(getUiState().sid).toBe('sess-1')
  })
})
