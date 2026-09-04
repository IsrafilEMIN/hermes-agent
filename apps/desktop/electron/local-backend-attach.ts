import fs from 'node:fs'
import path from 'node:path'

import {
  backendIdentityKey,
  createSafehouseAttachHandle,
  expectedSafehouseEndpoint,
  parseSafehouseBackendRecord,
  resolveLifecycleCommands,
  resolveReadyRecordPath,
  type SafehouseAttachHandle,
  type SafehouseBackendRecord,
  SafehouseBackendUnavailableError,
  safehouseBaseUrl,
  safehouseWsUrl,
  validateSafehouseBackendRecord
} from './local-backend-lifecycle'

export type SafehouseLocalConnection = {
  authMode: 'token'
  baseUrl: string
  mode: 'local'
  source: 'safehouse'
  token: string
  wsUrl: string
}

export type ConnectSafehouseLocalBackendDeps = {
  adoptToken?: (baseUrl: string, token: string) => Promise<string>
  env?: NodeJS.Dict<string | undefined>
  hermesHome: string
  probeWebSocket: (wsUrl: string) => Promise<{ ok: boolean; reason?: string }>
  readFile?: (filePath: string) => string
  waitForReady: (baseUrl: string, token: string) => Promise<void>
}

export type WatchSafehouseBackendReadyDeps = {
  current: SafehouseBackendRecord
  env?: NodeJS.Dict<string | undefined>
  hermesHome: string
  interval?: (handler: () => void, ms: number) => { unref?: () => void }
  onRestart: (next: SafehouseBackendRecord) => void
  onUnavailable?: (error: SafehouseBackendUnavailableError) => void
  pollMs?: number
  readFile?: (filePath: string) => string
  unavailablePolls?: number
}

function readText(filePath: string, readFile?: (filePath: string) => string): string {
  if (readFile) {
    return readFile(filePath)
  }

  return fs.readFileSync(filePath, 'utf8')
}

export function readSafehouseBackendRecord(
  hermesHome: string,
  env: NodeJS.Dict<string | undefined> = process.env,
  readFile?: (filePath: string) => string
): SafehouseBackendRecord | null {
  const readyPath = resolveReadyRecordPath(hermesHome, env)

  try {
    return parseSafehouseBackendRecord(readText(readyPath, readFile))
  } catch {
    return null
  }
}

function resolveRecordToken(
  record: SafehouseBackendRecord,
  hermesHome: string,
  readFile?: (filePath: string) => string
): string {
  if (record.token) {
    return record.token
  }

  if (!record.tokenPath) {
    return ''
  }

  const tokenPath = path.isAbsolute(record.tokenPath) ? record.tokenPath : path.join(hermesHome, record.tokenPath)

  try {
    return readText(tokenPath, readFile).trim()
  } catch {
    return ''
  }
}

export async function connectSafehouseLocalBackend(
  deps: ConnectSafehouseLocalBackendDeps
): Promise<{
  connection: SafehouseLocalConnection
  handle: SafehouseAttachHandle
  record: SafehouseBackendRecord
}> {
  const env = deps.env || process.env
  const expected = expectedSafehouseEndpoint(env)
  const commands = resolveLifecycleCommands(env)

  const fail = (reason: string): never => {
    throw new SafehouseBackendUnavailableError({
      host: expected.host,
      port: expected.port,
      reason,
      launchCommand: commands.launchCommand,
      updateCommand: commands.updateCommand,
      restartCommand: commands.restartCommand
    })
  }

  const record = readSafehouseBackendRecord(deps.hermesHome, env, deps.readFile)

  if (!record) {
    return fail(`missing or invalid ready record at ${resolveReadyRecordPath(deps.hermesHome, env)}`)
  }

  const validated = validateSafehouseBackendRecord(record, expected)

  if (validated.ok === false) {
    fail(validated.reason)
  }

  const baseUrl = safehouseBaseUrl(record)
  let token = resolveRecordToken(record, deps.hermesHome, deps.readFile)

  try {
    await deps.waitForReady(baseUrl, token)
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)

    fail(`health check failed: ${detail}`)
  }

  if (deps.adoptToken) {
    try {
      token = await deps.adoptToken(baseUrl, token)
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error)

      fail(`session token adoption failed: ${detail}`)
    }
  }

  const wsUrl = safehouseWsUrl(baseUrl, token)
  const wsProbe = await deps.probeWebSocket(wsUrl)

  if (!wsProbe.ok) {
    fail(`WebSocket (/api/ws) rejected the session token: ${wsProbe.reason || 'unknown error'}`)
  }

  return {
    record,
    handle: createSafehouseAttachHandle(record),
    connection: {
      authMode: 'token',
      baseUrl,
      mode: 'local',
      source: 'safehouse',
      token,
      wsUrl
    }
  }
}

export function watchSafehouseBackendReady(deps: WatchSafehouseBackendReadyDeps): { stop: () => void } {
  const env = deps.env || process.env
  const expected = expectedSafehouseEndpoint(env)
  const commands = resolveLifecycleCommands(env)
  const pollMs = deps.pollMs ?? 500
  const unavailablePolls = deps.unavailablePolls ?? 6
  let currentKey = backendIdentityKey(deps.current)
  let missing = 0
  let stopped = false
  let unavailableSent = false

  const tick = () => {
    if (stopped) {
      return
    }

    const next = readSafehouseBackendRecord(deps.hermesHome, env, deps.readFile)

    if (!next) {
      missing += 1

      if (!unavailableSent && missing >= unavailablePolls) {
        unavailableSent = true
        deps.onUnavailable?.(
          new SafehouseBackendUnavailableError({
            host: expected.host,
            port: expected.port,
            reason: 'ready record disappeared during a backend restart',
            launchCommand: commands.launchCommand,
            updateCommand: commands.updateCommand,
            restartCommand: commands.restartCommand
          })
        )
      }

      return
    }

    const validated = validateSafehouseBackendRecord(next, expected)

    if (validated.ok === false) {
      missing += 1

      return
    }

    missing = 0
    unavailableSent = false
    const nextKey = backendIdentityKey(next)

    if (nextKey !== currentKey) {
      currentKey = nextKey
      deps.onRestart(next)
    }
  }

  const schedule = deps.interval || ((handler, ms) => setInterval(handler, ms))
  const timer = schedule(tick, pollMs)

  if (typeof timer.unref === 'function') {
    timer.unref()
  }

  return {
    stop() {
      stopped = true
      clearInterval(timer as ReturnType<typeof setInterval>)
    }
  }
}
