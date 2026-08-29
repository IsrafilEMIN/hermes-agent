import assert from 'node:assert/strict'

import { test } from 'vitest'

import { connectSafehouseLocalBackend, watchSafehouseBackendReady } from './local-backend-attach'
import {
  SAFEHOUSE_BACKEND_KIND,
  SAFEHOUSE_BACKEND_OWNER,
  SafehouseBackendUnavailableError
} from './local-backend-lifecycle'

const record = {
  schemaVersion: 1,
  kind: SAFEHOUSE_BACKEND_KIND,
  owner: SAFEHOUSE_BACKEND_OWNER,
  host: '127.0.0.1',
  port: 8642,
  pid: 4242,
  generation: 3,
  startedAt: '2026-08-27T18:00:00.000Z',
  sourceRoot: '/Users/kutluk/Developer/harness/hermes-agent',
  version: '0.17.0',
  gitSha: 'abc1234',
  healthPath: '/api/health',
  token: 'tok',
  launchCommand: 'scripts/hermes backend-start',
  updateCommand: 'scripts/hermes update',
  restartCommand: 'scripts/restart-managed-backend'
}

test('connectSafehouseLocalBackend attaches to a valid ready record without spawning', async () => {
  const result = await connectSafehouseLocalBackend({
    hermesHome: '/tmp/hermes',
    readFile: () => JSON.stringify(record),
    waitForReady: async () => {},
    probeWebSocket: async () => ({ ok: true }),
    adoptToken: async (_baseUrl, token) => token
  })

  assert.equal(result.connection.mode, 'local')
  assert.equal(result.connection.source, 'safehouse')
  assert.equal(result.connection.baseUrl, 'http://127.0.0.1:8642')
  assert.equal(result.handle.ownsProcess, false)
  assert.equal(result.record.generation, 3)
})

test('connectSafehouseLocalBackend fails closed when the ready record is missing', async () => {
  await assert.rejects(
    () =>
      connectSafehouseLocalBackend({
        hermesHome: '/tmp/hermes',
        readFile: () => {
          throw new Error('ENOENT')
        },
        waitForReady: async () => {
          throw new Error('should not health-check')
        },
        probeWebSocket: async () => ({ ok: true })
      }),
    error => {
      assert.equal(error instanceof SafehouseBackendUnavailableError, true)
      assert.match((error as Error).message, /scripts\/hermes backend-start/)
      assert.match((error as Error).message, /scripts\/hermes update/)
      assert.match((error as Error).message, /does not spawn/)

      return true
    }
  )
})

test('connectSafehouseLocalBackend rejects a non-loopback ready record', async () => {
  await assert.rejects(
    () =>
      connectSafehouseLocalBackend({
        hermesHome: '/tmp/hermes',
        readFile: () => JSON.stringify({ ...record, host: '8.8.8.8' }),
        waitForReady: async () => {},
        probeWebSocket: async () => ({ ok: true })
      }),
    /loopback/
  )
})

test('connectSafehouseLocalBackend rejects owner metadata that is not the safehoused backend', async () => {
  await assert.rejects(
    () =>
      connectSafehouseLocalBackend({
        hermesHome: '/tmp/hermes',
        readFile: () => JSON.stringify({ ...record, owner: 'electron-child', kind: 'desktop-spawn' }),
        waitForReady: async () => {},
        probeWebSocket: async () => ({ ok: true })
      }),
    /owner\/kind/
  )
})

test('watchSafehouseBackendReady reconnects when generation metadata changes', () => {
  let current = { ...record }
  const restarts: number[] = []
  let handler: () => void = () => {}

  const watch = watchSafehouseBackendReady({
    hermesHome: '/tmp/hermes',
    current,
    pollMs: 1,
    unavailablePolls: 2,
    interval: tick => {
      handler = tick

      return { unref() {} }
    },
    readFile: () => JSON.stringify(current),
    onRestart: next => {
      restarts.push(next.generation)
    }
  })

  handler()
  assert.deepEqual(restarts, [])
  current = { ...record, generation: 4, pid: 9000, startedAt: '2026-08-27T18:01:00.000Z' }
  handler()
  assert.deepEqual(restarts, [4])
  watch.stop()
  current = { ...record, generation: 5, pid: 9001 }
  handler()
  assert.deepEqual(restarts, [4])
})

test('watchSafehouseBackendReady reports unavailable after the ready record vanishes', () => {
  const errors: string[] = []
  let missing = false
  let handler: () => void = () => {}

  watchSafehouseBackendReady({
    hermesHome: '/tmp/hermes',
    current: record,
    pollMs: 1,
    unavailablePolls: 2,
    interval: tick => {
      handler = tick

      return { unref() {} }
    },
    readFile: () => {
      if (missing) {
        throw new Error('ENOENT')
      }

      return JSON.stringify(record)
    },
    onRestart: () => {
      throw new Error('should not restart')
    },
    onUnavailable: error => {
      errors.push(error.message)
    }
  })

  missing = true
  handler()
  assert.equal(errors.length, 0)
  handler()
  assert.equal(errors.length, 1)
  assert.match(errors[0], /scripts\/hermes backend-start/)
})
