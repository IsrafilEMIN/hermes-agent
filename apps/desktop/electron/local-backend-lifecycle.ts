import path from 'node:path'

export const SAFEHOUSE_BACKEND_KIND = 'hermes-serve-safehouse'
export const SAFEHOUSE_BACKEND_OWNER = 'hermes-serve-safehouse'
export const SAFEHOUSE_BACKEND_SCHEMA = 1
export const SAFEHOUSE_BACKEND_HOST = '127.0.0.1'
export const SAFEHOUSE_BACKEND_PORT = 8642
export const SAFEHOUSE_BACKEND_READY_FILENAME = 'desktop-backend.json'
export const SAFEHOUSE_BACKEND_LAUNCH_COMMAND = 'scripts/hermes backend-start'
export const SAFEHOUSE_BACKEND_UPDATE_COMMAND = 'scripts/hermes update'
export const SAFEHOUSE_BACKEND_RESTART_COMMAND = 'scripts/restart-managed-backend'
export const SAFEHOUSE_BACKEND_HEALTH_PATH = '/api/health'

export type SafehouseBackendRecord = {
  schemaVersion: number
  kind: string
  owner: string
  host: string
  port: number
  pid: number
  generation: number
  startedAt: string
  sourceRoot: string
  version: string
  gitSha: string
  healthPath: string
  token?: string
  tokenPath?: string
  launchCommand: string
  updateCommand: string
  restartCommand: string
}

export type SafehouseAttachHandle = {
  kind: 'safehouse-attach'
  ownsProcess: false
  killed: true
  exitCode: 0
  signalCode: null
  pid: number
  generation: number
  kill: (signal?: string) => void
}

export type SafehouseEndpoint = {
  host: string
  port: number
}

export function defaultReadyRecordPath(hermesHome: string): string {
  return path.join(hermesHome, SAFEHOUSE_BACKEND_READY_FILENAME)
}

export function resolveReadyRecordPath(
  hermesHome: string,
  env: NodeJS.Dict<string | undefined> = process.env
): string {
  const override = String(env.HERMES_DESKTOP_BACKEND_READY_FILE || '').trim()

  return override || defaultReadyRecordPath(hermesHome)
}

export function expectedSafehouseEndpoint(
  env: NodeJS.Dict<string | undefined> = process.env
): SafehouseEndpoint {
  const host = String(env.HERMES_DESKTOP_BACKEND_HOST || SAFEHOUSE_BACKEND_HOST).trim() || SAFEHOUSE_BACKEND_HOST
  const parsed = Number(env.HERMES_DESKTOP_BACKEND_PORT)
  const port = Number.isInteger(parsed) && parsed > 0 ? parsed : SAFEHOUSE_BACKEND_PORT

  return { host, port }
}

export function resolveLifecycleCommands(env: NodeJS.Dict<string | undefined> = process.env): {
  launchCommand: string
  updateCommand: string
  restartCommand: string
} {
  return {
    launchCommand: String(env.HERMES_DESKTOP_BACKEND_LAUNCH_COMMAND || '').trim() || SAFEHOUSE_BACKEND_LAUNCH_COMMAND,
    updateCommand: String(env.HERMES_DESKTOP_BACKEND_UPDATE_COMMAND || '').trim() || SAFEHOUSE_BACKEND_UPDATE_COMMAND,
    restartCommand:
      String(env.HERMES_DESKTOP_BACKEND_RESTART_COMMAND || '').trim() || SAFEHOUSE_BACKEND_RESTART_COMMAND
  }
}

export function isLoopbackHost(host: string): boolean {
  const normalized = String(host || '')
    .trim()
    .toLowerCase()
    .replace(/^\[|\]$/g, '')

  return normalized === '127.0.0.1' || normalized === 'localhost' || normalized === '::1'
}

export function backendIdentityKey(
  record: Pick<SafehouseBackendRecord, 'generation' | 'gitSha' | 'pid' | 'startedAt'>
): string {
  return `${record.generation}:${record.pid}:${record.startedAt}:${record.gitSha}`
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0
}

function positiveInt(value: unknown): number | null {
  const parsed = typeof value === 'number' ? value : Number(value)

  return Number.isInteger(parsed) && parsed > 0 ? parsed : null
}

export function parseSafehouseBackendRecord(contents: unknown): SafehouseBackendRecord | null {
  let parsed: unknown = contents

  if (typeof contents === 'string') {
    const text = contents.trim()

    if (!text) {
      return null
    }

    try {
      parsed = JSON.parse(text)
    } catch {
      return null
    }
  }

  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return null
  }

  const raw = parsed as Record<string, unknown>
  const port = positiveInt(raw.port)
  const pid = positiveInt(raw.pid)
  const generation = positiveInt(raw.generation)
  const schemaVersion = Number(raw.schemaVersion)
  const host = typeof raw.host === 'string' ? raw.host.trim() : ''
  const sourceRoot = typeof raw.sourceRoot === 'string' ? raw.sourceRoot.trim() : ''
  const version = typeof raw.version === 'string' ? raw.version.trim() : ''
  const startedAt = typeof raw.startedAt === 'string' ? raw.startedAt.trim() : ''
  const kind = typeof raw.kind === 'string' ? raw.kind.trim() : ''
  const owner = typeof raw.owner === 'string' ? raw.owner.trim() : ''

  if (
    schemaVersion !== SAFEHOUSE_BACKEND_SCHEMA ||
    !port ||
    !pid ||
    !generation ||
    !host ||
    !sourceRoot ||
    !version ||
    !startedAt ||
    !kind ||
    !owner
  ) {
    return null
  }

  const healthPathRaw = typeof raw.healthPath === 'string' ? raw.healthPath.trim() : ''
  const token = nonEmptyString(raw.token) ? raw.token.trim() : undefined
  const tokenPath = nonEmptyString(raw.tokenPath) ? raw.tokenPath.trim() : undefined
  const commands = resolveLifecycleCommands()

  return {
    schemaVersion,
    kind,
    owner,
    host,
    port,
    pid,
    generation,
    startedAt,
    sourceRoot,
    version,
    gitSha: typeof raw.gitSha === 'string' ? raw.gitSha.trim() : '',
    healthPath: healthPathRaw || SAFEHOUSE_BACKEND_HEALTH_PATH,
    token,
    tokenPath,
    launchCommand: nonEmptyString(raw.launchCommand) ? raw.launchCommand.trim() : commands.launchCommand,
    updateCommand: nonEmptyString(raw.updateCommand) ? raw.updateCommand.trim() : commands.updateCommand,
    restartCommand: nonEmptyString(raw.restartCommand) ? raw.restartCommand.trim() : commands.restartCommand
  }
}

export function validateSafehouseBackendRecord(
  record: SafehouseBackendRecord,
  expected: SafehouseEndpoint = { host: SAFEHOUSE_BACKEND_HOST, port: SAFEHOUSE_BACKEND_PORT }
): { ok: true } | { ok: false; reason: string } {
  if (record.kind !== SAFEHOUSE_BACKEND_KIND || record.owner !== SAFEHOUSE_BACKEND_OWNER) {
    return {
      ok: false,
      reason: `ready record owner/kind must be ${SAFEHOUSE_BACKEND_OWNER}/${SAFEHOUSE_BACKEND_KIND}`
    }
  }

  if (!isLoopbackHost(record.host)) {
    return { ok: false, reason: `ready record host must be loopback, got ${record.host}` }
  }

  if (!isLoopbackHost(expected.host)) {
    return { ok: false, reason: `expected backend host must be loopback, got ${expected.host}` }
  }

  if (record.port !== expected.port) {
    return {
      ok: false,
      reason: `ready record port ${record.port} does not match expected ${expected.host}:${expected.port}`
    }
  }

  if (!record.version || !record.sourceRoot) {
    return { ok: false, reason: 'ready record is missing version/sourceRoot metadata' }
  }

  return { ok: true }
}

export function createSafehouseAttachHandle(record: SafehouseBackendRecord): SafehouseAttachHandle {
  return {
    kind: 'safehouse-attach',
    ownsProcess: false,
    killed: true,
    exitCode: 0,
    signalCode: null,
    pid: record.pid,
    generation: record.generation,
    kill() {}
  }
}

export function formatSafehouseBackendUnavailable(details: {
  host: string
  port: number
  reason: string
  launchCommand?: string
  updateCommand?: string
  restartCommand?: string
}): string {
  const launchCommand = details.launchCommand || SAFEHOUSE_BACKEND_LAUNCH_COMMAND
  const updateCommand = details.updateCommand || SAFEHOUSE_BACKEND_UPDATE_COMMAND
  const restartCommand = details.restartCommand || SAFEHOUSE_BACKEND_RESTART_COMMAND

  return [
    `Safehouse-wrapped Hermes backend is unavailable at ${details.host}:${details.port}.`,
    details.reason,
    `Start it with: ${launchCommand}`,
    `Restart it with: ${restartCommand}`,
    `Update it with: ${updateCommand}`,
    'Hermes Desktop does not spawn hermes serve. Electron native fs/git IPC remains outside Safehouse.'
  ].join('\n')
}

export class SafehouseBackendUnavailableError extends Error {
  launchCommand: string
  updateCommand: string
  restartCommand: string
  retryable = true
  host: string
  port: number

  constructor(details: {
    host: string
    port: number
    reason: string
    launchCommand?: string
    updateCommand?: string
    restartCommand?: string
  }) {
    super(formatSafehouseBackendUnavailable(details))
    this.name = 'SafehouseBackendUnavailableError'
    this.host = details.host
    this.port = details.port
    this.launchCommand = details.launchCommand || SAFEHOUSE_BACKEND_LAUNCH_COMMAND
    this.updateCommand = details.updateCommand || SAFEHOUSE_BACKEND_UPDATE_COMMAND
    this.restartCommand = details.restartCommand || SAFEHOUSE_BACKEND_RESTART_COMMAND
  }
}

export function safehouseBaseUrl(record: Pick<SafehouseBackendRecord, 'host' | 'port'>): string {
  const host = record.host.includes(':') && !record.host.startsWith('[') ? `[${record.host}]` : record.host

  return `http://${host}:${record.port}`
}

export function safehouseWsUrl(baseUrl: string, token: string): string {
  const url = new URL('/api/ws', baseUrl)

  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'

  if (token) {
    url.searchParams.set('token', token)
  }

  return url.toString()
}
