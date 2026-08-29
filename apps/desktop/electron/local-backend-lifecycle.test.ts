import assert from 'node:assert/strict'
import path from 'node:path'

import { test } from 'vitest'

import {
  backendIdentityKey,
  createSafehouseAttachHandle,
  expectedSafehouseEndpoint,
  formatSafehouseBackendUnavailable,
  isLoopbackHost,
  parseSafehouseBackendRecord,
  resolveReadyRecordPath,
  SAFEHOUSE_BACKEND_HOST,
  SAFEHOUSE_BACKEND_KIND,
  SAFEHOUSE_BACKEND_OWNER,
  SAFEHOUSE_BACKEND_PORT,
  SafehouseBackendUnavailableError,
  safehouseBaseUrl,
  safehouseWsUrl,
  validateSafehouseBackendRecord
} from './local-backend-lifecycle'

const validRecord = {
  schemaVersion: 1,
  kind: SAFEHOUSE_BACKEND_KIND,
  owner: SAFEHOUSE_BACKEND_OWNER,
  host: SAFEHOUSE_BACKEND_HOST,
  port: SAFEHOUSE_BACKEND_PORT,
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

test('parseSafehouseBackendRecord accepts a complete ready record', () => {
  const parsed = parseSafehouseBackendRecord(JSON.stringify(validRecord))

  assert.equal(parsed?.port, 8642)
  assert.equal(parsed?.owner, SAFEHOUSE_BACKEND_OWNER)
  assert.equal(parsed?.version, '0.17.0')
})

test('parseSafehouseBackendRecord rejects missing version metadata', () => {
  assert.equal(parseSafehouseBackendRecord({ ...validRecord, version: '' }), null)
  assert.equal(parseSafehouseBackendRecord({ ...validRecord, sourceRoot: '' }), null)
  assert.equal(parseSafehouseBackendRecord('{"port":8642}'), null)
})

test('validateSafehouseBackendRecord requires loopback owner-matched endpoint', () => {
  const ok = parseSafehouseBackendRecord(validRecord)

  assert.equal(ok && validateSafehouseBackendRecord(ok).ok, true)
  assert.equal(validateSafehouseBackendRecord({ ...ok!, host: '10.0.0.8' }).ok, false)
  assert.equal(validateSafehouseBackendRecord({ ...ok!, owner: 'electron-child' }).ok, false)
  assert.equal(validateSafehouseBackendRecord({ ...ok!, port: 9999 }).ok, false)
})

test('expectedSafehouseEndpoint honors env overrides', () => {
  assert.deepEqual(expectedSafehouseEndpoint({}), { host: '127.0.0.1', port: 8642 })
  assert.deepEqual(expectedSafehouseEndpoint({ HERMES_DESKTOP_BACKEND_PORT: '8642' }), {
    host: '127.0.0.1',
    port: 8642
  })
})

test('resolveReadyRecordPath prefers HERMES_DESKTOP_BACKEND_READY_FILE', () => {
  assert.equal(
    resolveReadyRecordPath('/tmp/hermes', { HERMES_DESKTOP_BACKEND_READY_FILE: '/tmp/custom.json' }),
    '/tmp/custom.json'
  )
  assert.equal(resolveReadyRecordPath('/tmp/hermes', {}), path.join('/tmp/hermes', 'desktop-backend.json'))
})

test('isLoopbackHost accepts only local addresses', () => {
  assert.equal(isLoopbackHost('127.0.0.1'), true)
  assert.equal(isLoopbackHost('localhost'), true)
  assert.equal(isLoopbackHost('::1'), true)
  assert.equal(isLoopbackHost('192.168.1.9'), false)
})

test('createSafehouseAttachHandle never claims process ownership', () => {
  const handle = createSafehouseAttachHandle(validRecord)

  assert.equal(handle.ownsProcess, false)
  assert.equal(handle.killed, true)
  assert.equal(handle.exitCode, 0)
  assert.equal(handle.pid, 4242)
})

test('backendIdentityKey changes across restart metadata', () => {
  const first = backendIdentityKey(validRecord)
  const restarted = backendIdentityKey({ ...validRecord, generation: 4, pid: 9999 })

  assert.notEqual(first, restarted)
})

test('unavailable error names launch and update commands', () => {
  const error = new SafehouseBackendUnavailableError({
    host: '127.0.0.1',
    port: 8642,
    reason: 'ready record missing'
  })

  assert.match(error.message, /scripts\/hermes backend-start/)
  assert.match(error.message, /scripts\/hermes update/)
  assert.match(error.message, /does not spawn hermes serve/)
  assert.equal(error.retryable, true)
  assert.match(formatSafehouseBackendUnavailable({ host: '127.0.0.1', port: 8642, reason: 'x' }), /Restart it with/)
})

test('safehouse URLs stay on the ready-record endpoint', () => {
  assert.equal(safehouseBaseUrl(validRecord), 'http://127.0.0.1:8642')
  assert.equal(safehouseWsUrl('http://127.0.0.1:8642', 'tok'), 'ws://127.0.0.1:8642/api/ws?token=tok')
})
